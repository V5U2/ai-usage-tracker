#!/usr/bin/env python3
"""
Local OTLP/HTTP receiver for Codex usage observability.

It stores every received payload in SQLite and extracts token-like numeric
attributes from OTEL logs, traces, and metrics into a simple usage table.
Use Codex's OTEL JSON protocol for best results.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import html
import json
import os
import secrets
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable, Sequence, TextIO
from urllib import error as urlerror
from urllib import parse, request

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback.
    tomllib = None  # type: ignore[assignment]

DEFAULT_DB = Path(os.environ.get("CODEX_USAGE_DB", "codex_usage.sqlite"))
DEFAULT_SERVER_DB = Path(os.environ.get("CODEX_USAGE_SERVER_DB", "codex_usage_server.sqlite"))
DEFAULT_CONFIG = Path(os.environ.get("CODEX_USAGE_CONFIG", "codex_usage_observer.toml"))
DEFAULT_MAX_BODY_BYTES = int(os.environ.get("CODEX_USAGE_MAX_BODY_BYTES", str(50 * 1024 * 1024)))
SENSITIVE_ATTR_KEYS = {
    "authorization",
    "cookie",
    "set-cookie",
    "user.account_id",
    "user.email",
}
SENSITIVE_ATTR_PARTS = (
    "api_key",
    "apikey",
    "auth_token",
    "bearer",
    "credential",
    "password",
    "secret",
)
TOKEN_KEYS = {
    "input": (
        "input_tokens",
        "input_token_count",
        "prompt_tokens",
        "usage.input_tokens",
        "gen_ai.usage.input_tokens",
        "llm.usage.prompt_tokens",
    ),
    "output": (
        "output_tokens",
        "output_token_count",
        "completion_tokens",
        "usage.output_tokens",
        "gen_ai.usage.output_tokens",
        "llm.usage.completion_tokens",
    ),
    "total": (
        "total_tokens",
        "tool_token_count",
        "usage.total_tokens",
        "gen_ai.usage.total_tokens",
        "llm.usage.total_tokens",
    ),
    "cached": (
        "cached_tokens",
        "cached_token_count",
        "input_cached_tokens",
        "usage.cached_tokens",
        "usage.input_cached_tokens",
        "gen_ai.usage.input_cached_tokens",
    ),
    "reasoning": (
        "reasoning_tokens",
        "reasoning_token_count",
        "output_reasoning_tokens",
        "usage.output_reasoning_tokens",
        "gen_ai.usage.output_reasoning_tokens",
    ),
}
REPORT_COLUMNS = (
    "period",
    "model",
    "session_id",
    "events",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_tokens",
    "reasoning_tokens",
    "first_seen",
    "last_seen",
)
DISPLAY_NAMES = {
    "period": "day",
    "model": "model",
    "session_id": "session",
    "events": "events",
    "input_tokens": "input",
    "output_tokens": "output",
    "total_tokens": "total",
    "cached_tokens": "cached",
    "reasoning_tokens": "reason",
    "first_seen": "first",
    "last_seen": "last",
}
NUMERIC_COLUMNS = {
    "events",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_tokens",
    "reasoning_tokens",
}
TIME_COLUMNS = {"first_seen", "last_seen"}


@dataclass(frozen=True)
class StorageConfig:
    raw_payload_body: bool = True
    extracted_attributes: str = "redacted"
    model: bool = True
    session_id: bool = True
    thread_id: bool = True
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES


@dataclass(frozen=True)
class RemoteServerConfig:
    endpoint: str | None = None
    api_key: str | None = None
    batch_size: int = 100
    timeout_seconds: int = 10


@dataclass(frozen=True)
class ServerConfig:
    admin_api_key: str | None = None
    host: str = "127.0.0.1"
    port: int = 8318
    db: str = str(DEFAULT_SERVER_DB)


@dataclass(frozen=True)
class AppConfig:
    client_name: str = "local"
    storage: StorageConfig = field(default_factory=StorageConfig)
    server: RemoteServerConfig = field(default_factory=RemoteServerConfig)
    central: ServerConfig = field(default_factory=ServerConfig)
    redaction_keys: frozenset[str] = frozenset(SENSITIVE_ATTR_KEYS)
    redaction_key_parts: tuple[str, ...] = SENSITIVE_ATTR_PARTS


DEFAULT_APP_CONFIG = AppConfig()


def bool_config(data: dict[str, Any], key: str, default: bool) -> bool:
    value = data.get(key, default)
    if isinstance(value, bool):
        return value
    raise ValueError(f"{key} must be true or false")


def int_config(data: dict[str, Any], key: str, default: int) -> int:
    value = data.get(key, default)
    if isinstance(value, int) and value >= 0:
        return value
    raise ValueError(f"{key} must be a non-negative integer")


def list_config(data: dict[str, Any], key: str, default: Iterable[str]) -> tuple[str, ...]:
    value = data.get(key)
    if value is None:
        return tuple(default)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    raise ValueError(f"{key} must be a list of strings")


def str_config(data: dict[str, Any], key: str, default: str) -> str:
    value = data.get(key, default)
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError(f"{key} must be a non-empty string")


def optional_str_config(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError(f"{key} must be a non-empty string when provided")


def load_config(path: Path | None) -> AppConfig:
    if path is None or not path.exists():
        return DEFAULT_APP_CONFIG
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        if tomllib is None:
            raise ValueError("TOML config files require Python 3.11+; use JSON on this Python version")
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("config root must be a table/object")

    storage_data = data.get("storage", {})
    server_data = data.get("server", {})
    central_data = data.get("central_server", {})
    redaction_data = data.get("redaction", {})
    if not isinstance(storage_data, dict):
        raise ValueError("storage must be a table/object")
    if not isinstance(server_data, dict):
        raise ValueError("server must be a table/object")
    if not isinstance(central_data, dict):
        raise ValueError("central_server must be a table/object")
    if not isinstance(redaction_data, dict):
        raise ValueError("redaction must be a table/object")

    extracted_attributes = storage_data.get("extracted_attributes", "redacted")
    if extracted_attributes not in ("redacted", "full", "none"):
        raise ValueError('storage.extracted_attributes must be "redacted", "full", or "none"')

    storage = StorageConfig(
        raw_payload_body=bool_config(storage_data, "raw_payload_body", True),
        extracted_attributes=str(extracted_attributes),
        model=bool_config(storage_data, "model", True),
        session_id=bool_config(storage_data, "session_id", True),
        thread_id=bool_config(storage_data, "thread_id", True),
        max_body_bytes=int_config(storage_data, "max_body_bytes", DEFAULT_MAX_BODY_BYTES),
    )
    remote_server = RemoteServerConfig(
        endpoint=optional_str_config(server_data, "endpoint"),
        api_key=optional_str_config(server_data, "api_key"),
        batch_size=int_config(server_data, "batch_size", 100),
        timeout_seconds=int_config(server_data, "timeout_seconds", 10),
    )
    central = ServerConfig(
        admin_api_key=optional_str_config(central_data, "admin_api_key"),
        host=str_config(central_data, "host", "127.0.0.1"),
        port=int_config(central_data, "port", 8318),
        db=str_config(central_data, "db", str(DEFAULT_SERVER_DB)),
    )
    return AppConfig(
        client_name=str_config(data, "client_name", "local"),
        storage=storage,
        server=remote_server,
        central=central,
        redaction_keys=frozenset(key.lower() for key in list_config(redaction_data, "keys", SENSITIVE_ATTR_KEYS)),
        redaction_key_parts=tuple(
            part.lower() for part in list_config(redaction_data, "key_parts", SENSITIVE_ATTR_PARTS)
        ),
    )


def connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("pragma journal_mode=wal")
    con.execute(
        """
        create table if not exists raw_payloads (
            id integer primary key autoincrement,
            received_at text not null,
            path text not null,
            content_type text,
            body blob not null
        )
        """
    )
    con.execute(
        """
        create table if not exists usage_events (
            id integer primary key autoincrement,
            raw_payload_id integer not null,
            received_at text not null,
            signal text not null,
            event_name text,
            model text,
            session_id text,
            thread_id text,
            input_tokens integer default 0,
            output_tokens integer default 0,
            total_tokens integer default 0,
            cached_tokens integer default 0,
            reasoning_tokens integer default 0,
            attributes_json text not null,
            foreign key(raw_payload_id) references raw_payloads(id)
        )
        """
    )
    con.execute("create index if not exists idx_raw_payloads_received_at on raw_payloads(received_at)")
    con.execute("create index if not exists idx_usage_events_received_at on usage_events(received_at)")
    con.execute("create index if not exists idx_usage_events_model on usage_events(model)")
    con.execute("create index if not exists idx_usage_events_session on usage_events(session_id)")
    ensure_client_sync_schema(con)
    return con


def table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in con.execute(f"pragma table_info({table})")}


def ensure_client_sync_schema(con: sqlite3.Connection) -> None:
    columns = table_columns(con, "usage_events")
    if "client_event_id" not in columns:
        con.execute("alter table usage_events add column client_event_id text")
    if "synced_at" not in columns:
        con.execute("alter table usage_events add column synced_at text")
    if "sync_attempts" not in columns:
        con.execute("alter table usage_events add column sync_attempts integer default 0")
    if "last_sync_error" not in columns:
        con.execute("alter table usage_events add column last_sync_error text")
    con.execute(
        """
        update usage_events
        set client_event_id = printf('local-%d', id)
        where client_event_id is null or client_event_id = ''
        """
    )
    con.execute("create unique index if not exists idx_usage_events_client_event_id on usage_events(client_event_id)")


def connect_server(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("pragma journal_mode=wal")
    con.execute(
        """
        create table if not exists clients (
            id integer primary key autoincrement,
            client_name text not null unique,
            display_name text not null,
            token_hash text not null,
            created_at text not null,
            updated_at text not null,
            revoked_at text,
            last_seen_at text
        )
        """
    )
    con.execute(
        """
        create table if not exists usage_events (
            id integer primary key autoincrement,
            client_name text not null,
            client_event_id text not null,
            received_at text not null,
            source_received_at text not null,
            signal text not null,
            event_name text,
            model text,
            session_id text,
            thread_id text,
            input_tokens integer default 0,
            output_tokens integer default 0,
            total_tokens integer default 0,
            cached_tokens integer default 0,
            reasoning_tokens integer default 0,
            attributes_json text not null,
            unique(client_name, client_event_id)
        )
        """
    )
    con.execute("create index if not exists idx_server_usage_received_at on usage_events(source_received_at)")
    con.execute("create index if not exists idx_server_usage_client on usage_events(client_name)")
    con.execute("create index if not exists idx_server_usage_model on usage_events(model)")
    return con


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def otel_value(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    for key in (
        "stringValue",
        "intValue",
        "doubleValue",
        "boolValue",
        "bytesValue",
    ):
        if key in value:
            raw = value[key]
            if key == "intValue":
                try:
                    return int(raw)
                except (TypeError, ValueError):
                    return raw
            if key == "doubleValue":
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    return raw
            return raw
    if "arrayValue" in value:
        return [otel_value(item) for item in value["arrayValue"].get("values", [])]
    if "kvlistValue" in value:
        return attrs_to_dict(value["kvlistValue"].get("values", []))
    return value


def attrs_to_dict(attrs: Iterable[dict[str, Any]] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for attr in attrs or []:
        key = attr.get("key")
        if key:
            out[str(key)] = otel_value(attr.get("value"))
    return out


def merge_attrs(*sources: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for source in sources:
        merged.update(source)
    return merged


def int_attr(attrs: dict[str, Any], aliases: tuple[str, ...]) -> int:
    for alias in aliases:
        value = attrs.get(alias)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def first_attr(attrs: dict[str, Any], aliases: tuple[str, ...]) -> str | None:
    for alias in aliases:
        value = attrs.get(alias)
        if value not in (None, ""):
            return str(value)
    return None


def maybe_parse_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    value = value.strip()
    if not value or value[0] not in "[{":
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def flatten_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    flattened: dict[str, Any] = {}

    def visit(prefix: str, value: Any) -> None:
        value = maybe_parse_json(value)
        if isinstance(value, dict):
            for key, child in value.items():
                child_key = str(key)
                visit(f"{prefix}.{child_key}" if prefix else child_key, child)
            return
        flattened[prefix] = value

    for key, value in attrs.items():
        visit(str(key), value)
    return flattened


def redact_attrs(attrs: dict[str, Any], config: AppConfig) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in attrs.items():
        normalized = key.lower()
        if normalized in config.redaction_keys or any(part in normalized for part in config.redaction_key_parts):
            redacted[key] = "[redacted]"
        else:
            redacted[key] = value
    return redacted


def stored_attributes_json(attrs: dict[str, Any], config: AppConfig) -> str:
    if config.storage.extracted_attributes == "none":
        return "{}"
    if config.storage.extracted_attributes == "full":
        stored_attrs = attrs
    else:
        stored_attrs = redact_attrs(attrs, config)
    return json.dumps(stored_attrs, sort_keys=True)


def has_token_signal(attrs: dict[str, Any]) -> bool:
    keys = set(attrs)
    return any(any(alias in keys for alias in aliases) for aliases in TOKEN_KEYS.values())


def usage_from_attrs(
    signal: str,
    event_name: str | None,
    attrs: dict[str, Any],
    config: AppConfig = DEFAULT_APP_CONFIG,
) -> dict[str, Any] | None:
    attrs = flatten_attrs(attrs)
    input_tokens = int_attr(attrs, TOKEN_KEYS["input"])
    output_tokens = int_attr(attrs, TOKEN_KEYS["output"])
    total_tokens = int_attr(attrs, TOKEN_KEYS["total"])
    cached_tokens = int_attr(attrs, TOKEN_KEYS["cached"])
    reasoning_tokens = int_attr(attrs, TOKEN_KEYS["reasoning"])
    if not any((input_tokens, output_tokens, total_tokens, cached_tokens, reasoning_tokens)):
        if not has_token_signal(attrs):
            return None

    if total_tokens == 0 and (input_tokens or output_tokens):
        total_tokens = input_tokens + output_tokens

    return {
        "signal": signal,
        "event_name": event_name,
        "model": first_attr(attrs, ("model", "model_name", "gen_ai.request.model", "gen_ai.response.model"))
        if config.storage.model
        else None,
        "session_id": first_attr(attrs, ("session_id", "session.id", "codex.session_id"))
        if config.storage.session_id
        else None,
        "thread_id": first_attr(attrs, ("thread_id", "thread.id", "codex.thread_id"))
        if config.storage.thread_id
        else None,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cached_tokens": cached_tokens,
        "reasoning_tokens": reasoning_tokens,
        "attributes_json": stored_attributes_json(attrs, config),
    }


def iter_log_records(payload: dict[str, Any]) -> Iterable[tuple[str | None, dict[str, Any]]]:
    for resource in payload.get("resourceLogs", []):
        resource_attrs = attrs_to_dict(resource.get("resource", {}).get("attributes"))
        for scope in resource.get("scopeLogs", []):
            scope_attrs = attrs_to_dict(scope.get("scope", {}).get("attributes"))
            for record in scope.get("logRecords", []):
                attrs = merge_attrs(resource_attrs, scope_attrs, attrs_to_dict(record.get("attributes")))
                body = maybe_parse_json(otel_value(record.get("body")))
                if isinstance(body, dict):
                    attrs = merge_attrs(attrs, body)
                yield first_attr(attrs, ("event.name", "name")), attrs


def iter_spans(payload: dict[str, Any]) -> Iterable[tuple[str | None, dict[str, Any]]]:
    for resource in payload.get("resourceSpans", []):
        resource_attrs = attrs_to_dict(resource.get("resource", {}).get("attributes"))
        for scope in resource.get("scopeSpans", []):
            scope_attrs = attrs_to_dict(scope.get("scope", {}).get("attributes"))
            for span in scope.get("spans", []):
                attrs = merge_attrs(resource_attrs, scope_attrs, attrs_to_dict(span.get("attributes")))
                yield span.get("name"), attrs


def metric_points(metric: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for data_key in ("sum", "gauge", "histogram"):
        data = metric.get(data_key)
        if not data:
            continue
        for point in data.get("dataPoints", []):
            yield point


def iter_metrics(payload: dict[str, Any]) -> Iterable[tuple[str | None, dict[str, Any]]]:
    for resource in payload.get("resourceMetrics", []):
        resource_attrs = attrs_to_dict(resource.get("resource", {}).get("attributes"))
        for scope in resource.get("scopeMetrics", []):
            scope_attrs = attrs_to_dict(scope.get("scope", {}).get("attributes"))
            for metric in scope.get("metrics", []):
                name = metric.get("name")
                for point in metric_points(metric):
                    attrs = merge_attrs(resource_attrs, scope_attrs, attrs_to_dict(point.get("attributes")))
                    value = point.get("asInt", point.get("asDouble"))
                    if name and value is not None:
                        attrs[str(name)] = value
                    yield name, attrs


def extract_usage(path: str, body: bytes, config: AppConfig = DEFAULT_APP_CONFIG) -> list[dict[str, Any]]:
    payload = json.loads(body.decode("utf-8"))
    if path.endswith("/v1/logs"):
        events = (usage_from_attrs("logs", name, attrs, config) for name, attrs in iter_log_records(payload))
    elif path.endswith("/v1/traces"):
        events = (usage_from_attrs("traces", name, attrs, config) for name, attrs in iter_spans(payload))
    elif path.endswith("/v1/metrics"):
        events = (usage_from_attrs("metrics", name, attrs, config) for name, attrs in iter_metrics(payload))
    else:
        events = []
    return [event for event in events if event]


def insert_payload(
    con: sqlite3.Connection,
    path: str,
    content_type: str | None,
    body: bytes,
    config: AppConfig = DEFAULT_APP_CONFIG,
) -> int:
    received_at = now_iso()
    stored_body = body if config.storage.raw_payload_body else b""
    cur = con.execute(
        "insert into raw_payloads(received_at, path, content_type, body) values (?, ?, ?, ?)",
        (received_at, path, content_type, stored_body),
    )
    return int(cur.lastrowid)


def insert_usage(con: sqlite3.Connection, raw_id: int, received_at: str, events: list[dict[str, Any]]) -> None:
    for event in events:
        client_event_id = event.get("client_event_id") or f"evt_{secrets.token_urlsafe(18)}"
        con.execute(
            """
            insert into usage_events(
                raw_payload_id, received_at, signal, event_name, model, session_id, thread_id,
                input_tokens, output_tokens, total_tokens, cached_tokens, reasoning_tokens, attributes_json,
                client_event_id
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                raw_id,
                received_at,
                event["signal"],
                event["event_name"],
                event["model"],
                event["session_id"],
                event["thread_id"],
                event["input_tokens"],
                event["output_tokens"],
                event["total_tokens"],
                event["cached_tokens"],
                event["reasoning_tokens"],
                event["attributes_json"],
                client_event_id,
            ),
        )


def parse_datetime_filter(value: str | None, *, end_of_day: bool = False) -> str | None:
    if not value:
        return None
    if len(value) == 10:
        suffix = "23:59:59" if end_of_day else "00:00:00"
        return dt.datetime.fromisoformat(f"{value}T{suffix}+00:00").isoformat(timespec="seconds")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def where_clause(args: argparse.Namespace) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    since = parse_datetime_filter(getattr(args, "since", None))
    until = parse_datetime_filter(getattr(args, "until", None), end_of_day=True)
    if since:
        clauses.append("received_at >= ?")
        params.append(since)
    if until:
        clauses.append("received_at <= ?")
        params.append(until)
    if getattr(args, "model", None):
        clauses.append("model = ?")
        params.append(args.model)
    if getattr(args, "session_id", None):
        clauses.append("session_id = ?")
        params.append(args.session_id)
    return ("where " + " and ".join(clauses), params) if clauses else ("", params)


def raw_where_clause(args: argparse.Namespace) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    since = parse_datetime_filter(getattr(args, "since", None))
    until = parse_datetime_filter(getattr(args, "until", None), end_of_day=True)
    if since:
        clauses.append("received_at >= ?")
        params.append(since)
    if until:
        clauses.append("received_at <= ?")
        params.append(until)
    return ("where " + " and ".join(clauses), params) if clauses else ("", params)


def group_expression(group_by: str) -> tuple[str, str, str]:
    period_expr = "''"
    model_expr = "''"
    session_expr = "''"
    if group_by in ("day", "day-model", "day-session"):
        period_expr = "substr(received_at, 1, 10)"
    if group_by in ("model", "day-model"):
        model_expr = "coalesce(model, '(unknown)')"
    if group_by in ("session", "day-session"):
        session_expr = "coalesce(session_id, '(unknown)')"
    return period_expr, model_expr, session_expr


def usage_report_rows(con: sqlite3.Connection, args: argparse.Namespace) -> list[sqlite3.Row]:
    period_expr, model_expr, session_expr = group_expression(args.group_by)
    where, params = where_clause(args)
    order_by = "last_seen desc"
    if args.group_by.startswith("day"):
        order_by = "period desc, total_tokens desc"
    elif args.group_by in ("model", "session"):
        order_by = "total_tokens desc"
    query = f"""
        select
            {period_expr} as period,
            {model_expr} as model,
            {session_expr} as session_id,
            count(*) as events,
            coalesce(sum(input_tokens), 0) as input_tokens,
            coalesce(sum(output_tokens), 0) as output_tokens,
            coalesce(sum(total_tokens), 0) as total_tokens,
            coalesce(sum(cached_tokens), 0) as cached_tokens,
            coalesce(sum(reasoning_tokens), 0) as reasoning_tokens,
            min(received_at) as first_seen,
            max(received_at) as last_seen
        from usage_events
        {where}
        group by 1, 2, 3
        order by {order_by}
        limit ?
    """
    return con.execute(query, (*params, args.limit)).fetchall()


def write_csv(rows: Sequence[sqlite3.Row], out: TextIO) -> None:
    writer = csv.DictWriter(out, fieldnames=REPORT_COLUMNS)
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row[column] for column in REPORT_COLUMNS})


def format_timestamp(value: str | None) -> str:
    if not value:
        return ""
    return value.replace("T", " ").replace("+00:00", "Z")


def format_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if amount < 1024 or unit == "GiB":
            if unit == "B":
                return f"{int(amount)} {unit}"
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} GiB"


def payload_shape(content_type: str | None, body: bytes) -> str:
    if not content_type or "json" not in content_type:
        return content_type or "unknown"
    if not body:
        return "empty-json"
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "invalid-json"
    if isinstance(payload, dict) and payload:
        return ",".join(str(key) for key in payload.keys())
    return type(payload).__name__


def serve_payload_log_line(
    *,
    path: str,
    content_type: str | None,
    body_bytes: int,
    shape: str,
    raw_body_retained: bool,
    events: int,
    raw_id: int,
) -> str:
    raw_state = "kept" if raw_body_retained else "metadata-only"
    content = content_type or "-"
    return (
        f"{time.strftime('%H:%M:%S')} received {path} "
        f"payload={format_bytes(body_bytes)} shape={shape} content_type={content} "
        f"raw_body={raw_state} usage_events={events} raw_id={raw_id}"
    )


def format_cell(column: str, value: Any) -> str:
    if value in (None, ""):
        return ""
    if column in NUMERIC_COLUMNS:
        return f"{int(value):,}"
    if column in TIME_COLUMNS:
        return format_timestamp(str(value))
    return str(value)


def default_columns(group_by: str) -> tuple[str, ...]:
    if group_by == "total":
        return (
            "events",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_tokens",
            "reasoning_tokens",
            "first_seen",
            "last_seen",
        )
    if group_by == "model":
        return (
            "model",
            "events",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_tokens",
            "reasoning_tokens",
            "last_seen",
        )
    if group_by == "day":
        return (
            "period",
            "events",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_tokens",
            "reasoning_tokens",
        )
    if group_by == "session":
        return (
            "session_id",
            "events",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_tokens",
            "reasoning_tokens",
            "last_seen",
        )
    if group_by == "day-session":
        return (
            "period",
            "session_id",
            "events",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_tokens",
            "reasoning_tokens",
        )
    return (
        "period",
        "model",
        "events",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_tokens",
        "reasoning_tokens",
    )


def print_compact_rows(rows: Sequence[sqlite3.Row], columns: Sequence[str]) -> None:
    for index, row in enumerate(rows, start=1):
        first_line = []
        second_line = []
        for column in columns:
            cell = format_cell(column, row[column])
            if not cell:
                continue
            entry = f"{DISPLAY_NAMES.get(column, column)}: {cell}"
            if column in ("period", "model", "session_id", "events", "total_tokens", "last_seen"):
                first_line.append(entry)
            else:
                second_line.append(entry)
        if len(rows) > 1:
            print(f"[{index}] " + "  ".join(first_line))
        else:
            print("  ".join(first_line))
        if second_line:
            print("  ".join(second_line))
        print()


def print_table(rows: Sequence[sqlite3.Row], columns: Sequence[str] = REPORT_COLUMNS) -> None:
    if not rows:
        print("No matching usage events.")
        return
    formatted_rows = [{column: format_cell(column, row[column]) for column in columns} for row in rows]
    widths = {
        column: max(len(DISPLAY_NAMES.get(column, column)), *(len(row[column]) for row in formatted_rows))
        for column in columns
    }
    table_width = sum(widths.values()) + (2 * (len(columns) - 1))
    terminal_width = shutil.get_terminal_size((120, 20)).columns
    if table_width > terminal_width:
        print_compact_rows(rows, columns)
        return
    print("  ".join(DISPLAY_NAMES.get(column, column).ljust(widths[column]) for column in columns))
    print("  ".join("-" * widths[column] for column in columns))
    for row in formatted_rows:
        cells = []
        for column in columns:
            cell = row[column]
            if column in NUMERIC_COLUMNS:
                cells.append(cell.rjust(widths[column]))
            else:
                cells.append(cell.ljust(widths[column]))
        print("  ".join(cells))


class Receiver(BaseHTTPRequestHandler):
    db_path: Path = DEFAULT_DB
    app_config: AppConfig = DEFAULT_APP_CONFIG

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        if length > self.app_config.storage.max_body_bytes:
            self.send_response(413)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"payload too large"}')
            sys.stderr.write(
                f"{time.strftime('%H:%M:%S')} rejected {self.path} "
                f"payload={format_bytes(length)} max={format_bytes(self.app_config.storage.max_body_bytes)}\n"
            )
            return
        body = self.rfile.read(length)
        content_type = self.headers.get("content-type")
        received_at = now_iso()
        shape = payload_shape(content_type, body)

        con = connect(self.db_path)
        try:
            raw_id = insert_payload(con, self.path, content_type, body, self.app_config)
            events: list[dict[str, Any]] = []
            if content_type and "json" in content_type:
                try:
                    events = extract_usage(self.path, body, self.app_config)
                except Exception as exc:  # Keep raw data even when parser lags schema changes.
                    sys.stderr.write(f"parse error for {self.path}: {exc}\n")
            insert_usage(con, raw_id, received_at, events)
            con.commit()
            if events and self.app_config.server.endpoint and self.app_config.server.api_key:
                _, _, sync_error = sync_pending_usage(con, self.app_config, limit=len(events))
                con.commit()
                if sync_error:
                    sys.stderr.write(f"sync error for {self.path}: {sync_error}\n")
        finally:
            con.close()

        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")
        sys.stderr.write(
            serve_payload_log_line(
                path=self.path,
                content_type=content_type,
                body_bytes=len(body),
                shape=shape,
                raw_body_retained=self.app_config.storage.raw_payload_body,
                events=len(events),
                raw_id=raw_id,
            )
            + "\n"
        )

    def log_message(self, fmt: str, *args: Any) -> None:
        return


class ServerReceiver(BaseHTTPRequestHandler):
    db_path: Path = DEFAULT_SERVER_DB
    app_config: AppConfig = DEFAULT_APP_CONFIG

    def send_json(self, status: int, payload: dict[str, Any] | list[dict[str, Any]]) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, status: int, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length", "0"))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def read_form(self) -> dict[str, str]:
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length).decode("utf-8") if length else ""
        parsed = parse.parse_qs(body)
        return {key: values[0] for key, values in parsed.items() if values}

    def redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("location", location)
        self.end_headers()

    def render_admin(self, con: sqlite3.Connection, *, message: str | None = None, token: str | None = None) -> str:
        clients = con.execute(
            """
            select client_name, display_name, created_at, updated_at, revoked_at, last_seen_at
            from clients
            order by revoked_at is not null, client_name
            """
        ).fetchall()
        rows = []
        for client in clients:
            client_name = html.escape(client["client_name"])
            display_name = html.escape(client["display_name"])
            revoked = html.escape(client["revoked_at"] or "")
            last_seen = html.escape(client["last_seen_at"] or "")
            status = "revoked" if client["revoked_at"] else "active"
            revoke_button = (
                ""
                if client["revoked_at"]
                else f"""
                <form method="post" action="/admin/clients/revoke" class="inline">
                  <input type="hidden" name="client_name" value="{client_name}">
                  <button type="submit">Revoke</button>
                </form>
                """
            )
            rows.append(
                f"""
                <tr>
                  <td>{client_name}</td>
                  <td>
                    <form method="post" action="/admin/clients/rename" class="inline">
                      <input type="hidden" name="client_name" value="{client_name}">
                      <input name="display_name" value="{display_name}">
                      <button type="submit">Rename</button>
                    </form>
                  </td>
                  <td>{status}</td>
                  <td>{html.escape(client["created_at"])}</td>
                  <td>{html.escape(client["updated_at"])}</td>
                  <td>{last_seen}</td>
                  <td>{revoked}</td>
                  <td>{revoke_button}</td>
                </tr>
                """
            )
        token_block = ""
        if token:
            token_block = f"""
            <section class="notice">
              <strong>New token, shown once:</strong>
              <code>{html.escape(token)}</code>
            </section>
            """
        message_block = f"<section class=\"notice\">{html.escape(message)}</section>" if message else ""
        return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>AI Usage Tracker Admin</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #17202a; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
    th, td {{ border-bottom: 1px solid #d7dde3; padding: .5rem; text-align: left; vertical-align: top; }}
    input {{ padding: .35rem; }}
    button {{ padding: .35rem .6rem; }}
    code {{ display: block; padding: .75rem; background: #f3f5f7; margin-top: .5rem; word-break: break-all; }}
    .notice {{ padding: .75rem; background: #eef6ff; border: 1px solid #b9d7f2; margin: 1rem 0; }}
    .inline {{ display: inline; }}
    .create {{ display: flex; gap: .5rem; align-items: end; margin-top: 1rem; }}
    label {{ display: grid; gap: .25rem; }}
  </style>
</head>
<body>
  <h1>AI Usage Tracker Admin</h1>
  {message_block}
  {token_block}
  <h2>Create Client Token</h2>
  <form method="post" action="/admin/clients/create" class="create">
    <label>Client name <input name="client_name" required pattern="[A-Za-z0-9_.-]+"></label>
    <label>Display name <input name="display_name"></label>
    <button type="submit">Create token</button>
  </form>
  <h2>Clients</h2>
  <table>
    <thead>
      <tr><th>Client</th><th>Display name</th><th>Status</th><th>Created</th><th>Updated</th><th>Last seen</th><th>Revoked</th><th>Actions</th></tr>
    </thead>
    <tbody>{''.join(rows) or '<tr><td colspan="8">No clients yet.</td></tr>'}</tbody>
  </table>
</body>
</html>"""

    def do_GET(self) -> None:
        parsed = parse.urlparse(self.path)
        if parsed.path in ("", "/"):
            self.redirect("/admin")
            return
        con = connect_server(self.db_path)
        try:
            if parsed.path == "/admin":
                query = parse.parse_qs(parsed.query)
                message = query.get("message", [None])[0]
                self.send_html(200, self.render_admin(con, message=message))
                return
            if parsed.path == "/api/v1/stats":
                if not require_admin(self.app_config, self.headers):
                    self.send_json(401, {"error": "admin authorization required"})
                    return
                self.send_json(200, server_stats_dict(con))
                return
            if parsed.path == "/api/v1/reports/usage":
                if not require_admin(self.app_config, self.headers):
                    self.send_json(401, {"error": "admin authorization required"})
                    return
                query = parse.parse_qs(parsed.query)
                args = argparse.Namespace(
                    group_by=query.get("group_by", ["day-model-client"])[0],
                    since=query.get("since", [None])[0],
                    until=query.get("until", [None])[0],
                    model=query.get("model", [None])[0],
                    session_id=query.get("session_id", [None])[0],
                    client_name=query.get("client_name", [None])[0],
                    limit=int(query.get("limit", ["100"])[0]),
                )
                rows = [dict(row) for row in server_report_rows(con, args)]
                self.send_json(200, {"rows": rows})
                return
            self.send_json(404, {"error": "not found"})
        finally:
            con.close()

    def do_POST(self) -> None:
        parsed = parse.urlparse(self.path)
        con = connect_server(self.db_path)
        try:
            if parsed.path == "/api/v1/usage-events":
                payload = self.read_json()
                client_name = str(payload.get("client_name") or "")
                if not authenticate_client(con, client_name, bearer_token(self.headers)):
                    self.send_json(401, {"error": "invalid client token"})
                    return
                events = payload.get("events")
                if not isinstance(events, list):
                    self.send_json(400, {"error": "events must be a list"})
                    return
                accepted, duplicates = ingest_usage_events(con, client_name, events)
                con.commit()
                self.send_json(200, {"accepted": accepted, "duplicates": duplicates})
                return
            if parsed.path == "/admin/clients/create":
                form = self.read_form()
                client_name = form.get("client_name", "").strip()
                display_name = form.get("display_name", "").strip() or client_name
                if not client_name:
                    self.send_html(400, self.render_admin(con, message="Client name is required."))
                    return
                try:
                    token = create_client_token(con, client_name, display_name)
                    con.commit()
                except sqlite3.IntegrityError:
                    self.send_html(409, self.render_admin(con, message=f"Client {client_name} already exists."))
                    return
                self.send_html(200, self.render_admin(con, message=f"Created {client_name}.", token=token))
                return
            if parsed.path == "/admin/clients/rename":
                form = self.read_form()
                rename_client(con, form.get("client_name", ""), form.get("display_name", "").strip())
                con.commit()
                self.redirect("/admin?message=Client%20renamed")
                return
            if parsed.path == "/admin/clients/revoke":
                form = self.read_form()
                revoke_client(con, form.get("client_name", ""))
                con.commit()
                self.redirect("/admin?message=Client%20revoked")
                return
            self.send_json(404, {"error": "not found"})
        finally:
            con.close()

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))


def serve(args: argparse.Namespace) -> None:
    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.allow_remote:
        print(
            "Refusing to bind outside loopback without --allow-remote. "
            "OTEL payloads can contain sensitive local telemetry.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    Receiver.db_path = Path(args.db)
    Receiver.app_config = load_config(Path(args.config))
    connect(Receiver.db_path).close()
    server = ThreadingHTTPServer((args.host, args.port), Receiver)
    print(f"Listening on http://{args.host}:{args.port}; db={Receiver.db_path}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping receiver.", file=sys.stderr, flush=True)
        raise SystemExit(130) from None
    finally:
        server.server_close()


def server_serve(args: argparse.Namespace) -> None:
    config = load_config(Path(args.config))
    host = args.host or config.central.host
    port = args.port or config.central.port
    db_path = Path(args.server_db or config.central.db)
    if host not in ("127.0.0.1", "localhost", "::1") and not args.allow_remote:
        print(
            "Refusing to bind outside loopback without --allow-remote. "
            "The admin UI has no login in the MVP.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    ServerReceiver.db_path = db_path
    ServerReceiver.app_config = config
    connect_server(db_path).close()
    server = ThreadingHTTPServer((host, port), ServerReceiver)
    print(f"Server listening on http://{host}:{port}; db={db_path}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.", file=sys.stderr, flush=True)
        raise SystemExit(130) from None
    finally:
        server.server_close()


def summary(args: argparse.Namespace) -> None:
    con = connect(Path(args.db))
    try:
        rows = usage_report_rows(con, args)
        if not rows:
            raw_count = con.execute("select count(*) from raw_payloads").fetchone()[0]
            print(f"No token usage events extracted yet. Raw OTEL payloads stored: {raw_count}")
            return
        if args.format == "json":
            print(json.dumps([{column: row[column] for column in REPORT_COLUMNS} for row in rows], indent=2))
        elif args.format == "csv":
            write_csv(rows, sys.stdout)
        else:
            print_table(rows, default_columns(args.group_by))
    finally:
        con.close()


def raw(args: argparse.Namespace) -> None:
    con = connect(Path(args.db))
    where, params = raw_where_clause(args)
    try:
        rows = con.execute(
            f"""
            select id, received_at, path, content_type, length(body) as bytes
            from raw_payloads
            {where}
            order by id desc
            limit ?
            """,
            (*params, args.limit),
        ).fetchall()
        if args.format == "json":
            print(json.dumps([dict(row) for row in rows], indent=2))
        else:
            print_table(rows, ("id", "received_at", "path", "content_type", "bytes"))
    finally:
        con.close()


def dump_raw(args: argparse.Namespace) -> None:
    con = connect(Path(args.db))
    try:
        row = con.execute("select body, content_type from raw_payloads where id = ?", (args.id,)).fetchone()
        if not row:
            print(f"No raw payload found with id {args.id}", file=sys.stderr)
            raise SystemExit(1)
        body = bytes(row["body"])
        content_type = row["content_type"] or ""
        if "json" in content_type:
            try:
                print(json.dumps(json.loads(body.decode("utf-8")), indent=2))
                return
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
        sys.stdout.buffer.write(body)
    finally:
        con.close()


def stats(args: argparse.Namespace) -> None:
    con = connect(Path(args.db))
    try:
        raw_count = con.execute("select count(*) from raw_payloads").fetchone()[0]
        event_count = con.execute("select count(*) from usage_events").fetchone()[0]
        totals = con.execute(
            """
            select
                coalesce(sum(input_tokens), 0) as input_tokens,
                coalesce(sum(output_tokens), 0) as output_tokens,
                coalesce(sum(total_tokens), 0) as total_tokens,
                coalesce(sum(cached_tokens), 0) as cached_tokens,
                coalesce(sum(reasoning_tokens), 0) as reasoning_tokens,
                min(received_at) as first_seen,
                max(received_at) as last_seen
            from usage_events
            """
        ).fetchone()
        print(f"db: {Path(args.db)}")
        print(f"raw_payloads: {raw_count}")
        print(f"usage_events: {event_count}")
        for key in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_tokens",
            "reasoning_tokens",
            "first_seen",
            "last_seen",
        ):
            print(f"{key}: {totals[key]}")
    finally:
        con.close()


def reindex_database(con: sqlite3.Connection, config: AppConfig, *, keep_existing: bool = False) -> tuple[int, int]:
    if not keep_existing:
        con.execute("delete from usage_events")
    rows = con.execute(
        """
        select id, received_at, path, content_type, body
        from raw_payloads
        order by id
        """
    ).fetchall()
    inserted = 0
    for row in rows:
        content_type = row["content_type"] or ""
        body = bytes(row["body"])
        if "json" not in content_type or not body:
            continue
        try:
            events = extract_usage(row["path"], body, config)
        except Exception as exc:
            sys.stderr.write(f"parse error for raw payload {row['id']}: {exc}\n")
            continue
        insert_usage(con, row["id"], row["received_at"], events)
        inserted += len(events)
    return len(rows), inserted


def event_from_stored_row(row: sqlite3.Row, config: AppConfig) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    try:
        loaded_attrs = json.loads(row["attributes_json"])
        if isinstance(loaded_attrs, dict):
            attrs = loaded_attrs
    except json.JSONDecodeError:
        attrs = {}
    return {
        "signal": row["signal"],
        "event_name": row["event_name"],
        "model": row["model"] if config.storage.model else None,
        "session_id": row["session_id"] if config.storage.session_id else None,
        "thread_id": row["thread_id"] if config.storage.thread_id else None,
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
        "total_tokens": row["total_tokens"],
        "cached_tokens": row["cached_tokens"],
        "reasoning_tokens": row["reasoning_tokens"],
        "attributes_json": stored_attributes_json(attrs, config),
    }


def usage_events_without_reindexable_raw(
    con: sqlite3.Connection,
    config: AppConfig,
) -> list[tuple[int, str, dict[str, Any]]]:
    rows = con.execute(
        """
        select usage_events.*
        from usage_events
        join raw_payloads on raw_payloads.id = usage_events.raw_payload_id
        where length(raw_payloads.body) = 0 or coalesce(raw_payloads.content_type, '') not like '%json%'
        order by usage_events.id
        """
    ).fetchall()
    return [(row["raw_payload_id"], row["received_at"], event_from_stored_row(row, config)) for row in rows]


def cleanup_stored_data(con: sqlite3.Connection, config: AppConfig) -> int:
    cleared_payloads = 0
    if not config.storage.raw_payload_body:
        cleared_payloads = con.execute("select count(*) from raw_payloads where length(body) > 0").fetchone()[0]
        con.execute("update raw_payloads set body = X'' where length(body) > 0")
    return int(cleared_payloads)


def usage_event_to_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "client_event_id": row["client_event_id"],
        "received_at": row["received_at"],
        "signal": row["signal"],
        "event_name": row["event_name"],
        "model": row["model"],
        "session_id": row["session_id"],
        "thread_id": row["thread_id"],
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
        "total_tokens": row["total_tokens"],
        "cached_tokens": row["cached_tokens"],
        "reasoning_tokens": row["reasoning_tokens"],
        "attributes_json": row["attributes_json"],
    }


def pending_sync_rows(con: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return con.execute(
        """
        select *
        from usage_events
        where synced_at is null
        order by id
        limit ?
        """,
        (limit,),
    ).fetchall()


def post_usage_batch(config: AppConfig, rows: Sequence[sqlite3.Row]) -> dict[str, Any]:
    if not config.server.endpoint or not config.server.api_key:
        raise ValueError("server.endpoint and server.api_key are required for sync")
    url = config.server.endpoint.rstrip("/") + "/api/v1/usage-events"
    payload = {
        "client_name": config.client_name,
        "sent_at": now_iso(),
        "events": [usage_event_to_payload(row) for row in rows],
    }
    req = request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "authorization": f"Bearer {config.server.api_key}",
            "content-type": "application/json",
        },
        method="POST",
    )
    with request.urlopen(req, timeout=config.server.timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def sync_pending_usage(con: sqlite3.Connection, config: AppConfig, *, limit: int | None = None) -> tuple[int, int, str | None]:
    batch_limit = limit or config.server.batch_size
    rows = pending_sync_rows(con, batch_limit)
    if not rows:
        return 0, 0, None
    ids = [row["id"] for row in rows]
    try:
        result = post_usage_batch(config, rows)
    except (OSError, ValueError, urlerror.URLError, urlerror.HTTPError) as exc:
        message = str(exc)
        con.executemany(
            """
            update usage_events
            set sync_attempts = coalesce(sync_attempts, 0) + 1,
                last_sync_error = ?
            where id = ?
            """,
            [(message, row_id) for row_id in ids],
        )
        return len(rows), 0, message
    synced_at = now_iso()
    con.executemany(
        """
        update usage_events
        set synced_at = ?,
            last_sync_error = null
        where id = ?
        """,
        [(synced_at, row_id) for row_id in ids],
    )
    return len(rows), int(result.get("accepted", len(rows))) + int(result.get("duplicates", 0)), None


def sync(args: argparse.Namespace) -> None:
    config = load_config(Path(args.config))
    con = connect(Path(args.db))
    try:
        attempted, synced, error = sync_pending_usage(con, config, limit=args.limit)
        con.commit()
    finally:
        con.close()
    if error:
        print(f"Sync failed for {attempted} events: {error}", file=sys.stderr)
        raise SystemExit(1)
    print(f"Synced {synced} usage events.")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token() -> str:
    return "ait_" + secrets.token_urlsafe(32)


def create_client_token(con: sqlite3.Connection, client_name: str, display_name: str) -> str:
    token = generate_token()
    now = now_iso()
    con.execute(
        """
        insert into clients(client_name, display_name, token_hash, created_at, updated_at)
        values (?, ?, ?, ?, ?)
        """,
        (client_name, display_name or client_name, hash_token(token), now, now),
    )
    return token


def rename_client(con: sqlite3.Connection, client_name: str, display_name: str) -> None:
    con.execute(
        "update clients set display_name = ?, updated_at = ? where client_name = ?",
        (display_name, now_iso(), client_name),
    )


def revoke_client(con: sqlite3.Connection, client_name: str) -> None:
    now = now_iso()
    con.execute(
        "update clients set revoked_at = coalesce(revoked_at, ?), updated_at = ? where client_name = ?",
        (now, now, client_name),
    )


def bearer_token(headers: Any) -> str | None:
    value = headers.get("authorization") or headers.get("Authorization")
    if not value or not value.lower().startswith("bearer "):
        return None
    return value.split(" ", 1)[1].strip()


def authenticate_client(con: sqlite3.Connection, client_name: str, token: str | None) -> bool:
    if not token:
        return False
    row = con.execute(
        "select token_hash, revoked_at from clients where client_name = ?",
        (client_name,),
    ).fetchone()
    if not row or row["revoked_at"]:
        return False
    return secrets.compare_digest(row["token_hash"], hash_token(token))


def require_admin(config: AppConfig, headers: Any) -> bool:
    if not config.central.admin_api_key:
        return False
    return secrets.compare_digest(bearer_token(headers) or "", config.central.admin_api_key)


def ingest_usage_events(con: sqlite3.Connection, client_name: str, events: Sequence[dict[str, Any]]) -> tuple[int, int]:
    accepted = 0
    duplicates = 0
    received_at = now_iso()
    for event in events:
        try:
            con.execute(
                """
                insert into usage_events(
                    client_name, client_event_id, received_at, source_received_at, signal, event_name,
                    model, session_id, thread_id, input_tokens, output_tokens, total_tokens,
                    cached_tokens, reasoning_tokens, attributes_json
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    client_name,
                    str(event["client_event_id"]),
                    received_at,
                    str(event.get("received_at") or received_at),
                    str(event.get("signal") or "logs"),
                    event.get("event_name"),
                    event.get("model"),
                    event.get("session_id"),
                    event.get("thread_id"),
                    int(event.get("input_tokens") or 0),
                    int(event.get("output_tokens") or 0),
                    int(event.get("total_tokens") or 0),
                    int(event.get("cached_tokens") or 0),
                    int(event.get("reasoning_tokens") or 0),
                    str(event.get("attributes_json") or "{}"),
                ),
            )
            accepted += 1
        except sqlite3.IntegrityError:
            duplicates += 1
    con.execute("update clients set last_seen_at = ?, updated_at = ? where client_name = ?", (received_at, received_at, client_name))
    return accepted, duplicates


def server_where_clause(args: argparse.Namespace) -> tuple[str, list[Any]]:
    clauses = []
    params: list[Any] = []
    since = parse_datetime_filter(getattr(args, "since", None))
    until = parse_datetime_filter(getattr(args, "until", None), end_of_day=True)
    if since:
        clauses.append("source_received_at >= ?")
        params.append(since)
    if until:
        clauses.append("source_received_at <= ?")
        params.append(until)
    if getattr(args, "model", None):
        clauses.append("model = ?")
        params.append(args.model)
    if getattr(args, "session_id", None):
        clauses.append("session_id = ?")
        params.append(args.session_id)
    if getattr(args, "client_name", None):
        clauses.append("client_name = ?")
        params.append(args.client_name)
    return ("where " + " and ".join(clauses), params) if clauses else ("", params)


def server_group_expressions(group_by: str) -> tuple[str, str, str, str]:
    period_expr = "''"
    client_expr = "''"
    model_expr = "''"
    session_expr = "''"
    if group_by in ("day", "day-model", "day-session", "day-client", "day-model-client"):
        period_expr = "substr(source_received_at, 1, 10)"
    if group_by in ("client", "day-client", "day-model-client"):
        client_expr = "coalesce(client_name, '(unknown)')"
    if group_by in ("model", "day-model", "day-model-client"):
        model_expr = "coalesce(model, '(unknown)')"
    if group_by in ("session", "day-session"):
        session_expr = "coalesce(session_id, '(unknown)')"
    return period_expr, client_expr, model_expr, session_expr


def server_report_rows(con: sqlite3.Connection, args: argparse.Namespace) -> list[sqlite3.Row]:
    period_expr, client_expr, model_expr, session_expr = server_group_expressions(args.group_by)
    where, params = server_where_clause(args)
    order_by = "last_seen desc"
    if args.group_by.startswith("day"):
        order_by = "period desc, total_tokens desc"
    elif args.group_by in ("model", "session", "client"):
        order_by = "total_tokens desc"
    query = f"""
        select
            {period_expr} as period,
            {client_expr} as client_name,
            {model_expr} as model,
            {session_expr} as session_id,
            count(*) as events,
            coalesce(sum(input_tokens), 0) as input_tokens,
            coalesce(sum(output_tokens), 0) as output_tokens,
            coalesce(sum(total_tokens), 0) as total_tokens,
            coalesce(sum(cached_tokens), 0) as cached_tokens,
            coalesce(sum(reasoning_tokens), 0) as reasoning_tokens,
            min(source_received_at) as first_seen,
            max(source_received_at) as last_seen
        from usage_events
        {where}
        group by 1, 2, 3, 4
        order by {order_by}
        limit ?
    """
    return con.execute(query, (*params, args.limit)).fetchall()


def server_stats_dict(con: sqlite3.Connection) -> dict[str, Any]:
    totals = con.execute(
        """
        select
            count(*) as usage_events,
            count(distinct client_name) as active_clients,
            coalesce(sum(input_tokens), 0) as input_tokens,
            coalesce(sum(output_tokens), 0) as output_tokens,
            coalesce(sum(total_tokens), 0) as total_tokens,
            coalesce(sum(cached_tokens), 0) as cached_tokens,
            coalesce(sum(reasoning_tokens), 0) as reasoning_tokens,
            min(source_received_at) as first_seen,
            max(source_received_at) as last_seen
        from usage_events
        """
    ).fetchone()
    clients = con.execute("select count(*) from clients where revoked_at is null").fetchone()[0]
    out = dict(totals)
    out["configured_clients"] = clients
    return out


def reindex(args: argparse.Namespace) -> None:
    con = connect(Path(args.db))
    config = load_config(Path(args.config))
    try:
        raw_count, inserted = reindex_database(con, config, keep_existing=args.keep_existing)
        con.commit()
        print(f"Reindexed {raw_count} raw payloads; inserted {inserted} usage events.")
    finally:
        con.close()


def cleanup(args: argparse.Namespace) -> None:
    con = connect(Path(args.db))
    config = load_config(Path(args.config))
    try:
        preserved_events = usage_events_without_reindexable_raw(con, config)
        raw_count, inserted = reindex_database(con, config)
        for raw_id, received_at, event in preserved_events:
            insert_usage(con, raw_id, received_at, [event])
        cleared_payloads = cleanup_stored_data(con, config)
        con.commit()
        print(
            f"Reindexed {raw_count} raw payloads; inserted {inserted} usage events; "
            f"preserved {len(preserved_events)} existing usage events without raw bodies."
        )
        if config.storage.raw_payload_body:
            print("Raw payload bodies kept because storage.raw_payload_body is true.")
        else:
            print(f"Cleared stored bodies from {cleared_payloads} raw payloads.")
    finally:
        con.close()


def samples(args: argparse.Namespace) -> None:
    con = connect(Path(args.db))
    try:
        rows = con.execute(
            """
            select id, received_at, signal, event_name, model, input_tokens, output_tokens,
                   total_tokens, substr(attributes_json, 1, 1000) as attributes
            from usage_events
            order by id desc
            limit ?
            """,
            (args.limit,),
        ).fetchall()
    finally:
        con.close()
    for row in rows:
        print(json.dumps(dict(row), indent=2))


def add_report_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--since", help="UTC timestamp or YYYY-MM-DD lower bound")
    parser.add_argument("--until", help="UTC timestamp or YYYY-MM-DD upper bound")
    parser.add_argument("--model", help="Only include one model")
    parser.add_argument("--session-id", help="Only include one session")
    parser.add_argument("--limit", type=int, default=100)


def add_report_output(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", choices=("table", "csv", "json"), default="table")


def add_raw_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--since", help="UTC timestamp or YYYY-MM-DD lower bound")
    parser.add_argument("--until", help="UTC timestamp or YYYY-MM-DD upper bound")
    parser.add_argument("--limit", type=int, default=100)


def legacy_summary(args: argparse.Namespace) -> None:
    args.group_by = "model"
    args.format = "table"
    args.since = None
    args.until = None
    args.model = None
    args.session_id = None
    args.limit = 100
    summary(args)


def report(args: argparse.Namespace) -> None:
    summary(args)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    db_parent = argparse.ArgumentParser(add_help=False)
    db_parent.add_argument("--db", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    db_parent.add_argument("--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", parents=[db_parent])
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=4318)
    p_serve.add_argument("--allow-remote", action="store_true", help="Allow binding to a non-loopback host")
    p_serve.set_defaults(func=serve)

    p_summary = sub.add_parser("summary", parents=[db_parent], help="Usage totals by model")
    p_summary.set_defaults(func=legacy_summary)

    p_report = sub.add_parser("report", parents=[db_parent], help="Grouped usage report")
    p_report.add_argument(
        "--group-by",
        choices=("total", "day", "model", "session", "day-model", "day-session"),
        default="day-model",
    )
    add_report_filters(p_report)
    add_report_output(p_report)
    p_report.set_defaults(func=report)

    p_raw = sub.add_parser("raw", parents=[db_parent], help="List stored raw OTEL payloads")
    add_raw_filters(p_raw)
    add_report_output(p_raw)
    p_raw.set_defaults(func=raw)

    p_dump_raw = sub.add_parser("dump-raw", parents=[db_parent], help="Print one raw OTEL payload by id")
    p_dump_raw.add_argument("id", type=int)
    p_dump_raw.set_defaults(func=dump_raw)

    p_stats = sub.add_parser("stats", parents=[db_parent], help="Database-level counters")
    p_stats.set_defaults(func=stats)

    p_reindex = sub.add_parser("reindex", parents=[db_parent], help="Rebuild usage events from stored raw payloads")
    p_reindex.add_argument("--keep-existing", action="store_true")
    p_reindex.set_defaults(func=reindex)

    p_cleanup = sub.add_parser("cleanup", parents=[db_parent], help="Apply current storage config to existing data")
    p_cleanup.set_defaults(func=cleanup)

    p_samples = sub.add_parser("samples", parents=[db_parent])
    p_samples.add_argument("--limit", type=int, default=10)
    p_samples.set_defaults(func=samples)

    p_sync = sub.add_parser("sync", parents=[db_parent], help="Forward queued client usage events to the central server")
    p_sync.add_argument("--limit", type=int)
    p_sync.set_defaults(func=sync)

    p_client = sub.add_parser("client", help="Client-side commands")
    client_sub = p_client.add_subparsers(dest="client_cmd", required=True)

    p_client_serve = client_sub.add_parser("serve", parents=[db_parent])
    p_client_serve.add_argument("--host", default="127.0.0.1")
    p_client_serve.add_argument("--port", type=int, default=4318)
    p_client_serve.add_argument("--allow-remote", action="store_true", help="Allow binding to a non-loopback host")
    p_client_serve.set_defaults(func=serve)

    p_client_sync = client_sub.add_parser("sync", parents=[db_parent])
    p_client_sync.add_argument("--limit", type=int)
    p_client_sync.set_defaults(func=sync)

    p_server = sub.add_parser("server", help="Central server commands")
    server_sub = p_server.add_subparsers(dest="server_cmd", required=True)
    p_server_serve = server_sub.add_parser("serve", parents=[db_parent])
    p_server_serve.add_argument("--host")
    p_server_serve.add_argument("--port", type=int)
    p_server_serve.add_argument("--server-db")
    p_server_serve.add_argument("--allow-remote", action="store_true", help="Allow binding to a non-loopback host")
    p_server_serve.set_defaults(func=server_serve)

    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
