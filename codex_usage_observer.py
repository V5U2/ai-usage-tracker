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
import json
import os
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable, Sequence, TextIO

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback.
    tomllib = None  # type: ignore[assignment]

DEFAULT_DB = Path(os.environ.get("CODEX_USAGE_DB", "codex_usage.sqlite"))
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
class AppConfig:
    storage: StorageConfig = field(default_factory=StorageConfig)
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
    redaction_data = data.get("redaction", {})
    if not isinstance(storage_data, dict):
        raise ValueError("storage must be a table/object")
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
    return AppConfig(
        storage=storage,
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
        con.execute(
            """
            insert into usage_events(
                raw_payload_id, received_at, signal, event_name, model, session_id, thread_id,
                input_tokens, output_tokens, total_tokens, cached_tokens, reasoning_tokens, attributes_json
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            return
        body = self.rfile.read(length)
        content_type = self.headers.get("content-type")
        received_at = now_iso()

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
        finally:
            con.close()

        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

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
    server.serve_forever()


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


def reindex(args: argparse.Namespace) -> None:
    con = connect(Path(args.db))
    config = load_config(Path(args.config))
    try:
        if not args.keep_existing:
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
            if "json" not in content_type:
                continue
            try:
                events = extract_usage(row["path"], bytes(row["body"]), config)
            except Exception as exc:
                sys.stderr.write(f"parse error for raw payload {row['id']}: {exc}\n")
                continue
            insert_usage(con, row["id"], row["received_at"], events)
            inserted += len(events)
        con.commit()
        print(f"Reindexed {len(rows)} raw payloads; inserted {inserted} usage events.")
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

    p_samples = sub.add_parser("samples", parents=[db_parent])
    p_samples.add_argument("--limit", type=int, default=10)
    p_samples.set_defaults(func=samples)

    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
