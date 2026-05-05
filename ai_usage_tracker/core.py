#!/usr/bin/env python3
"""
Local OTLP/HTTP receiver for AI usage observability.

It stores every received payload in SQLite and extracts token-like numeric
attributes from OTEL logs, traces, and metrics into a simple usage table.
Use provider-native OTEL JSON or Broadcast payloads for best results.
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

APP_VERSION = "0.4.0"


def env_value(name: str, default: str) -> str:
    return os.environ.get(name) or default


DEFAULT_DB = Path(env_value("AI_USAGE_DB", "ai_usage.sqlite"))
DEFAULT_SERVER_DB = Path(env_value("AI_USAGE_SERVER_DB", "ai_usage_server.sqlite"))
DEFAULT_CONFIG = Path(env_value("AI_USAGE_CONFIG", "ai_usage_tracker.toml"))
DEFAULT_MAX_BODY_BYTES = int(env_value("AI_USAGE_MAX_BODY_BYTES", str(50 * 1024 * 1024)))
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
        "cache_read_tokens",
        "cache_creation_tokens",
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
TOOL_REPORT_COLUMNS = (
    "period",
    "tool_name",
    "session_id",
    "event_name",
    "tool_events",
    "successes",
    "failures",
    "total_duration_ms",
    "avg_duration_ms",
    "first_seen",
    "last_seen",
)
DISPLAY_NAMES = {
    "period": "day",
    "source_provider": "provider",
    "source_label": "source",
    "client_name": "collector",
    "model": "model",
    "session_id": "session",
    "events": "events",
    "input_tokens": "input",
    "output_tokens": "output",
    "total_tokens": "total",
    "cached_tokens": "cached",
    "reasoning_tokens": "reason",
    "tool_name": "tool",
    "tool_events": "events",
    "successes": "ok",
    "failures": "fail",
    "total_duration_ms": "duration_ms",
    "avg_duration_ms": "avg_ms",
    "duration_ms": "duration_ms",
    "decision": "decision",
    "source": "source",
    "success": "success",
    "call_id": "call",
    "mcp_server": "mcp",
    "source_received_at": "time",
    "first_seen": "first",
    "last_seen": "last",
    "table_name": "table",
    "total_rows": "total",
    "synced_rows": "synced",
    "pending_rows": "pending",
    "error_rows": "errors",
    "newest_local": "newest_local",
    "newest_synced": "newest_synced",
    "last_synced_at": "last_sync",
    "source_kind": "source_kind",
    "workspace_label": "workspace",
    "api_key_label": "api_key",
    "provider_name": "model_provider",
    "cost_value": "cost",
    "cost_unit": "unit",
    "trace_id": "trace",
    "span_id": "span",
    "replay_status": "status",
    "replayed_at": "replayed",
    "last_error": "last_error",
    "payloads": "payloads",
    "accepted": "accepted",
    "duplicates": "duplicates",
    "errors": "errors",
}
NUMERIC_COLUMNS = {
    "events",
    "tool_events",
    "successes",
    "failures",
    "total_duration_ms",
    "avg_duration_ms",
    "duration_ms",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_tokens",
    "reasoning_tokens",
    "total_rows",
    "synced_rows",
    "pending_rows",
    "error_rows",
    "payloads",
    "accepted",
    "duplicates",
    "errors",
}
COST_COLUMNS = {"cost_value"}
TIME_COLUMNS = {"first_seen", "last_seen", "source_received_at", "newest_local", "newest_synced", "last_synced_at", "replayed_at"}
HTML_TIME_COLUMNS = TIME_COLUMNS | {"created_at", "updated_at", "revoked_at"}
TOOL_EVENT_NAMES = {
    "codex.tool_decision",
    "codex.tool_result",
    "claude_code.tool_decision",
    "claude_code.tool_result",
    "tool_decision",
    "tool_result",
}
VERBOSE_TOOL_ATTRS = {"arguments", "output"}
OPENROUTER_BROADCAST_CLIENT = "openrouter-broadcast"
OPENROUTER_SOURCE_KIND = "openrouter_broadcast"
OPENAI_API_MODEL_PRICES_USD_PER_1M: dict[str, tuple[float, float, float]] = {
    # model/prefix: (input, cached input, output)
    "gpt-5.5": (5.00, 0.50, 30.00),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
    "gpt-5.4-nano": (0.20, 0.02, 1.25),
    "gpt-5.4": (2.50, 0.25, 15.00),
    "gpt-5.3-codex": (1.75, 0.175, 14.00),
    "gpt-5.3-chat": (1.75, 0.175, 14.00),
    "gpt-5.3": (1.75, 0.175, 14.00),
    "gpt-5.2-codex": (1.75, 0.175, 14.00),
    "gpt-5.1-codex-mini": (0.25, 0.025, 2.00),
    "gpt-5.1-codex-max": (1.25, 0.125, 10.00),
    "gpt-5.1-codex": (1.25, 0.125, 10.00),
    "gpt-5-codex": (1.25, 0.125, 10.00),
    "gpt-5-chat": (1.25, 0.125, 10.00),
    "gpt-5-nano": (0.05, 0.005, 0.40),
    "gpt-5": (1.25, 0.125, 10.00),
}
CLAUDE_API_MODEL_PRICES_USD_PER_1M: dict[str, tuple[float, float, float, float, float]] = {
    # model/prefix: (input, 5m cache write, 1h cache write, cache hit/read, output)
    "claude-opus-4.7": (5.00, 6.25, 10.00, 0.50, 25.00),
    "claude-opus-4.6": (5.00, 6.25, 10.00, 0.50, 25.00),
    "claude-opus-4.5": (5.00, 6.25, 10.00, 0.50, 25.00),
    "claude-opus-4.1": (15.00, 18.75, 30.00, 1.50, 75.00),
    "claude-opus-4": (15.00, 18.75, 30.00, 1.50, 75.00),
    "claude-sonnet-4.6": (3.00, 3.75, 6.00, 0.30, 15.00),
    "claude-sonnet-4.5": (3.00, 3.75, 6.00, 0.30, 15.00),
    "claude-sonnet-4": (3.00, 3.75, 6.00, 0.30, 15.00),
    "claude-3-7-sonnet": (3.00, 3.75, 6.00, 0.30, 15.00),
    "claude-3.7-sonnet": (3.00, 3.75, 6.00, 0.30, 15.00),
    "claude-haiku-4.5": (1.00, 1.25, 2.00, 0.10, 5.00),
    "claude-3-5-haiku": (0.80, 1.00, 1.60, 0.08, 4.00),
    "claude-3.5-haiku": (0.80, 1.00, 1.60, 0.08, 4.00),
    "claude-3-opus": (15.00, 18.75, 30.00, 1.50, 75.00),
    "claude-3-haiku": (0.25, 0.30, 0.50, 0.03, 1.25),
}
COST_UNIT_REPORT_GROUPS = {
    "client",
    "client-model",
    "day-client",
    "day-model-client",
    "source",
    "workspace",
    "api-key",
    "provider",
    "provider-source",
    "provider-source-model",
    "model-provider",
}


@dataclass(frozen=True)
class StorageConfig:
    raw_payload_body: bool = False
    extracted_attributes: str = "redacted"
    model: bool = True
    session_id: bool = True
    thread_id: bool = True
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES


@dataclass(frozen=True)
class RemoteServerConfig:
    endpoint: str | None = None
    api_key: str | None = None
    cloudflare_access_client_id: str | None = None
    cloudflare_access_client_secret: str | None = None
    batch_size: int = 100
    timeout_seconds: int = 10


@dataclass(frozen=True)
class ServerConfig:
    admin_api_key: str | None = None
    host: str = "127.0.0.1"
    port: int = 8318
    db: str = str(DEFAULT_SERVER_DB)


@dataclass(frozen=True)
class OpenRouterBroadcastConfig:
    enabled: bool = False
    api_key: str | None = None
    required_header_name: str | None = None
    required_header_value: str | None = None
    retain_payload_body: bool = True


@dataclass(frozen=True)
class PricingConfig:
    estimate_openai_api_costs: bool = False
    estimate_claude_api_costs: bool = False
    include_reasoning_tokens_as_output: bool = True


@dataclass(frozen=True)
class AppConfig:
    client_name: str = "local"
    storage: StorageConfig = field(default_factory=StorageConfig)
    server: RemoteServerConfig = field(default_factory=RemoteServerConfig)
    central: ServerConfig = field(default_factory=ServerConfig)
    openrouter_broadcast: OpenRouterBroadcastConfig = field(default_factory=OpenRouterBroadcastConfig)
    pricing: PricingConfig = field(default_factory=PricingConfig)
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


def table_config(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be a table/object")
    return value


def merged_table_config(data: dict[str, Any], legacy_key: str, key: str) -> dict[str, Any]:
    legacy = table_config(data, legacy_key)
    current = table_config(data, key)
    return {**legacy, **current}


def strip_toml_comment(line: str) -> str:
    in_string = False
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\" and in_string:
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if char == "#" and not in_string:
            return line[:index].strip()
    return line.strip()


def parse_basic_toml_value(value: str) -> Any:
    value = value.strip()
    if value in ("true", "false"):
        return value == "true"
    if value.startswith('"') and value.endswith('"'):
        return json.loads(value)
    if value.startswith("[") and value.endswith("]"):
        body = value[1:-1].strip()
        if not body:
            return []
        return [parse_basic_toml_value(item.strip()) for item in body.split(",") if item.strip()]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"unsupported TOML value: {value}") from exc


def load_basic_toml(text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    current = data
    pending_key: str | None = None
    pending_values: list[str] = []

    for raw_line in text.splitlines():
        line = strip_toml_comment(raw_line)
        if not line:
            continue

        if pending_key is not None:
            pending_values.append(line)
            if line.endswith("]"):
                current[pending_key] = parse_basic_toml_value(" ".join(pending_values))
                pending_key = None
                pending_values = []
            continue

        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            if not section:
                raise ValueError("TOML section name must be non-empty")
            current = data.setdefault(section, {})
            if not isinstance(current, dict):
                raise ValueError(f"TOML section conflicts with existing key: {section}")
            continue

        if "=" not in line:
            raise ValueError(f"unsupported TOML line: {raw_line.strip()}")
        key, value = (part.strip() for part in line.split("=", 1))
        if not key:
            raise ValueError("TOML key must be non-empty")
        if value.startswith("[") and not value.endswith("]"):
            pending_key = key
            pending_values = [value]
            continue
        current[key] = parse_basic_toml_value(value)

    if pending_key is not None:
        raise ValueError(f"unterminated TOML array for {pending_key}")
    return data


def load_config(path: Path | None) -> AppConfig:
    if path is None or not path.exists():
        return DEFAULT_APP_CONFIG
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        text = path.read_text(encoding="utf-8")
        data = tomllib.loads(text) if tomllib is not None else load_basic_toml(text)
    if not isinstance(data, dict):
        raise ValueError("config root must be a table/object")

    storage_data = table_config(data, "storage")
    collector_data = merged_table_config(data, "server", "collector")
    aggregation_data = merged_table_config(data, "central_server", "aggregation_server")
    openrouter_data = table_config(data, "openrouter_broadcast")
    pricing_data = table_config(data, "pricing")
    redaction_data = table_config(data, "redaction")

    extracted_attributes = storage_data.get("extracted_attributes", "redacted")
    if extracted_attributes not in ("redacted", "full", "none"):
        raise ValueError('storage.extracted_attributes must be "redacted", "full", or "none"')

    storage = StorageConfig(
        raw_payload_body=bool_config(storage_data, "raw_payload_body", False),
        extracted_attributes=str(extracted_attributes),
        model=bool_config(storage_data, "model", True),
        session_id=bool_config(storage_data, "session_id", True),
        thread_id=bool_config(storage_data, "thread_id", True),
        max_body_bytes=int_config(storage_data, "max_body_bytes", DEFAULT_MAX_BODY_BYTES),
    )
    remote_server = RemoteServerConfig(
        endpoint=optional_str_config(collector_data, "endpoint"),
        api_key=optional_str_config(collector_data, "api_key"),
        cloudflare_access_client_id=optional_str_config(collector_data, "cloudflare_access_client_id"),
        cloudflare_access_client_secret=optional_str_config(collector_data, "cloudflare_access_client_secret"),
        batch_size=int_config(collector_data, "batch_size", 100),
        timeout_seconds=int_config(collector_data, "timeout_seconds", 10),
    )
    central = ServerConfig(
        admin_api_key=optional_str_config(aggregation_data, "admin_api_key"),
        host=str_config(aggregation_data, "host", "127.0.0.1"),
        port=int_config(aggregation_data, "port", 8318),
        db=str_config(aggregation_data, "db", str(DEFAULT_SERVER_DB)),
    )
    openrouter_broadcast = OpenRouterBroadcastConfig(
        enabled=bool_config(openrouter_data, "enabled", False),
        api_key=optional_str_config(openrouter_data, "api_key"),
        required_header_name=optional_str_config(openrouter_data, "required_header_name"),
        required_header_value=optional_str_config(openrouter_data, "required_header_value"),
        retain_payload_body=bool_config(openrouter_data, "retain_payload_body", True),
    )
    pricing = PricingConfig(
        estimate_openai_api_costs=bool_config(pricing_data, "estimate_openai_api_costs", False),
        estimate_claude_api_costs=bool_config(pricing_data, "estimate_claude_api_costs", False),
        include_reasoning_tokens_as_output=bool_config(pricing_data, "include_reasoning_tokens_as_output", True),
    )
    return AppConfig(
        client_name=str_config(data, "client_name", "local"),
        storage=storage,
        server=remote_server,
        central=central,
        openrouter_broadcast=openrouter_broadcast,
        pricing=pricing,
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
    con.execute(
        """
        create table if not exists tool_events (
            id integer primary key autoincrement,
            raw_payload_id integer not null,
            received_at text not null,
            signal text not null,
            event_name text not null,
            model text,
            session_id text,
            thread_id text,
            tool_name text not null,
            call_id text,
            decision text,
            source text,
            success text,
            duration_ms integer,
            mcp_server text,
            attributes_json text not null,
            foreign key(raw_payload_id) references raw_payloads(id)
        )
        """
    )
    con.execute("create index if not exists idx_raw_payloads_received_at on raw_payloads(received_at)")
    con.execute("create index if not exists idx_usage_events_received_at on usage_events(received_at)")
    con.execute("create index if not exists idx_usage_events_model on usage_events(model)")
    con.execute("create index if not exists idx_usage_events_session on usage_events(session_id)")
    con.execute("create index if not exists idx_tool_events_received_at on tool_events(received_at)")
    con.execute("create index if not exists idx_tool_events_tool_name on tool_events(tool_name)")
    con.execute("create index if not exists idx_tool_events_session on tool_events(session_id)")
    ensure_client_sync_schema(con)
    con.commit()
    return con


def table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in con.execute(f"pragma table_info({table})")}


def add_column_if_missing(con: sqlite3.Connection, table: str, columns: set[str], definition: str) -> None:
    name = definition.split()[0]
    if name not in columns:
        con.execute(f"alter table {table} add column {definition}")
        columns.add(name)


def ensure_usage_metadata_schema(con: sqlite3.Connection) -> None:
    columns = table_columns(con, "usage_events")
    for definition in (
        "source_kind text",
        "trace_id text",
        "span_id text",
        "workspace_label text",
        "api_key_label text",
        "provider_name text",
        "cost_value real default 0",
        "cost_unit text",
    ):
        add_column_if_missing(con, "usage_events", columns, definition)


def ensure_client_sync_schema(con: sqlite3.Connection) -> None:
    ensure_usage_metadata_schema(con)
    columns = table_columns(con, "usage_events")
    if "client_event_id" not in columns:
        con.execute("alter table usage_events add column client_event_id text")
    if "synced_at" not in columns:
        con.execute("alter table usage_events add column synced_at text")
    if "sync_attempts" not in columns:
        con.execute("alter table usage_events add column sync_attempts integer default 0")
    if "last_sync_error" not in columns:
        con.execute("alter table usage_events add column last_sync_error text")
    if "synced_server_key" not in columns:
        con.execute("alter table usage_events add column synced_server_key text")
    con.execute(
        """
        update usage_events
        set client_event_id = printf('local-%d', id)
        where client_event_id is null or client_event_id = ''
        """
    )
    con.execute("create unique index if not exists idx_usage_events_client_event_id on usage_events(client_event_id)")

    tool_columns = table_columns(con, "tool_events")
    if "client_tool_event_id" not in tool_columns:
        con.execute("alter table tool_events add column client_tool_event_id text")
    if "synced_at" not in tool_columns:
        con.execute("alter table tool_events add column synced_at text")
    if "sync_attempts" not in tool_columns:
        con.execute("alter table tool_events add column sync_attempts integer default 0")
    if "last_sync_error" not in tool_columns:
        con.execute("alter table tool_events add column last_sync_error text")
    if "synced_server_key" not in tool_columns:
        con.execute("alter table tool_events add column synced_server_key text")
    con.execute(
        """
        update tool_events
        set client_tool_event_id = printf('tool-local-%d', id)
        where client_tool_event_id is null or client_tool_event_id = ''
        """
    )
    con.execute("create unique index if not exists idx_tool_events_client_tool_event_id on tool_events(client_tool_event_id)")


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
            source_kind text,
            trace_id text,
            span_id text,
            workspace_label text,
            api_key_label text,
            provider_name text,
            cost_value real default 0,
            cost_unit text,
            attributes_json text not null,
            unique(client_name, client_event_id)
        )
        """
    )
    ensure_usage_metadata_schema(con)
    con.execute(
        """
        create table if not exists broadcast_payloads (
            id integer primary key autoincrement,
            received_at text not null,
            path text not null,
            content_type text,
            body blob not null,
            replay_status text not null default 'received',
            replayed_at text,
            last_error text
        )
        """
    )
    con.execute(
        """
        create table if not exists tool_events (
            id integer primary key autoincrement,
            client_name text not null,
            client_tool_event_id text not null,
            received_at text not null,
            source_received_at text not null,
            signal text not null,
            event_name text not null,
            model text,
            session_id text,
            thread_id text,
            tool_name text not null,
            call_id text,
            decision text,
            source text,
            success text,
            duration_ms integer,
            mcp_server text,
            attributes_json text not null,
            unique(client_name, client_tool_event_id)
        )
        """
    )
    con.execute("create index if not exists idx_server_usage_received_at on usage_events(source_received_at)")
    con.execute("create index if not exists idx_server_usage_client on usage_events(client_name)")
    con.execute("create index if not exists idx_server_usage_model on usage_events(model)")
    con.execute("create index if not exists idx_server_usage_source_kind on usage_events(source_kind)")
    con.execute("create index if not exists idx_server_usage_workspace on usage_events(workspace_label)")
    con.execute("create index if not exists idx_server_usage_api_key on usage_events(api_key_label)")
    con.execute("create index if not exists idx_server_usage_provider on usage_events(provider_name)")
    con.execute("create index if not exists idx_broadcast_payloads_received_at on broadcast_payloads(received_at)")
    con.execute("create index if not exists idx_broadcast_payloads_replay_status on broadcast_payloads(replay_status)")
    con.execute("create index if not exists idx_server_tool_received_at on tool_events(source_received_at)")
    con.execute("create index if not exists idx_server_tool_client on tool_events(client_name)")
    con.execute("create index if not exists idx_server_tool_tool_name on tool_events(tool_name)")
    con.commit()
    return con


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def unix_nano_to_iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        nanos = int(value)
    except (TypeError, ValueError):
        return None
    seconds, remainder = divmod(nanos, 1_000_000_000)
    timestamp = dt.datetime.fromtimestamp(seconds, dt.timezone.utc)
    if remainder:
        timestamp = timestamp.replace(microsecond=remainder // 1000)
    return timestamp.isoformat(timespec="microseconds" if timestamp.microsecond else "seconds")


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


def optional_int_attr(attrs: dict[str, Any], aliases: tuple[str, ...]) -> int | None:
    for alias in aliases:
        value = attrs.get(alias)
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def float_attr(attrs: dict[str, Any], aliases: tuple[str, ...]) -> tuple[float, str | None]:
    for alias in aliases:
        value = attrs.get(alias)
        if value in (None, ""):
            continue
        try:
            return float(value), alias
        except (TypeError, ValueError):
            continue
    return 0.0, None


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


def stored_tool_attributes_json(attrs: dict[str, Any], config: AppConfig) -> str:
    compact_attrs = {key: value for key, value in attrs.items() if key not in VERBOSE_TOOL_ATTRS}
    return stored_attributes_json(compact_attrs, config)


def normalize_model_name(model: str | None) -> str:
    return (model or "").strip().lower()


def model_name_variants(model: str | None) -> tuple[str, ...]:
    normalized = normalize_model_name(model)
    if not normalized:
        return ()
    variants = [normalized]
    dotted_parts = normalized.split("-")
    for index in range(len(dotted_parts) - 1):
        if dotted_parts[index].isdigit() and dotted_parts[index + 1].isdigit():
            variants.append(
                "-".join(
                    (
                        *dotted_parts[:index],
                        f"{dotted_parts[index]}.{dotted_parts[index + 1]}",
                        *dotted_parts[index + 2 :],
                    )
                )
            )
    return tuple(dict.fromkeys(variants))


def openai_api_price_for_model(model: str | None) -> tuple[float, float, float] | None:
    variants = model_name_variants(model)
    if not variants:
        return None
    for normalized in variants:
        if normalized in OPENAI_API_MODEL_PRICES_USD_PER_1M:
            return OPENAI_API_MODEL_PRICES_USD_PER_1M[normalized]
    prices = sorted(OPENAI_API_MODEL_PRICES_USD_PER_1M.items(), key=lambda item: len(item[0]), reverse=True)
    for normalized in variants:
        for prefix, price in prices:
            if normalized.startswith(prefix + "-") or normalized.startswith(prefix + "."):
                return price
    return None


def claude_api_price_for_model(model: str | None) -> tuple[float, float, float, float, float] | None:
    variants = model_name_variants(model)
    if not variants:
        return None
    for normalized in variants:
        if normalized in CLAUDE_API_MODEL_PRICES_USD_PER_1M:
            return CLAUDE_API_MODEL_PRICES_USD_PER_1M[normalized]
    prices = sorted(CLAUDE_API_MODEL_PRICES_USD_PER_1M.items(), key=lambda item: len(item[0]), reverse=True)
    for normalized in variants:
        for prefix, price in prices:
            if normalized.startswith(prefix + "-") or normalized.startswith(prefix + "."):
                return price
    return None


def estimate_openai_api_cost(
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
    reasoning_tokens: int,
    config: AppConfig,
) -> float:
    price = openai_api_price_for_model(model)
    if not price:
        return 0.0
    input_rate, cached_input_rate, output_rate = price
    cached_billable = min(max(cached_tokens, 0), max(input_tokens, 0))
    uncached_input = max(input_tokens - cached_billable, 0)
    output_billable = max(output_tokens, 0)
    if config.pricing.include_reasoning_tokens_as_output:
        output_billable += max(reasoning_tokens, 0)
    return (
        uncached_input * input_rate
        + cached_billable * cached_input_rate
        + output_billable * output_rate
    ) / 1_000_000


def estimate_claude_api_cost(
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
) -> float:
    price = claude_api_price_for_model(model)
    if not price:
        return 0.0
    input_rate, cache_write_5m_rate, _cache_write_1h_rate, cache_read_rate, output_rate = price
    cache_read_billable = max(cache_read_tokens, 0)
    cache_write_billable = max(cache_creation_tokens, 0)
    cache_adjusted_tokens = cache_read_billable + cache_write_billable
    uncached_input = max(input_tokens - cache_adjusted_tokens, 0)
    return (
        uncached_input * input_rate
        + cache_write_billable * cache_write_5m_rate
        + cache_read_billable * cache_read_rate
        + max(output_tokens, 0) * output_rate
    ) / 1_000_000


def has_token_signal(attrs: dict[str, Any]) -> bool:
    keys = set(attrs)
    return any(any(alias in keys for alias in aliases) for aliases in TOKEN_KEYS.values())


def normalize_claude_code_metric_attrs(name: str | None, attrs: dict[str, Any]) -> dict[str, Any]:
    if name == "claude_code.token.usage":
        value = attrs.get(name)
        token_type = attrs.get("type")
        if token_type == "input":
            attrs["input_tokens"] = value
        elif token_type == "output":
            attrs["output_tokens"] = value
        elif token_type == "cacheRead":
            attrs["cache_read_tokens"] = value
        elif token_type == "cacheCreation":
            attrs["cache_creation_tokens"] = value
    elif name == "claude_code.cost.usage":
        attrs["cost_usd"] = attrs.get(name)
        attrs["cost_unit"] = "USD"
    return attrs


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
    cache_read_tokens = int_attr(attrs, ("cache_read_tokens", "cache_read_input_tokens", "usage.cache_read_input_tokens"))
    cache_creation_tokens = int_attr(
        attrs,
        ("cache_creation_tokens", "cache_creation_input_tokens", "usage.cache_creation_input_tokens"),
    )
    reasoning_tokens = int_attr(attrs, TOKEN_KEYS["reasoning"])
    cost_value, cost_alias = float_attr(
        attrs,
        (
            "gen_ai.usage.cost_usd",
            "usage.cost_usd",
            "cost_usd",
            "cost",
            "usage.cost",
            "gen_ai.usage.total_cost",
            "gen_ai.usage.cost",
            "openrouter.cost",
            "openrouter.credits",
        ),
    )
    cost_unit = first_attr(
        attrs,
        ("cost_unit", "cost.currency", "gen_ai.usage.cost_unit", "gen_ai.usage.cost_currency", "openrouter.cost_unit"),
    )
    if cost_alias is not None and not cost_unit:
        if cost_alias in ("gen_ai.usage.cost_usd", "usage.cost_usd", "cost_usd"):
            cost_unit = "USD"
        else:
            cost_unit = "credits"
    if not any((input_tokens, output_tokens, total_tokens, cached_tokens, reasoning_tokens)):
        if not has_token_signal(attrs) and cost_alias is None:
            return None

    if total_tokens == 0 and (input_tokens or output_tokens):
        total_tokens = input_tokens + output_tokens

    raw_model = first_attr(attrs, ("model", "model_name", "gen_ai.request.model", "gen_ai.response.model"))
    model = raw_model if config.storage.model else None
    if config.pricing.estimate_openai_api_costs and cost_alias is None:
        estimated_cost = estimate_openai_api_cost(
            raw_model,
            input_tokens,
            output_tokens,
            cached_tokens,
            reasoning_tokens,
            config,
        )
        if estimated_cost:
            cost_value = estimated_cost
            cost_unit = "USD"
    if config.pricing.estimate_claude_api_costs and cost_alias is None:
        estimated_cost = estimate_claude_api_cost(
            raw_model,
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_creation_tokens,
        )
        if estimated_cost:
            cost_value = estimated_cost
            cost_unit = "USD"

    return {
        "signal": signal,
        "event_name": event_name,
        "model": model,
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
        "source_kind": first_attr(attrs, ("source_kind", "source.kind", "service.name")),
        "trace_id": first_attr(attrs, ("trace_id", "traceId")),
        "span_id": first_attr(attrs, ("span_id", "spanId")),
        "workspace_label": first_attr(
            attrs,
            (
                "workspace_label",
                "workspace.id",
                "workspace.name",
                "openrouter.workspace",
                "openrouter.workspace_id",
                "trace.metadata.openrouter.entity_id",
                "trace.metadata.workspace",
                "trace.metadata.workspace_id",
                "trace.metadata.workspace_name",
            ),
        ),
        "api_key_label": first_attr(
            attrs,
            (
                "api_key_label",
                "openrouter.api_key_id",
                "openrouter.api_key_label",
                "trace.metadata.openrouter.api_key_name",
                "trace.metadata.api_key_label",
                "trace.metadata.api_key_id",
            ),
        ),
        "provider_name": first_attr(
            attrs,
            (
                "provider_name",
                "trace.metadata.openrouter.provider_name",
                "trace.metadata.openrouter.provider_slug",
                "gen_ai.provider.name",
                "openrouter.provider_name",
                "provider",
                "gen_ai.system",
                "gen_ai.response.provider",
                "gen_ai.request.provider",
                "openrouter.provider",
            ),
        ),
        "cost_value": cost_value,
        "cost_unit": cost_unit,
        "attributes_json": stored_attributes_json(attrs, config),
    }


def tool_event_from_attrs(
    signal: str,
    event_name: str | None,
    attrs: dict[str, Any],
    config: AppConfig = DEFAULT_APP_CONFIG,
) -> dict[str, Any] | None:
    attrs = flatten_attrs(attrs)
    event_name = event_name or first_attr(attrs, ("event.name", "name"))
    if event_name not in TOOL_EVENT_NAMES:
        return None
    tool_name = first_attr(attrs, ("tool_name", "tool.name", "gen_ai.tool.name"))
    if not tool_name:
        return None
    return {
        "signal": signal,
        "event_name": event_name,
        "model": first_attr(attrs, ("model", "model_name", "gen_ai.request.model", "gen_ai.response.model"))
        if config.storage.model
        else None,
        "session_id": first_attr(attrs, ("session_id", "session.id", "codex.session_id", "conversation.id"))
        if config.storage.session_id
        else None,
        "thread_id": first_attr(attrs, ("thread_id", "thread.id", "codex.thread_id"))
        if config.storage.thread_id
        else None,
        "tool_name": tool_name,
        "call_id": first_attr(attrs, ("call_id", "tool_call_id", "tool_use_id", "gen_ai.tool.call.id")),
        "decision": first_attr(attrs, ("decision", "decision_type")),
        "source": first_attr(attrs, ("source", "decision_source")),
        "success": first_attr(attrs, ("success",)),
        "duration_ms": optional_int_attr(attrs, ("duration_ms", "duration")),
        "mcp_server": first_attr(attrs, ("mcp_server", "mcp_server_scope", "server_name")),
        "attributes_json": stored_tool_attributes_json(attrs, config),
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
                attrs = merge_attrs(
                    resource_attrs,
                    scope_attrs,
                    attrs_to_dict(span.get("attributes")),
                    {
                        "traceId": span.get("traceId"),
                        "spanId": span.get("spanId"),
                        "startTimeUnixNano": span.get("startTimeUnixNano"),
                        "endTimeUnixNano": span.get("endTimeUnixNano"),
                    },
                )
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
                    attrs = normalize_claude_code_metric_attrs(name, attrs)
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


def extract_tool_events(path: str, body: bytes, config: AppConfig = DEFAULT_APP_CONFIG) -> list[dict[str, Any]]:
    payload = json.loads(body.decode("utf-8"))
    if path.endswith("/v1/logs"):
        events = (tool_event_from_attrs("logs", name, attrs, config) for name, attrs in iter_log_records(payload))
    elif path.endswith("/v1/traces"):
        events = (tool_event_from_attrs("traces", name, attrs, config) for name, attrs in iter_spans(payload))
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
                input_tokens, output_tokens, total_tokens, cached_tokens, reasoning_tokens,
                source_kind, trace_id, span_id, workspace_label, api_key_label, provider_name,
                cost_value, cost_unit, attributes_json, client_event_id
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                event.get("source_kind"),
                event.get("trace_id"),
                event.get("span_id"),
                event.get("workspace_label"),
                event.get("api_key_label"),
                event.get("provider_name"),
                float(event.get("cost_value") or 0),
                event.get("cost_unit"),
                event["attributes_json"],
                client_event_id,
            ),
        )


def insert_tool_events(con: sqlite3.Connection, raw_id: int, received_at: str, events: list[dict[str, Any]]) -> None:
    for event in events:
        client_tool_event_id = event.get("client_tool_event_id") or f"tool_{secrets.token_urlsafe(18)}"
        con.execute(
            """
            insert into tool_events(
                raw_payload_id, received_at, signal, event_name, model, session_id, thread_id,
                tool_name, call_id, decision, source, success, duration_ms, mcp_server, attributes_json,
                client_tool_event_id
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                raw_id,
                received_at,
                event["signal"],
                event["event_name"],
                event["model"],
                event["session_id"],
                event["thread_id"],
                event["tool_name"],
                event["call_id"],
                event["decision"],
                event["source"],
                event["success"],
                event["duration_ms"],
                event["mcp_server"],
                event["attributes_json"],
                client_tool_event_id,
            ),
        )


def parse_datetime_filter(value: str | None, *, end_of_day: bool = False) -> str | None:
    if not value:
        return None
    if len(value) == 10:
        if end_of_day:
            return dt.datetime.fromisoformat(f"{value}T23:59:59.999999+00:00").isoformat(timespec="microseconds")
        return dt.datetime.fromisoformat(f"{value}T00:00:00+00:00").isoformat(timespec="seconds")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    parsed = parsed.astimezone(dt.timezone.utc)
    return parsed.isoformat(timespec="microseconds" if parsed.microsecond else "seconds")


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


def tool_group_expression(group_by: str) -> tuple[str, str, str, str]:
    period_expr = "''"
    tool_expr = "''"
    session_expr = "''"
    event_expr = "''"
    if group_by in ("day", "day-tool", "day-session"):
        period_expr = "substr(received_at, 1, 10)"
    if group_by in ("tool", "day-tool"):
        tool_expr = "coalesce(tool_name, '(unknown)')"
    if group_by in ("session", "day-session"):
        session_expr = "coalesce(session_id, '(unknown)')"
    if group_by == "event":
        event_expr = "event_name"
    return period_expr, tool_expr, session_expr, event_expr


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


def tool_where_clause(args: argparse.Namespace) -> tuple[str, list[Any]]:
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
    if getattr(args, "tool_name", None):
        clauses.append("tool_name = ?")
        params.append(args.tool_name)
    if getattr(args, "session_id", None):
        clauses.append("session_id = ?")
        params.append(args.session_id)
    if getattr(args, "event_name", None):
        clauses.append("event_name = ?")
        params.append(args.event_name)
    return ("where " + " and ".join(clauses), params) if clauses else ("", params)


def tool_report_rows(con: sqlite3.Connection, args: argparse.Namespace) -> list[sqlite3.Row]:
    period_expr, tool_expr, session_expr, event_expr = tool_group_expression(args.group_by)
    where, params = tool_where_clause(args)
    order_by = "last_seen desc"
    if args.group_by.startswith("day"):
        order_by = "period desc, tool_events desc"
    elif args.group_by in ("tool", "session", "event"):
        order_by = "tool_events desc"
    query = f"""
        select
            {period_expr} as period,
            {tool_expr} as tool_name,
            {session_expr} as session_id,
            {event_expr} as event_name,
            count(*) as tool_events,
            coalesce(sum(case when success = 'true' then 1 else 0 end), 0) as successes,
            coalesce(sum(case when success = 'false' then 1 else 0 end), 0) as failures,
            coalesce(sum(duration_ms), 0) as total_duration_ms,
            coalesce(round(avg(duration_ms)), 0) as avg_duration_ms,
            min(received_at) as first_seen,
            max(received_at) as last_seen
        from tool_events
        {where}
        group by 1, 2, 3, 4
        order by {order_by}
        limit ?
    """
    return con.execute(query, (*params, args.limit)).fetchall()


def write_csv(rows: Sequence[sqlite3.Row], out: TextIO, columns: Sequence[str] = REPORT_COLUMNS) -> None:
    writer = csv.DictWriter(out, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row[column] for column in columns})


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
    tool_events: int,
    raw_id: int,
) -> str:
    raw_state = "kept" if raw_body_retained else "metadata-only"
    content = content_type or "-"
    return (
        f"{time.strftime('%H:%M:%S')} received {path} "
        f"payload={format_bytes(body_bytes)} shape={shape} content_type={content} "
        f"raw_body={raw_state} usage_events={events} tool_events={tool_events} raw_id={raw_id}"
    )


def format_cell(column: str, value: Any) -> str:
    if value in (None, ""):
        return ""
    if column in COST_COLUMNS:
        amount = float(value)
        return f"{amount:.6f}".rstrip("0").rstrip(".") if amount else "0"
    if column in NUMERIC_COLUMNS:
        return f"{int(value):,}"
    if column in TIME_COLUMNS:
        return format_timestamp(str(value))
    return str(value)


def server_html_cell(column: str, value: Any, *, classes: str = "") -> str:
    class_names = [name for name in classes.split() if name]
    if column in HTML_TIME_COLUMNS:
        class_names.append("browser-time")
        class_attr = f' class="{" ".join(class_names)}"' if class_names else ""
        if value in (None, ""):
            return f"<td{class_attr}></td>"
        raw = str(value)
        fallback = format_timestamp(raw)
        return (
            f'<td{class_attr} data-utc="{html.escape(raw, quote=True)}" '
            f'title="{html.escape(raw, quote=True)}">{html.escape(fallback)}</td>'
        )
    class_attr = f' class="{html.escape(classes, quote=True)}"' if classes else ""
    return f"<td{class_attr}>{html.escape(format_cell(column, value))}</td>"


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


def tool_default_columns(group_by: str) -> tuple[str, ...]:
    if group_by == "total":
        return ("tool_events", "successes", "failures", "total_duration_ms", "avg_duration_ms", "first_seen", "last_seen")
    if group_by == "day":
        return ("period", "tool_events", "successes", "failures", "total_duration_ms")
    if group_by == "tool":
        return ("tool_name", "tool_events", "successes", "failures", "total_duration_ms", "avg_duration_ms", "last_seen")
    if group_by == "session":
        return ("session_id", "tool_events", "successes", "failures", "total_duration_ms", "last_seen")
    if group_by == "day-session":
        return ("period", "session_id", "tool_events", "successes", "failures", "total_duration_ms")
    if group_by == "event":
        return ("event_name", "tool_events", "successes", "failures", "last_seen")
    return ("period", "tool_name", "tool_events", "successes", "failures", "total_duration_ms")


def server_default_columns(group_by: str) -> tuple[str, ...]:
    source_columns = {
        "source": "source_kind",
        "workspace": "workspace_label",
        "api-key": "api_key_label",
        "provider": "source_provider",
        "model-provider": "provider_name",
    }
    if group_by in source_columns:
        return (
            source_columns[group_by],
            "events",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_tokens",
            "reasoning_tokens",
            "cost_value",
            "cost_unit",
            "last_seen",
        )
    if group_by == "provider-source":
        return (
            "source_provider",
            "source_label",
            "events",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_tokens",
            "reasoning_tokens",
            "cost_value",
            "cost_unit",
            "last_seen",
        )
    if group_by == "provider-source-model":
        return (
            "source_provider",
            "source_label",
            "model",
            "events",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_tokens",
            "reasoning_tokens",
            "cost_value",
            "cost_unit",
            "last_seen",
        )
    if group_by == "client":
        return (
            "client_name",
            "events",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_tokens",
            "reasoning_tokens",
            "cost_value",
            "cost_unit",
            "last_seen",
        )
    if group_by == "client-model":
        return (
            "client_name",
            "model",
            "events",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_tokens",
            "reasoning_tokens",
            "cost_value",
            "cost_unit",
            "last_seen",
        )
    if group_by == "day-client":
        return (
            "period",
            "client_name",
            "events",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_tokens",
            "reasoning_tokens",
            "cost_value",
            "cost_unit",
        )
    if group_by == "day-model-client":
        return (
            "period",
            "client_name",
            "model",
            "events",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_tokens",
            "reasoning_tokens",
            "cost_value",
            "cost_unit",
        )
    return default_columns(group_by)


def server_tool_default_columns(group_by: str) -> tuple[str, ...]:
    if group_by == "client":
        return ("client_name", "tool_events", "successes", "failures", "total_duration_ms", "last_seen")
    if group_by == "client-tool":
        return (
            "client_name",
            "tool_name",
            "tool_events",
            "successes",
            "failures",
            "total_duration_ms",
            "avg_duration_ms",
            "last_seen",
        )
    if group_by == "day-client":
        return ("period", "client_name", "tool_events", "successes", "failures", "total_duration_ms")
    if group_by == "day-tool-client":
        return ("period", "client_name", "tool_name", "tool_events", "successes", "failures", "total_duration_ms")
    return tool_default_columns(group_by)


def print_compact_rows(rows: Sequence[sqlite3.Row], columns: Sequence[str]) -> None:
    for index, row in enumerate(rows, start=1):
        first_line = []
        second_line = []
        for column in columns:
            cell = format_cell(column, row[column])
            if not cell:
                continue
            entry = f"{DISPLAY_NAMES.get(column, column)}: {cell}"
            if column in (
                "period",
                "model",
                "tool_name",
                "session_id",
                "event_name",
                "source_kind",
                "workspace_label",
                "api_key_label",
                "provider_name",
                "events",
                "tool_events",
                "total_tokens",
                "cost_value",
                "last_seen",
            ):
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
            if column in NUMERIC_COLUMNS or column in COST_COLUMNS:
                cells.append(cell.rjust(widths[column]))
            else:
                cells.append(cell.ljust(widths[column]))
        print("  ".join(cells))


def server_nav(active: str) -> str:
    items = (
        ("usage", "/reports", "Token Usage"),
        ("tools", "/tools", "Tool Usage"),
    )
    links = []
    for key, href, label in items:
        active_attr = ' class="active"' if key == active else ""
        links.append(f'<a href="{href}"{active_attr}>{label}</a>')
    admin_attr = ' class="active"' if active == "admin" else ""
    return (
        "<nav>"
        f'<div class="nav-primary">{"".join(links)}</div>'
        '<div class="nav-actions">'
        '<button type="button" class="theme-toggle" title="Toggle dark mode" '
        'aria-label="Toggle dark mode" onclick="aitToggleTheme()">Dark</button>'
        f'<a href="/admin"{admin_attr}>Admin</a>'
        "</div>"
        "</nav>"
    )


def server_footer() -> str:
    return f'<footer class="app-footer">AI Usage Tracker v{html.escape(APP_VERSION)}</footer>'


def server_theme_script() -> str:
    return """
(function () {
  function preferredTheme() {
    try {
      var stored = localStorage.getItem("ait-theme");
      if (stored === "dark" || stored === "light") return stored;
      if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) return "dark";
    } catch (error) {}
    return "light";
  }
  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    var button = document.querySelector(".theme-toggle");
    if (button) button.textContent = theme === "dark" ? "Light" : "Dark";
  }
  window.aitToggleTheme = function () {
    var next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    try { localStorage.setItem("ait-theme", next); } catch (error) {}
    applyTheme(next);
  };
  function formatBrowserTimes() {
    if (!window.Intl || !Intl.DateTimeFormat) return;
    var formatter = new Intl.DateTimeFormat(undefined, {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      timeZoneName: "short"
    });
    document.querySelectorAll("[data-utc]").forEach(function (element) {
      var raw = element.getAttribute("data-utc");
      if (!raw) return;
      var parsed = new Date(raw);
      if (Number.isNaN(parsed.getTime())) return;
      element.textContent = formatter.format(parsed);
      element.title = raw;
    });
  }
  applyTheme(preferredTheme());
  document.addEventListener("DOMContentLoaded", function () {
    applyTheme(document.documentElement.dataset.theme || preferredTheme());
    formatBrowserTimes();
  });
}());
"""


def server_favicon_link() -> str:
    # MDI chart-bar icon: https://pictogrammers.com/library/mdi/icon/chart-bar/
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        '<path fill="#17202a" d="M5 9.2H7V19H5V9.2M10.6 5H12.6V19H10.6V5M16.2 '
        '13H18.2V19H16.2V13M3 21H21V22H3V21Z"/></svg>'
    )
    return f'<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,{parse.quote(svg)}">'


def server_page_styles(*, tools: bool = False, admin: bool = False) -> str:
    extra = ""
    if tools:
        extra += """
    .status { display: inline-block; min-width: 2.4rem; text-align: center; padding: .12rem .35rem; border: 1px solid var(--border-strong); font-size: .78rem; }
    .status.ok { background: var(--ok-bg); border-color: var(--ok-border); color: var(--ok-text); }
    .status.fail { background: var(--danger-bg); border-color: var(--danger-border); color: var(--danger-text); }
    .status.neutral { background: var(--surface-soft); color: var(--muted); }
"""
    if admin:
        extra += """
    form.inline { display: inline; }
    .token { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; background: var(--surface-soft); padding: .5rem; }
    .message { border: 1px solid var(--border-strong); padding: .6rem; background: var(--surface); }
"""
    return f"""
    :root {{ color-scheme: light; --bg: #fbfcfd; --text: #17202a; --muted: #5b6773; --surface: #ffffff; --surface-soft: #f3f6f8; --border: #d7dde3; --border-strong: #cfd7df; --active-bg: #17202a; --active-text: #ffffff; --danger-bg: #fdecec; --danger-border: #efb1b1; --danger-text: #9f1d1d; --ok-bg: #eaf7ee; --ok-border: #afd9bb; --ok-text: #1d6b38; }}
    html[data-theme="dark"] {{ color-scheme: dark; --bg: #111418; --text: #edf2f7; --muted: #a8b3bf; --surface: #181d23; --surface-soft: #202731; --border: #303946; --border-strong: #465160; --active-bg: #edf2f7; --active-text: #111418; --danger-bg: #3a1f24; --danger-border: #7d3941; --danger-text: #ffb8c1; --ok-bg: #173321; --ok-border: #315f40; --ok-text: #a9e7b8; }}
    body {{ font-family: system-ui, sans-serif; margin: 2rem; color: var(--text); background: var(--bg); }}
    nav {{ display: flex; gap: .75rem; margin-bottom: 1.5rem; align-items: center; }}
    .nav-primary, .nav-actions {{ display: flex; gap: .5rem; align-items: center; }}
    .nav-actions {{ margin-left: auto; }}
    nav a, .theme-toggle {{ border: 1px solid var(--border-strong); color: var(--text); padding: .4rem .65rem; text-decoration: none; background: var(--surface); font: inherit; }}
    nav a.active {{ background: var(--active-bg); color: var(--active-text); border-color: var(--active-bg); }}
    .theme-toggle {{ cursor: pointer; }}
    main {{ display: grid; gap: 1.25rem; }}
    table {{ border-collapse: collapse; width: 100%; background: var(--surface); }}
    th, td {{ border-bottom: 1px solid var(--border); padding: .5rem; text-align: left; vertical-align: top; }}
    th {{ background: var(--surface-soft); font-size: .82rem; color: var(--muted); }}
    input, select {{ padding: .4rem; border: 1px solid var(--border-strong); background: var(--surface); color: var(--text); }}
    button {{ padding: .42rem .7rem; border: 1px solid var(--active-bg); background: var(--active-bg); color: var(--active-text); }}
    .filters {{ display: flex; flex-wrap: wrap; gap: .75rem; align-items: end; background: var(--surface); border: 1px solid var(--border); padding: .85rem; }}
    .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr)); gap: .75rem; }}
    .summary div {{ border: 1px solid var(--border); padding: .75rem; background: var(--surface); }}
    .panel {{ display: grid; gap: .75rem; }}
    .panel-head {{ display: flex; justify-content: space-between; gap: 1rem; align-items: baseline; }}
    .panel-head h2 {{ margin: 0; font-size: 1.05rem; }}
    .quick-links {{ display: flex; flex-wrap: wrap; gap: .5rem; }}
    .quick-links a {{ border: 1px solid var(--border-strong); padding: .3rem .5rem; color: var(--text); text-decoration: none; background: var(--surface); }}
    .label {{ color: var(--muted); font-size: .82rem; }}
    .value {{ font-size: 1.2rem; font-weight: 650; }}
    .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    label {{ display: grid; gap: .25rem; }}
    .app-footer {{ margin-top: 2rem; color: var(--muted); font-size: .8rem; }}
{extra}"""


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
            tool_events: list[dict[str, Any]] = []
            if content_type and "json" in content_type:
                try:
                    events = extract_usage(self.path, body, self.app_config)
                    tool_events = extract_tool_events(self.path, body, self.app_config)
                except Exception as exc:  # Keep raw data even when parser lags schema changes.
                    sys.stderr.write(f"parse error for {self.path}: {exc}\n")
            insert_usage(con, raw_id, received_at, events)
            insert_tool_events(con, raw_id, received_at, tool_events)
            con.commit()
            if (events or tool_events) and self.app_config.server.endpoint and self.app_config.server.api_key:
                _, _, sync_error = sync_pending_usage(con, self.app_config, limit=self.app_config.server.batch_size)
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
                tool_events=len(tool_events),
                raw_id=raw_id,
            )
            + "\n"
        )

    def log_message(self, fmt: str, *args: Any) -> None:
        return


class ServerReceiver(BaseHTTPRequestHandler):
    REPORT_GROUPS = (
        "total",
        "provider-source-model",
        "provider-source",
        "provider",
        "day",
        "model",
        "client",
        "client-model",
        "session",
        "day-model",
        "day-client",
        "day-session",
        "day-model-client",
        "source",
        "workspace",
        "api-key",
        "model-provider",
    )
    TOOL_REPORT_GROUPS = (
        "total",
        "day",
        "tool",
        "client",
        "client-tool",
        "session",
        "event",
        "day-tool",
        "day-client",
        "day-session",
        "day-tool-client",
    )

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

    @staticmethod
    def reports_args(query: dict[str, list[str]]) -> argparse.Namespace:
        group_by = query.get("group_by", ["provider-source-model"])[0]
        if group_by not in ServerReceiver.REPORT_GROUPS:
            group_by = "provider-source-model"
        try:
            limit = int(query.get("limit", ["100"])[0])
        except ValueError:
            limit = 100
        limit = min(max(limit, 1), 1000)
        return argparse.Namespace(
            group_by=group_by,
            since=query.get("since", [None])[0] or None,
            until=query.get("until", [None])[0] or None,
            model=query.get("model", [None])[0] or None,
            session_id=query.get("session_id", [None])[0] or None,
            client_name=query.get("client_name", [None])[0] or None,
            source_kind=query.get("source_kind", [None])[0] or None,
            workspace_label=query.get("workspace_label", [None])[0] or None,
            api_key_label=query.get("api_key_label", [None])[0] or None,
            provider_name=query.get("provider_name", [None])[0] or None,
            limit=limit,
        )

    @staticmethod
    def tool_reports_args(query: dict[str, list[str]]) -> argparse.Namespace:
        group_by = query.get("group_by", ["client-tool"])[0]
        if group_by not in ServerReceiver.TOOL_REPORT_GROUPS:
            group_by = "client-tool"
        try:
            limit = int(query.get("limit", ["100"])[0])
        except ValueError:
            limit = 100
        limit = min(max(limit, 1), 1000)
        return argparse.Namespace(
            group_by=group_by,
            since=query.get("since", [None])[0] or None,
            until=query.get("until", [None])[0] or None,
            tool_name=query.get("tool_name", [None])[0] or None,
            session_id=query.get("session_id", [None])[0] or None,
            client_name=query.get("client_name", [None])[0] or None,
            event_name=query.get("event_name", [""])[0],
            limit=limit,
        )

    def render_reports(self, con: sqlite3.Connection, query: dict[str, list[str]]) -> str:
        args = ServerReceiver.reports_args(query)
        stats = server_stats_dict(con)
        rows = server_report_rows(con, args)
        columns = server_default_columns(args.group_by)

        group_options = "\n".join(
            f'<option value="{html.escape(group)}"{" selected" if group == args.group_by else ""}>{html.escape(group)}</option>'
            for group in ServerReceiver.REPORT_GROUPS
        )
        table_rows = []
        for row in rows:
            cells = []
            for column in columns:
                classes = "num" if column in NUMERIC_COLUMNS or column in COST_COLUMNS else ""
                cells.append(server_html_cell(column, row[column], classes=classes))
            table_rows.append(f"<tr>{''.join(cells)}</tr>")
        headers = "".join(f"<th>{html.escape(DISPLAY_NAMES.get(column, column))}</th>" for column in columns)
        empty_row = f'<tr><td colspan="{len(columns)}">No matching usage events.</td></tr>'

        def field(name: str) -> str:
            value = getattr(args, name)
            return html.escape(str(value or ""))

        nav = server_nav("usage")
        styles = server_page_styles()
        theme_script = server_theme_script()
        favicon = server_favicon_link()
        return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>AI Usage Tracker Reports</title>
  {favicon}
  <script>{theme_script}</script>
  <style>{styles}</style>
</head>
<body>
  {nav}
	  <main>
	    <h1>Usage Reports</h1>
    <section class="summary">
      <div><div class="label">Events</div><div class="value">{format_cell("events", stats["usage_events"])}</div></div>
      <div><div class="label">Total tokens</div><div class="value">{format_cell("total_tokens", stats["total_tokens"])}</div></div>
	      <div><div class="label">Input</div><div class="value">{format_cell("input_tokens", stats["input_tokens"])}</div></div>
	      <div><div class="label">Output</div><div class="value">{format_cell("output_tokens", stats["output_tokens"])}</div></div>
	      <div><div class="label">Cost</div><div class="value">{format_cell("cost_value", stats["cost_value"])} {html.escape(str(stats.get("cost_unit") or ""))}</div></div>
	      <div><div class="label">Tool events</div><div class="value">{format_cell("tool_events", stats["tool_events"])}</div></div>
      <div><div class="label">Collectors</div><div class="value">{format_cell("events", stats["configured_clients"])}</div></div>
    </section>
    <form method="get" action="/reports" class="filters">
      <label>Group by <select name="group_by">{group_options}</select></label>
      <label>Since <input name="since" value="{field("since")}" placeholder="YYYY-MM-DD"></label>
      <label>Until <input name="until" value="{field("until")}" placeholder="YYYY-MM-DD"></label>
	      <label>Collector <input name="client_name" value="{field("client_name")}"></label>
	      <label>Model <input name="model" value="{field("model")}"></label>
	      <label>Session <input name="session_id" value="{field("session_id")}"></label>
	      <label>Source kind <input name="source_kind" value="{field("source_kind")}"></label>
	      <label>Workspace <input name="workspace_label" value="{field("workspace_label")}"></label>
	      <label>API key <input name="api_key_label" value="{field("api_key_label")}"></label>
	      <label>Model provider <input name="provider_name" value="{field("provider_name")}"></label>
      <label>Limit <input name="limit" type="number" min="1" max="1000" value="{args.limit}"></label>
      <button type="submit">Apply</button>
    </form>
    <table>
      <thead><tr>{headers}</tr></thead>
      <tbody>{''.join(table_rows) or empty_row}</tbody>
    </table>
  </main>
  {server_footer()}
</body>
</html>"""

    def render_tool_reports(self, con: sqlite3.Connection, query: dict[str, list[str]]) -> str:
        args = ServerReceiver.tool_reports_args(query)
        stats = server_stats_dict(con)
        tool_summary = server_tool_summary(con, args)
        rows = server_tool_report_rows(con, args)
        recent_rows = server_tool_recent_rows(con, args)
        columns = server_tool_default_columns(args.group_by)
        recent_columns = (
            "source_received_at",
            "client_name",
            "tool_name",
            "success",
            "duration_ms",
            "decision",
            "source",
            "mcp_server",
            "session_id",
            "call_id",
        )

        group_options = "\n".join(
            f'<option value="{html.escape(group)}"{" selected" if group == args.group_by else ""}>{html.escape(group)}</option>'
            for group in ServerReceiver.TOOL_REPORT_GROUPS
        )
        table_rows = []
        for row in rows:
            cells = []
            for column in columns:
                classes = "num" if column in NUMERIC_COLUMNS else ""
                cells.append(server_html_cell(column, row[column], classes=classes))
            table_rows.append(f"<tr>{''.join(cells)}</tr>")
        headers = "".join(f"<th>{html.escape(DISPLAY_NAMES.get(column, column))}</th>" for column in columns)
        empty_row = f'<tr><td colspan="{len(columns)}">No matching tool events.</td></tr>'
        recent_table_rows = []
        for row in recent_rows:
            cells = []
            for column in recent_columns:
                if column == "success":
                    status_class = "ok" if row[column] == "true" else "fail" if row[column] == "false" else "neutral"
                    value = "ok" if row[column] == "true" else "fail" if row[column] == "false" else ""
                    cells.append(f'<td><span class="status {status_class}">{html.escape(value)}</span></td>')
                    continue
                classes = "num" if column in NUMERIC_COLUMNS else ""
                cells.append(server_html_cell(column, row[column], classes=classes))
            recent_table_rows.append(f"<tr>{''.join(cells)}</tr>")
        recent_headers = "".join(
            f"<th>{html.escape(DISPLAY_NAMES.get(column, column))}</th>" for column in recent_columns
        )
        recent_empty_row = f'<tr><td colspan="{len(recent_columns)}">No matching tool calls.</td></tr>'

        def field(name: str) -> str:
            value = getattr(args, name)
            return html.escape(str(value or ""))

        nav = server_nav("tools")
        styles = server_page_styles(tools=True)
        theme_script = server_theme_script()
        favicon = server_favicon_link()
        return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>AI Usage Tracker Tool Reports</title>
  {favicon}
  <script>{theme_script}</script>
  <style>{styles}</style>
</head>
<body>
  {nav}
  <main>
    <header>
      <h1>Tool Reports</h1>
      <div class="quick-links">
        <a href="/tools?group_by=client-tool">By collector/tool</a>
        <a href="/tools?group_by=day-tool">By day/tool</a>
        <a href="/tools?group_by=event&event_name=">Decisions and results</a>
      </div>
    </header>
    <section class="summary">
      <div><div class="label">Matching events</div><div class="value">{format_cell("tool_events", tool_summary["tool_events"])}</div></div>
      <div><div class="label">Successes</div><div class="value">{format_cell("successes", tool_summary["successes"])}</div></div>
      <div><div class="label">Failures</div><div class="value">{format_cell("failures", tool_summary["failures"])}</div></div>
      <div><div class="label">Duration</div><div class="value">{format_cell("total_duration_ms", tool_summary["total_duration_ms"])}</div></div>
      <div><div class="label">Avg duration</div><div class="value">{format_cell("avg_duration_ms", tool_summary["avg_duration_ms"])}</div></div>
      <div><div class="label">All tool events</div><div class="value">{format_cell("tool_events", stats["tool_events"])}</div></div>
    </section>
    <form method="get" action="/tools" class="filters">
      <label>Group by <select name="group_by">{group_options}</select></label>
      <label>Since <input name="since" value="{field("since")}" placeholder="YYYY-MM-DD"></label>
      <label>Until <input name="until" value="{field("until")}" placeholder="YYYY-MM-DD"></label>
      <label>Collector <input name="client_name" value="{field("client_name")}"></label>
      <label>Tool <input name="tool_name" value="{field("tool_name")}"></label>
      <label>Event <input name="event_name" value="{field("event_name")}"></label>
      <label>Session <input name="session_id" value="{field("session_id")}"></label>
      <label>Limit <input name="limit" type="number" min="1" max="1000" value="{args.limit}"></label>
      <button type="submit">Apply</button>
    </form>
    <section class="panel">
      <div class="panel-head"><h2>Grouped totals</h2></div>
      <table>
        <thead><tr>{headers}</tr></thead>
        <tbody>{''.join(table_rows) or empty_row}</tbody>
      </table>
    </section>
    <section class="panel">
      <div class="panel-head"><h2>Recent tool calls</h2></div>
      <table>
        <thead><tr>{recent_headers}</tr></thead>
        <tbody>{''.join(recent_table_rows) or recent_empty_row}</tbody>
      </table>
    </section>
  </main>
  {server_footer()}
</body>
</html>"""

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
            delete_button = (
                f"""
                <form method="post" action="/admin/clients/delete" class="inline">
                  <input type="hidden" name="client_name" value="{client_name}">
                  <button type="submit">Delete</button>
                </form>
                """
                if client["revoked_at"]
                else ""
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
                  {server_html_cell("created_at", client["created_at"])}
                  {server_html_cell("updated_at", client["updated_at"])}
                  {server_html_cell("last_seen", client["last_seen_at"])}
                  {server_html_cell("revoked_at", client["revoked_at"])}
                  <td>{revoke_button}{delete_button}</td>
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
        nav = server_nav("admin")
        styles = server_page_styles(admin=True)
        theme_script = server_theme_script()
        favicon = server_favicon_link()
        return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>AI Usage Tracker Admin</title>
  {favicon}
  <script>{theme_script}</script>
  <style>{styles}</style>
</head>
<body>
  {nav}
  <main>
    <h1>Collector Admin</h1>
    {message_block}
    {token_block}
    <section class="panel">
      <h2>Create Collector Token</h2>
      <form method="post" action="/admin/clients/create" class="filters">
        <label>Collector name <input name="client_name" required pattern="[A-Za-z0-9_.-]+"></label>
        <label>Display name <input name="display_name"></label>
        <button type="submit">Create token</button>
      </form>
    </section>
    <section class="panel">
      <h2>Collectors</h2>
      <table>
        <thead>
          <tr><th>Collector</th><th>Display name</th><th>Status</th><th>Created</th><th>Updated</th><th>Last seen</th><th>Revoked</th><th>Actions</th></tr>
        </thead>
        <tbody>{''.join(rows) or '<tr><td colspan="8">No collectors yet.</td></tr>'}</tbody>
      </table>
    </section>
  </main>
  {server_footer()}
</body>
</html>"""

    def do_GET(self) -> None:
        parsed = parse.urlparse(self.path)
        if parsed.path in ("", "/"):
            self.redirect("/reports")
            return
        query = parse.parse_qs(parsed.query, keep_blank_values=True)
        con = connect_server(self.db_path)
        try:
            if parsed.path == "/admin":
                message = query.get("message", [None])[0]
                self.send_html(200, self.render_admin(con, message=message))
                return
            if parsed.path == "/reports":
                self.send_html(200, self.render_reports(con, query))
                return
            if parsed.path == "/tools":
                self.send_html(200, self.render_tool_reports(con, query))
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
                args = self.reports_args(query)
                rows = [dict(row) for row in server_report_rows(con, args)]
                self.send_json(200, {"rows": rows})
                return
            if parsed.path == "/api/v1/reports/tools":
                if not require_admin(self.app_config, self.headers):
                    self.send_json(401, {"error": "admin authorization required"})
                    return
                args = self.tool_reports_args(query)
                rows = [dict(row) for row in server_tool_report_rows(con, args)]
                self.send_json(200, {"rows": rows})
                return
            self.send_json(404, {"error": "not found"})
        finally:
            con.close()

    def do_POST(self) -> None:
        parsed = parse.urlparse(self.path)
        con = connect_server(self.db_path)
        try:
            if parsed.path == "/v1/traces":
                if not require_openrouter_broadcast(self.app_config, self.headers):
                    self.send_json(401, {"error": "invalid OpenRouter Broadcast token"})
                    return
                try:
                    ensure_no_openrouter_client_conflict(con)
                except ValueError as exc:
                    self.send_json(409, {"error": str(exc)})
                    return
                length = int(self.headers.get("content-length", "0"))
                if length > self.app_config.storage.max_body_bytes:
                    self.send_json(413, {"error": "payload too large"})
                    return
                body = self.rfile.read(length)
                content_type = self.headers.get("content-type")
                if not body or body.strip() in (b"{}", b'{"resourceSpans":[]}'):
                    payload_id = insert_broadcast_payload(con, parsed.path, content_type, body, self.app_config, status="ingested")
                    con.commit()
                    self.send_json(200, {"accepted": 0, "duplicates": 0, "payload_id": payload_id})
                    return
                payload_id = insert_broadcast_payload(con, parsed.path, content_type, body, self.app_config)
                try:
                    accepted, duplicates = ingest_openrouter_broadcast(con, body, self.app_config)
                    con.execute(
                        "update broadcast_payloads set replay_status = ?, replayed_at = ?, last_error = null where id = ?",
                        ("ingested", now_iso(), payload_id),
                    )
                    con.commit()
                except Exception as exc:
                    con.execute(
                        "update broadcast_payloads set replay_status = ?, replayed_at = ?, last_error = ? where id = ?",
                        ("error", now_iso(), str(exc), payload_id),
                    )
                    con.commit()
                    self.send_json(400, {"error": "invalid OpenRouter Broadcast payload", "payload_id": payload_id})
                    return
                self.send_json(200, {"accepted": accepted, "duplicates": duplicates, "payload_id": payload_id})
                return
            if parsed.path == "/api/v1/usage-events":
                payload = self.read_json()
                client_name = str(payload.get("client_name") or "")
                if client_name == OPENROUTER_BROADCAST_CLIENT:
                    self.send_json(400, {"error": f"{OPENROUTER_BROADCAST_CLIENT} is reserved"})
                    return
                if not authenticate_client(con, client_name, bearer_token(self.headers)):
                    self.send_json(401, {"error": "invalid client token"})
                    return
                events = payload.get("events")
                if not isinstance(events, list):
                    self.send_json(400, {"error": "events must be a list"})
                    return
                tool_events = payload.get("tool_events", [])
                if not isinstance(tool_events, list):
                    self.send_json(400, {"error": "tool_events must be a list"})
                    return
                accepted, duplicates = ingest_usage_events(con, client_name, events)
                accepted_tool_events, duplicate_tool_events = ingest_tool_events(con, client_name, tool_events)
                con.commit()
                self.send_json(
                    200,
                    {
                        "accepted": accepted,
                        "duplicates": duplicates,
                        "accepted_tool_events": accepted_tool_events,
                        "duplicate_tool_events": duplicate_tool_events,
                    },
                )
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
                except ValueError as exc:
                    self.send_html(400, self.render_admin(con, message=str(exc)))
                    return
                except sqlite3.IntegrityError:
                    self.send_html(409, self.render_admin(con, message=f"Client {client_name} already exists."))
                    return
                self.send_html(200, self.render_admin(con, message=f"Created {client_name}.", token=token))
                return
            if parsed.path == "/admin/clients/rename":
                form = self.read_form()
                try:
                    rename_client(con, form.get("client_name", ""), form.get("display_name", "").strip())
                    con.commit()
                except ValueError as exc:
                    self.send_html(400, self.render_admin(con, message=str(exc)))
                    return
                self.redirect("/admin?message=Client%20renamed")
                return
            if parsed.path == "/admin/clients/revoke":
                form = self.read_form()
                revoke_client(con, form.get("client_name", ""))
                con.commit()
                self.redirect("/admin?message=Client%20revoked")
                return
            if parsed.path == "/admin/clients/delete":
                form = self.read_form()
                deleted = delete_revoked_client(con, form.get("client_name", ""))
                con.commit()
                message = "Revoked%20client%20deleted" if deleted else "Only%20revoked%20clients%20can%20be%20deleted"
                self.redirect(f"/admin?message={message}")
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
    con = connect(Receiver.db_path)
    try:
        if Receiver.app_config.server.endpoint and Receiver.app_config.server.api_key:
            attempted, synced, sync_error = sync_all_pending_usage(con, Receiver.app_config)
            con.commit()
            if attempted:
                print(f"Startup sync sent {synced}/{attempted} usage events.", flush=True)
            if sync_error:
                print(f"Startup sync error: {sync_error}", file=sys.stderr, flush=True)
    finally:
        con.close()
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
    con = connect_server(db_path)
    try:
        if config.openrouter_broadcast.enabled:
            ensure_no_openrouter_client_conflict(con)
    finally:
        con.close()
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
        tool_event_count = con.execute("select count(*) from tool_events").fetchone()[0]
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
        print(f"tool_events: {tool_event_count}")
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


def reindex_database(con: sqlite3.Connection, config: AppConfig, *, keep_existing: bool = False) -> tuple[int, int, int]:
    if not keep_existing:
        con.execute("delete from usage_events")
        con.execute("delete from tool_events")
    rows = con.execute(
        """
        select id, received_at, path, content_type, body
        from raw_payloads
        order by id
        """
    ).fetchall()
    inserted = 0
    inserted_tool_events = 0
    for row in rows:
        content_type = row["content_type"] or ""
        body = bytes(row["body"])
        if "json" not in content_type or not body:
            continue
        try:
            events = extract_usage(row["path"], body, config)
            tool_events = extract_tool_events(row["path"], body, config)
        except Exception as exc:
            sys.stderr.write(f"parse error for raw payload {row['id']}: {exc}\n")
            continue
        insert_usage(con, row["id"], row["received_at"], events)
        insert_tool_events(con, row["id"], row["received_at"], tool_events)
        inserted += len(events)
        inserted_tool_events += len(tool_events)
    return len(rows), inserted, inserted_tool_events


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
        "source_kind": row["source_kind"],
        "trace_id": row["trace_id"],
        "span_id": row["span_id"],
        "workspace_label": row["workspace_label"],
        "api_key_label": row["api_key_label"],
        "provider_name": row["provider_name"],
        "cost_value": row["cost_value"],
        "cost_unit": row["cost_unit"],
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


def tool_event_from_stored_row(row: sqlite3.Row, config: AppConfig) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    try:
        loaded_attrs = json.loads(row["attributes_json"])
        if isinstance(loaded_attrs, dict):
            attrs = loaded_attrs
    except json.JSONDecodeError:
        attrs = {}
    return {
        "client_tool_event_id": row["client_tool_event_id"],
        "signal": row["signal"],
        "event_name": row["event_name"],
        "model": row["model"] if config.storage.model else None,
        "session_id": row["session_id"] if config.storage.session_id else None,
        "thread_id": row["thread_id"] if config.storage.thread_id else None,
        "tool_name": row["tool_name"],
        "call_id": row["call_id"],
        "decision": row["decision"],
        "source": row["source"],
        "success": row["success"],
        "duration_ms": row["duration_ms"],
        "mcp_server": row["mcp_server"],
        "attributes_json": stored_tool_attributes_json(attrs, config),
    }


def tool_events_without_reindexable_raw(
    con: sqlite3.Connection,
    config: AppConfig,
) -> list[tuple[int, str, dict[str, Any]]]:
    rows = con.execute(
        """
        select tool_events.*
        from tool_events
        join raw_payloads on raw_payloads.id = tool_events.raw_payload_id
        where length(raw_payloads.body) = 0 or coalesce(raw_payloads.content_type, '') not like '%json%'
        order by tool_events.id
        """
    ).fetchall()
    return [(row["raw_payload_id"], row["received_at"], tool_event_from_stored_row(row, config)) for row in rows]


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
        "source_kind": row["source_kind"],
        "trace_id": row["trace_id"],
        "span_id": row["span_id"],
        "workspace_label": row["workspace_label"],
        "api_key_label": row["api_key_label"],
        "provider_name": row["provider_name"],
        "cost_value": row["cost_value"],
        "cost_unit": row["cost_unit"],
        "attributes_json": row["attributes_json"],
    }


def tool_event_to_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "client_tool_event_id": row["client_tool_event_id"],
        "received_at": row["received_at"],
        "signal": row["signal"],
        "event_name": row["event_name"],
        "model": row["model"],
        "session_id": row["session_id"],
        "thread_id": row["thread_id"],
        "tool_name": row["tool_name"],
        "call_id": row["call_id"],
        "decision": row["decision"],
        "source": row["source"],
        "success": row["success"],
        "duration_ms": row["duration_ms"],
        "mcp_server": row["mcp_server"],
        "attributes_json": row["attributes_json"],
    }


def sync_server_key(config: AppConfig) -> str:
    if not config.server.endpoint or not config.server.api_key:
        raise ValueError("collector.endpoint and collector.api_key are required for sync")
    material = "\n".join(
        (
            config.client_name,
            config.server.endpoint.rstrip("/"),
            config.server.api_key,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def pending_sync_rows(con: sqlite3.Connection, config: AppConfig, limit: int) -> list[sqlite3.Row]:
    target_key = sync_server_key(config)
    return con.execute(
        """
        select *
        from usage_events
        where synced_at is null
           or synced_server_key is null
           or synced_server_key != ?
        order by id
        limit ?
        """,
        (target_key, limit),
    ).fetchall()


def pending_tool_sync_rows(con: sqlite3.Connection, config: AppConfig, limit: int) -> list[sqlite3.Row]:
    target_key = sync_server_key(config)
    return con.execute(
        """
        select *
        from tool_events
        where synced_at is null
           or synced_server_key is null
           or synced_server_key != ?
        order by id
        limit ?
        """,
        (target_key, limit),
    ).fetchall()


def post_usage_batch(
    config: AppConfig,
    rows: Sequence[sqlite3.Row],
    tool_rows: Sequence[sqlite3.Row] = (),
) -> dict[str, Any]:
    if not config.server.endpoint or not config.server.api_key:
        raise ValueError("collector.endpoint and collector.api_key are required for sync")
    url = config.server.endpoint.rstrip("/") + "/api/v1/usage-events"
    payload = {
        "client_name": config.client_name,
        "sent_at": now_iso(),
        "events": [usage_event_to_payload(row) for row in rows],
        "tool_events": [tool_event_to_payload(row) for row in tool_rows],
    }
    headers = {
        "authorization": f"Bearer {config.server.api_key}",
        "content-type": "application/json",
        "user-agent": "ai-usage-tracker-collector/0.1",
    }
    if config.server.cloudflare_access_client_id and config.server.cloudflare_access_client_secret:
        headers["CF-Access-Client-Id"] = config.server.cloudflare_access_client_id
        headers["CF-Access-Client-Secret"] = config.server.cloudflare_access_client_secret
    req = request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with request.urlopen(req, timeout=config.server.timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def sync_pending_usage(con: sqlite3.Connection, config: AppConfig, *, limit: int | None = None) -> tuple[int, int, str | None]:
    batch_limit = limit or config.server.batch_size
    rows = pending_sync_rows(con, config, batch_limit)
    tool_rows = pending_tool_sync_rows(con, config, batch_limit)
    if not rows and not tool_rows:
        return 0, 0, None
    ids = [row["id"] for row in rows]
    tool_ids = [row["id"] for row in tool_rows]
    try:
        result = post_usage_batch(config, rows, tool_rows)
    except (OSError, ValueError, urlerror.URLError, urlerror.HTTPError) as exc:
        message = str(exc)
        if ids:
            con.executemany(
                """
                update usage_events
                set sync_attempts = coalesce(sync_attempts, 0) + 1,
                    last_sync_error = ?
                where id = ?
                """,
                [(message, row_id) for row_id in ids],
            )
        if tool_ids:
            con.executemany(
                """
                update tool_events
                set sync_attempts = coalesce(sync_attempts, 0) + 1,
                    last_sync_error = ?
                where id = ?
                """,
                [(message, row_id) for row_id in tool_ids],
            )
        return len(rows) + len(tool_rows), 0, message
    synced_at = now_iso()
    target_key = sync_server_key(config)
    tool_acknowledged = "accepted_tool_events" in result or "duplicate_tool_events" in result
    if ids:
        con.executemany(
            """
            update usage_events
            set synced_at = ?,
                synced_server_key = ?,
                last_sync_error = null
            where id = ?
            """,
            [(synced_at, target_key, row_id) for row_id in ids],
        )
    if tool_ids and tool_acknowledged:
        con.executemany(
            """
            update tool_events
            set synced_at = ?,
                synced_server_key = ?,
                last_sync_error = null
            where id = ?
            """,
            [(synced_at, target_key, row_id) for row_id in tool_ids],
        )
    elif tool_ids:
        message = "server did not acknowledge tool_events"
        con.executemany(
            """
            update tool_events
            set sync_attempts = coalesce(sync_attempts, 0) + 1,
                last_sync_error = ?
            where id = ?
            """,
            [(message, row_id) for row_id in tool_ids],
        )
        synced = int(result.get("accepted", len(rows))) + int(result.get("duplicates", 0))
        return len(rows) + len(tool_rows), synced, message
    synced = (
        int(result.get("accepted", len(rows)))
        + int(result.get("duplicates", 0))
        + int(result.get("accepted_tool_events", len(tool_rows)))
        + int(result.get("duplicate_tool_events", 0))
    )
    return len(rows) + len(tool_rows), synced, None


def sync_all_pending_usage(con: sqlite3.Connection, config: AppConfig) -> tuple[int, int, str | None]:
    total_attempted = 0
    total_synced = 0
    while True:
        attempted, synced, error = sync_pending_usage(con, config)
        total_attempted += attempted
        total_synced += synced
        if error or attempted == 0:
            return total_attempted, total_synced, error


def sync(args: argparse.Namespace) -> None:
    config = load_config(Path(args.config))
    con = connect(Path(args.db))
    try:
        if args.limit:
            attempted, synced, error = sync_pending_usage(con, config, limit=args.limit)
        else:
            attempted, synced, error = sync_all_pending_usage(con, config)
        con.commit()
    finally:
        con.close()
    if error:
        print(f"Sync failed for {attempted} events: {error}", file=sys.stderr)
        raise SystemExit(1)
    print(f"Synced {synced} events.")


def sync_status_rows(con: sqlite3.Connection, config: AppConfig) -> list[dict[str, Any]]:
    target_key = sync_server_key(config)
    rows: list[dict[str, Any]] = []
    for table_name in ("usage_events", "tool_events"):
        row = con.execute(
            f"""
            select
                count(*) as total_rows,
                coalesce(sum(case when synced_server_key = ? then 1 else 0 end), 0) as synced_rows,
                coalesce(sum(case when synced_server_key is null or synced_server_key != ? then 1 else 0 end), 0) as pending_rows,
                coalesce(sum(case when (synced_server_key is null or synced_server_key != ?) and last_sync_error is not null then 1 else 0 end), 0) as error_rows,
                max(received_at) as newest_local,
                max(case when synced_server_key = ? then received_at else null end) as newest_synced,
                max(case when synced_server_key = ? then synced_at else null end) as last_synced_at
            from {table_name}
            """,
            (target_key, target_key, target_key, target_key, target_key),
        ).fetchone()
        rows.append({"table_name": table_name, **dict(row)})
    return rows


def sync_status(args: argparse.Namespace) -> None:
    config = load_config(Path(args.config))
    con = connect(Path(args.db))
    try:
        rows = sync_status_rows(con, config)
        if args.format == "json":
            print(json.dumps(rows, indent=2))
        else:
            print(f"endpoint: {config.server.endpoint or '(not configured)'}")
            print(f"server_key: {sync_server_key(config)[:12]}")
            print_table(
                rows,
                (
                    "table_name",
                    "total_rows",
                    "synced_rows",
                    "pending_rows",
                    "error_rows",
                    "newest_local",
                    "newest_synced",
                    "last_synced_at",
                ),
            )
        if args.errors:
            error_rows = con.execute(
                """
                select 'usage_events' as table_name, last_sync_error, count(*) as events, max(received_at) as last_seen
                from usage_events
                where (synced_server_key is null or synced_server_key != ?) and last_sync_error is not null
                group by last_sync_error
                union all
                select 'tool_events' as table_name, last_sync_error, count(*) as events, max(received_at) as last_seen
                from tool_events
                where (synced_server_key is null or synced_server_key != ?) and last_sync_error is not null
                group by last_sync_error
                order by events desc
                limit ?
                """,
                (sync_server_key(config), sync_server_key(config), args.errors),
            ).fetchall()
            if error_rows:
                print()
                print_table(error_rows, ("table_name", "events", "last_seen", "last_sync_error"))
    finally:
        con.close()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token() -> str:
    return "ait_" + secrets.token_urlsafe(32)


def validate_client_name(client_name: str) -> None:
    if client_name == OPENROUTER_BROADCAST_CLIENT:
        raise ValueError(f"{OPENROUTER_BROADCAST_CLIENT} is reserved for OpenRouter Broadcast ingestion")


def ensure_no_openrouter_client_conflict(con: sqlite3.Connection) -> None:
    row = con.execute("select 1 from clients where client_name = ?", (OPENROUTER_BROADCAST_CLIENT,)).fetchone()
    if row:
        raise ValueError(
            f"Existing collector client {OPENROUTER_BROADCAST_CLIENT} conflicts with OpenRouter Broadcast ingestion"
        )
    row = con.execute(
        """
        select 1
        from usage_events
        where client_name = ?
          and coalesce(source_kind, '') != ?
        limit 1
        """,
        (OPENROUTER_BROADCAST_CLIENT, OPENROUTER_SOURCE_KIND),
    ).fetchone()
    if row:
        raise ValueError(
            f"Existing usage rows for {OPENROUTER_BROADCAST_CLIENT} conflict with OpenRouter Broadcast ingestion"
        )


def create_client_token(con: sqlite3.Connection, client_name: str, display_name: str) -> str:
    validate_client_name(client_name)
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
    validate_client_name(client_name)
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


def delete_revoked_client(con: sqlite3.Connection, client_name: str) -> bool:
    cursor = con.execute(
        "delete from clients where client_name = ? and revoked_at is not null",
        (client_name,),
    )
    return cursor.rowcount > 0


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


def require_openrouter_broadcast(config: AppConfig, headers: Any) -> bool:
    broadcast = config.openrouter_broadcast
    if not broadcast.enabled or not broadcast.api_key:
        return False
    if not secrets.compare_digest(bearer_token(headers) or "", broadcast.api_key):
        return False
    if broadcast.required_header_name:
        if broadcast.required_header_value is None:
            return False
        value = headers.get(broadcast.required_header_name)
        if not secrets.compare_digest(value or "", broadcast.required_header_value):
            return False
    return True


def insert_broadcast_payload(
    con: sqlite3.Connection,
    path: str,
    content_type: str | None,
    body: bytes,
    config: AppConfig,
    *,
    status: str = "received",
    last_error: str | None = None,
) -> int:
    stored_body = body if config.openrouter_broadcast.retain_payload_body else b""
    cur = con.execute(
        """
        insert into broadcast_payloads(received_at, path, content_type, body, replay_status, last_error)
        values (?, ?, ?, ?, ?, ?)
        """,
        (now_iso(), path, content_type, stored_body, status, last_error),
    )
    return int(cur.lastrowid)


def normalize_openrouter_broadcast(body: bytes, config: AppConfig) -> list[dict[str, Any]]:
    payload = json.loads(body.decode("utf-8"))
    events: list[dict[str, Any]] = []
    for name, attrs in iter_spans(payload):
        event = usage_from_attrs("traces", name, attrs, config)
        if not event:
            continue
        trace_id = event.get("trace_id")
        span_id = event.get("span_id")
        if not trace_id or not span_id:
            continue
        event["source_kind"] = OPENROUTER_SOURCE_KIND
        event["client_event_id"] = f"orb:{trace_id}:{span_id}"
        event["received_at"] = unix_nano_to_iso(attrs.get("startTimeUnixNano")) or unix_nano_to_iso(
            attrs.get("endTimeUnixNano")
        )
        events.append(event)
    return events


def ingest_openrouter_broadcast(
    con: sqlite3.Connection,
    body: bytes,
    config: AppConfig,
    *,
    update_existing: bool = False,
) -> tuple[int, int]:
    return ingest_usage_events(
        con,
        OPENROUTER_BROADCAST_CLIENT,
        normalize_openrouter_broadcast(body, config),
        update_existing=update_existing,
    )


def replay_broadcast_payloads(
    con: sqlite3.Connection,
    config: AppConfig,
    *,
    payload_id: int | None = None,
    since: str | None = None,
    until: str | None = None,
    replay_status: str | None = None,
    limit: int | None = None,
) -> dict[str, int]:
    ensure_no_openrouter_client_conflict(con)
    clauses: list[str] = []
    params: list[Any] = []
    since = parse_datetime_filter(since)
    until = parse_datetime_filter(until, end_of_day=True)
    if payload_id is not None:
        clauses.append("id = ?")
        params.append(payload_id)
    if since:
        clauses.append("received_at >= ?")
        params.append(since)
    if until:
        clauses.append("received_at <= ?")
        params.append(until)
    if replay_status:
        clauses.append("replay_status = ?")
        params.append(replay_status)
    where = "where " + " and ".join(clauses) if clauses else ""
    query = f"""
        select id, body
        from broadcast_payloads
        {where}
        order by id
        {f"limit {int(limit)}" if limit else ""}
    """
    rows = con.execute(query, params).fetchall()
    payloads = 0
    accepted = 0
    duplicates = 0
    errors = 0
    for row in rows:
        payloads += 1
        body = bytes(row["body"])
        if not body:
            errors += 1
            con.execute(
                "update broadcast_payloads set replay_status = ?, replayed_at = ?, last_error = ? where id = ?",
                ("error", now_iso(), "retained payload body is empty", row["id"]),
            )
            continue
        try:
            row_accepted, row_duplicates = ingest_openrouter_broadcast(con, body, config, update_existing=True)
            accepted += row_accepted
            duplicates += row_duplicates
            con.execute(
                "update broadcast_payloads set replay_status = ?, replayed_at = ?, last_error = null where id = ?",
                ("replayed", now_iso(), row["id"]),
            )
        except Exception as exc:
            errors += 1
            con.execute(
                "update broadcast_payloads set replay_status = ?, replayed_at = ?, last_error = ? where id = ?",
                ("error", now_iso(), str(exc), row["id"]),
            )
    return {"payloads": payloads, "accepted": accepted, "duplicates": duplicates, "errors": errors}


def ingest_usage_events(
    con: sqlite3.Connection,
    client_name: str,
    events: Sequence[dict[str, Any]],
    *,
    update_existing: bool = False,
) -> tuple[int, int]:
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
                    cached_tokens, reasoning_tokens, source_kind, trace_id, span_id, workspace_label,
                    api_key_label, provider_name, cost_value, cost_unit, attributes_json
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    event.get("source_kind"),
                    event.get("trace_id"),
                    event.get("span_id"),
                    event.get("workspace_label"),
                    event.get("api_key_label"),
                    event.get("provider_name"),
                    float(event.get("cost_value") or 0),
                    event.get("cost_unit"),
                    str(event.get("attributes_json") or "{}"),
                ),
            )
            accepted += 1
        except sqlite3.IntegrityError:
            if update_existing:
                con.execute(
                    """
                    update usage_events
                    set source_received_at = ?,
                        signal = ?,
                        event_name = ?,
                        model = ?,
                        session_id = ?,
                        thread_id = ?,
                        input_tokens = ?,
                        output_tokens = ?,
                        total_tokens = ?,
                        cached_tokens = ?,
                        reasoning_tokens = ?,
                        source_kind = ?,
                        trace_id = ?,
                        span_id = ?,
                        workspace_label = ?,
                        api_key_label = ?,
                        provider_name = ?,
                        cost_value = ?,
                        cost_unit = ?,
                        attributes_json = ?
                    where client_name = ? and client_event_id = ?
                    """,
                    (
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
                        event.get("source_kind"),
                        event.get("trace_id"),
                        event.get("span_id"),
                        event.get("workspace_label"),
                        event.get("api_key_label"),
                        event.get("provider_name"),
                        float(event.get("cost_value") or 0),
                        event.get("cost_unit"),
                        str(event.get("attributes_json") or "{}"),
                        client_name,
                        str(event["client_event_id"]),
                    ),
                )
            duplicates += 1
    con.execute("update clients set last_seen_at = ?, updated_at = ? where client_name = ?", (received_at, received_at, client_name))
    return accepted, duplicates


def ingest_tool_events(con: sqlite3.Connection, client_name: str, events: Sequence[dict[str, Any]]) -> tuple[int, int]:
    accepted = 0
    duplicates = 0
    received_at = now_iso()
    for event in events:
        try:
            con.execute(
                """
                insert into tool_events(
                    client_name, client_tool_event_id, received_at, source_received_at, signal, event_name,
                    model, session_id, thread_id, tool_name, call_id, decision, source, success,
                    duration_ms, mcp_server, attributes_json
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    client_name,
                    str(event["client_tool_event_id"]),
                    received_at,
                    str(event.get("received_at") or received_at),
                    str(event.get("signal") or "logs"),
                    str(event.get("event_name") or "tool_result"),
                    event.get("model"),
                    event.get("session_id"),
                    event.get("thread_id"),
                    str(event.get("tool_name") or "(unknown)"),
                    event.get("call_id"),
                    event.get("decision"),
                    event.get("source"),
                    event.get("success"),
                    int(event["duration_ms"]) if event.get("duration_ms") not in (None, "") else None,
                    event.get("mcp_server"),
                    str(event.get("attributes_json") or "{}"),
                ),
            )
            accepted += 1
        except sqlite3.IntegrityError:
            duplicates += 1
    if events:
        con.execute("update clients set last_seen_at = ?, updated_at = ? where client_name = ?", (received_at, received_at, client_name))
    return accepted, duplicates


def server_where_clause(args: argparse.Namespace) -> tuple[str, list[Any]]:
    clauses = []
    params: list[Any] = []
    since = parse_datetime_filter(getattr(args, "since", None))
    until = parse_datetime_filter(getattr(args, "until", None), end_of_day=True)
    if since:
        clauses.append("usage_events.source_received_at >= ?")
        params.append(since)
    if until:
        clauses.append("usage_events.source_received_at <= ?")
        params.append(until)
    if getattr(args, "model", None):
        clauses.append("usage_events.model = ?")
        params.append(args.model)
    if getattr(args, "session_id", None):
        clauses.append("usage_events.session_id = ?")
        params.append(args.session_id)
    if getattr(args, "client_name", None):
        clauses.append("usage_events.client_name = ?")
        params.append(args.client_name)
    if getattr(args, "source_kind", None):
        clauses.append("usage_events.source_kind = ?")
        params.append(args.source_kind)
    if getattr(args, "workspace_label", None):
        clauses.append("usage_events.workspace_label = ?")
        params.append(args.workspace_label)
    if getattr(args, "api_key_label", None):
        clauses.append("usage_events.api_key_label = ?")
        params.append(args.api_key_label)
    if getattr(args, "provider_name", None):
        clauses.append("usage_events.provider_name = ?")
        params.append(args.provider_name)
    return ("where " + " and ".join(clauses), params) if clauses else ("", params)


def server_tool_where_clause(args: argparse.Namespace) -> tuple[str, list[Any]]:
    clauses = []
    params: list[Any] = []
    since = parse_datetime_filter(getattr(args, "since", None))
    until = parse_datetime_filter(getattr(args, "until", None), end_of_day=True)
    if since:
        clauses.append("tool_events.source_received_at >= ?")
        params.append(since)
    if until:
        clauses.append("tool_events.source_received_at <= ?")
        params.append(until)
    if getattr(args, "tool_name", None):
        clauses.append("tool_events.tool_name = ?")
        params.append(args.tool_name)
    if getattr(args, "session_id", None):
        clauses.append("tool_events.session_id = ?")
        params.append(args.session_id)
    if getattr(args, "client_name", None):
        clauses.append("tool_events.client_name = ?")
        params.append(args.client_name)
    if getattr(args, "event_name", None):
        clauses.append("tool_events.event_name = ?")
        params.append(args.event_name)
    return ("where " + " and ".join(clauses), params) if clauses else ("", params)


def source_provider_expression(table: str = "usage_events") -> str:
    return f"""
        case
          when {table}.client_name = '{OPENROUTER_BROADCAST_CLIENT}'
            or {table}.source_kind = '{OPENROUTER_SOURCE_KIND}' then 'OpenRouter'
          when lower(coalesce({table}.source_kind, '')) in ('claude-code', 'claude_code')
            or {table}.event_name like 'claude_code.%' then 'Claude Code'
          when lower(coalesce({table}.source_kind, '')) in ('codex', 'openai-codex')
            or {table}.event_name like 'codex.%' then 'Codex'
          else 'Local OTEL'
        end
    """


def source_label_expression() -> str:
    return f"""
        case
          when usage_events.client_name = '{OPENROUTER_BROADCAST_CLIENT}'
            or usage_events.source_kind = '{OPENROUTER_SOURCE_KIND}'
            then coalesce(
              nullif(usage_events.workspace_label, ''),
              nullif(usage_events.api_key_label, ''),
              nullif(usage_events.provider_name, ''),
              '(unknown)'
            )
          else coalesce(
            nullif(usage_events.workspace_label, ''),
            clients.display_name,
            usage_events.client_name,
            '(unknown)'
          )
        end
    """


def server_group_expressions(group_by: str) -> tuple[str, str, str, str, str, str, str, str, str, str, str]:
    period_expr = "''"
    source_provider_expr = "''"
    source_label_expr = "''"
    client_expr = "''"
    model_expr = "''"
    session_expr = "''"
    source_expr = "''"
    workspace_expr = "''"
    api_key_expr = "''"
    provider_expr = "''"
    cost_unit_expr = "coalesce(cost_unit, '')"
    if group_by in ("day", "day-model", "day-session", "day-client", "day-model-client"):
        period_expr = "substr(usage_events.source_received_at, 1, 10)"
    if group_by in ("provider", "provider-source", "provider-source-model"):
        source_provider_expr = source_provider_expression()
    if group_by in ("provider-source", "provider-source-model"):
        source_label_expr = source_label_expression()
    if group_by in ("client", "client-model", "day-client", "day-model-client"):
        client_expr = "coalesce(clients.display_name, usage_events.client_name, '(unknown)')"
    if group_by in ("model", "client-model", "day-model", "day-model-client", "provider-source-model"):
        model_expr = "coalesce(model, '(unknown)')"
    if group_by in ("session", "day-session"):
        session_expr = "coalesce(session_id, '(unknown)')"
    if group_by == "source":
        source_expr = "coalesce(source_kind, '(unknown)')"
    if group_by == "workspace":
        workspace_expr = "coalesce(workspace_label, '(unknown)')"
    if group_by == "api-key":
        api_key_expr = "coalesce(api_key_label, '(unknown)')"
    if group_by == "model-provider":
        provider_expr = "coalesce(provider_name, '(unknown)')"
    return (
        period_expr,
        source_provider_expr,
        source_label_expr,
        client_expr,
        model_expr,
        session_expr,
        source_expr,
        workspace_expr,
        api_key_expr,
        provider_expr,
        cost_unit_expr,
    )


def server_tool_group_expressions(group_by: str) -> tuple[str, str, str, str, str]:
    period_expr = "''"
    client_expr = "''"
    tool_expr = "''"
    session_expr = "''"
    event_expr = "''"
    if group_by in ("day", "day-tool", "day-session", "day-client", "day-tool-client"):
        period_expr = "substr(tool_events.source_received_at, 1, 10)"
    if group_by in ("client", "client-tool", "day-client", "day-tool-client"):
        client_expr = "coalesce(clients.display_name, tool_events.client_name, '(unknown)')"
    if group_by in ("tool", "client-tool", "day-tool", "day-tool-client"):
        tool_expr = "coalesce(tool_name, '(unknown)')"
    if group_by in ("session", "day-session"):
        session_expr = "coalesce(session_id, '(unknown)')"
    if group_by == "event":
        event_expr = "event_name"
    return period_expr, client_expr, tool_expr, session_expr, event_expr


def server_report_rows(con: sqlite3.Connection, args: argparse.Namespace) -> list[sqlite3.Row]:
    (
        period_expr,
        source_provider_expr,
        source_label_expr,
        client_expr,
        model_expr,
        session_expr,
        source_expr,
        workspace_expr,
        api_key_expr,
        provider_expr,
        cost_unit_expr,
    ) = server_group_expressions(args.group_by)
    cost_unit_grouped = args.group_by in COST_UNIT_REPORT_GROUPS
    cost_value_select = "coalesce(sum(usage_events.cost_value), 0)"
    cost_unit_select = cost_unit_expr
    cost_group = "11"
    if not cost_unit_grouped:
        cost_value_select = """
            case
              when count(distinct coalesce(usage_events.cost_unit, '')) <= 1 then coalesce(sum(usage_events.cost_value), 0)
              else null
            end
        """
        cost_unit_select = """
            case
              when count(distinct coalesce(usage_events.cost_unit, '')) <= 1 then max(usage_events.cost_unit)
              else 'mixed'
            end
        """
        cost_group = "''"
    where, params = server_where_clause(args)
    order_by = "last_seen desc"
    if args.group_by.startswith("day"):
        order_by = "period desc, total_tokens desc"
    elif args.group_by in (
        "model",
        "session",
        "client",
        "client-model",
        "source",
        "workspace",
        "api-key",
        "provider",
        "provider-source",
        "provider-source-model",
        "model-provider",
    ):
        order_by = "total_tokens desc"
    query = f"""
        select
            {period_expr} as period,
            {source_provider_expr} as source_provider,
            {source_label_expr} as source_label,
            {client_expr} as client_name,
            {model_expr} as model,
            {session_expr} as session_id,
            {source_expr} as source_kind,
            {workspace_expr} as workspace_label,
            {api_key_expr} as api_key_label,
            {provider_expr} as provider_name,
            {cost_unit_select} as cost_unit,
            count(*) as events,
            coalesce(sum(usage_events.input_tokens), 0) as input_tokens,
            coalesce(sum(usage_events.output_tokens), 0) as output_tokens,
            coalesce(sum(usage_events.total_tokens), 0) as total_tokens,
            coalesce(sum(usage_events.cached_tokens), 0) as cached_tokens,
            coalesce(sum(usage_events.reasoning_tokens), 0) as reasoning_tokens,
            {cost_value_select} as cost_value,
            min(usage_events.source_received_at) as first_seen,
            max(usage_events.source_received_at) as last_seen
        from usage_events
        left join clients on clients.client_name = usage_events.client_name
        {where}
        group by 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, {cost_group}
        order by {order_by}
        limit ?
    """
    return con.execute(query, (*params, args.limit)).fetchall()


def server_tool_report_rows(con: sqlite3.Connection, args: argparse.Namespace) -> list[sqlite3.Row]:
    period_expr, client_expr, tool_expr, session_expr, event_expr = server_tool_group_expressions(args.group_by)
    where, params = server_tool_where_clause(args)
    order_by = "last_seen desc"
    if args.group_by.startswith("day"):
        order_by = "period desc, tool_events desc"
    elif args.group_by in ("tool", "session", "event", "client", "client-tool"):
        order_by = "tool_events desc"
    query = f"""
        select
            {period_expr} as period,
            {client_expr} as client_name,
            {tool_expr} as tool_name,
            {session_expr} as session_id,
            {event_expr} as event_name,
            count(*) as tool_events,
            coalesce(sum(case when tool_events.success = 'true' then 1 else 0 end), 0) as successes,
            coalesce(sum(case when tool_events.success = 'false' then 1 else 0 end), 0) as failures,
            coalesce(sum(tool_events.duration_ms), 0) as total_duration_ms,
            coalesce(round(avg(tool_events.duration_ms)), 0) as avg_duration_ms,
            min(tool_events.source_received_at) as first_seen,
            max(tool_events.source_received_at) as last_seen
        from tool_events
        left join clients on clients.client_name = tool_events.client_name
        {where}
        group by 1, 2, 3, 4, 5
        order by {order_by}
        limit ?
    """
    return con.execute(query, (*params, args.limit)).fetchall()


def server_tool_recent_rows(con: sqlite3.Connection, args: argparse.Namespace) -> list[sqlite3.Row]:
    where, params = server_tool_where_clause(args)
    query = f"""
        select
            tool_events.source_received_at,
            coalesce(clients.display_name, tool_events.client_name, '(unknown)') as client_name,
            tool_events.tool_name,
            tool_events.event_name,
            tool_events.success,
            tool_events.duration_ms,
            tool_events.decision,
            tool_events.source,
            tool_events.mcp_server,
            tool_events.session_id,
            tool_events.call_id
        from tool_events
        left join clients on clients.client_name = tool_events.client_name
        {where}
        order by tool_events.source_received_at desc, tool_events.id desc
        limit ?
    """
    return con.execute(query, (*params, args.limit)).fetchall()


def server_tool_summary(con: sqlite3.Connection, args: argparse.Namespace) -> sqlite3.Row:
    where, params = server_tool_where_clause(args)
    query = f"""
        select
            count(*) as tool_events,
            coalesce(sum(case when tool_events.success = 'true' then 1 else 0 end), 0) as successes,
            coalesce(sum(case when tool_events.success = 'false' then 1 else 0 end), 0) as failures,
            coalesce(sum(tool_events.duration_ms), 0) as total_duration_ms,
            coalesce(round(avg(tool_events.duration_ms)), 0) as avg_duration_ms,
            min(tool_events.source_received_at) as first_seen,
            max(tool_events.source_received_at) as last_seen
        from tool_events
        left join clients on clients.client_name = tool_events.client_name
        {where}
    """
    return con.execute(query, params).fetchone()


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
            case
              when count(distinct coalesce(cost_unit, '')) <= 1 then coalesce(sum(cost_value), 0)
              else null
            end as cost_value,
            case
              when count(distinct coalesce(cost_unit, '')) <= 1 then max(cost_unit)
              else 'mixed'
            end as cost_unit,
            min(source_received_at) as first_seen,
            max(source_received_at) as last_seen
        from usage_events
        """
    ).fetchone()
    clients = con.execute("select count(*) from clients where revoked_at is null").fetchone()[0]
    tool_events = con.execute("select count(*) from tool_events").fetchone()[0]
    out = dict(totals)
    out["configured_clients"] = clients
    out["tool_events"] = tool_events
    return out


def reindex(args: argparse.Namespace) -> None:
    con = connect(Path(args.db))
    config = load_config(Path(args.config))
    try:
        raw_count, inserted, inserted_tool_events = reindex_database(con, config, keep_existing=args.keep_existing)
        con.commit()
        print(
            f"Reindexed {raw_count} raw payloads; inserted {inserted} usage events "
            f"and {inserted_tool_events} tool events."
        )
    finally:
        con.close()


def replay_broadcast(args: argparse.Namespace) -> None:
    config = load_config(Path(args.config))
    con = connect_server(Path(args.server_db or config.central.db))
    try:
        result = replay_broadcast_payloads(
            con,
            config,
            payload_id=args.payload_id,
            since=args.since,
            until=args.until,
            replay_status=args.replay_status,
            limit=args.limit,
        )
        con.commit()
        if args.format == "json":
            print(json.dumps(result, indent=2))
        else:
            print_table([result], ("payloads", "accepted", "duplicates", "errors"))
    finally:
        con.close()


def cleanup(args: argparse.Namespace) -> None:
    con = connect(Path(args.db))
    config = load_config(Path(args.config))
    try:
        preserved_events = usage_events_without_reindexable_raw(con, config)
        preserved_tool_events = tool_events_without_reindexable_raw(con, config)
        raw_count, inserted, inserted_tool_events = reindex_database(con, config)
        for raw_id, received_at, event in preserved_events:
            insert_usage(con, raw_id, received_at, [event])
        for raw_id, received_at, event in preserved_tool_events:
            insert_tool_events(con, raw_id, received_at, [event])
        cleared_payloads = cleanup_stored_data(con, config)
        con.commit()
        print(
            f"Reindexed {raw_count} raw payloads; inserted {inserted} usage events "
            f"and {inserted_tool_events} tool events; "
            f"preserved {len(preserved_events)} existing usage events and "
            f"{len(preserved_tool_events)} tool events without raw bodies."
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


def version(args: argparse.Namespace) -> None:
    print(f"ai-usage-tracker {APP_VERSION}")


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


def tools_report(args: argparse.Namespace) -> None:
    con = connect(Path(args.db))
    try:
        rows = tool_report_rows(con, args)
        if args.format == "json":
            columns = tool_default_columns(args.group_by)
            print(json.dumps([{column: row[column] for column in columns} for row in rows], indent=2))
        elif args.format == "csv":
            write_csv(rows, sys.stdout, tool_default_columns(args.group_by))
        else:
            print_table(rows, tool_default_columns(args.group_by))
    finally:
        con.close()


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

    p_tools_report = sub.add_parser("tools-report", parents=[db_parent], help="Grouped AI tool usage report")
    p_tools_report.add_argument(
        "--group-by",
        choices=("total", "day", "tool", "session", "event", "day-tool", "day-session"),
        default="tool",
    )
    p_tools_report.add_argument("--event-name", default="", help="Only include one tool event type")
    p_tools_report.add_argument("--tool-name", help="Only include one tool")
    add_report_filters(p_tools_report)
    add_report_output(p_tools_report)
    p_tools_report.set_defaults(func=tools_report)

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

    p_version = sub.add_parser("version", help="Print version and exit")
    p_version.set_defaults(func=version)

    p_sync = sub.add_parser(
        "sync", parents=[db_parent], help="Forward queued client usage events to the aggregation server"
    )
    p_sync.add_argument("--limit", type=int)
    p_sync.set_defaults(func=sync)

    p_sync_status = sub.add_parser("sync-status", parents=[db_parent], help="Show collector sync progress")
    p_sync_status.add_argument("--format", choices=("table", "json"), default="table")
    p_sync_status.add_argument("--errors", type=int, default=0, help="Show this many pending sync error groups")
    p_sync_status.set_defaults(func=sync_status)

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

    p_client_sync_status = client_sub.add_parser("sync-status", parents=[db_parent])
    p_client_sync_status.add_argument("--format", choices=("table", "json"), default="table")
    p_client_sync_status.add_argument("--errors", type=int, default=0, help="Show this many pending sync error groups")
    p_client_sync_status.set_defaults(func=sync_status)

    p_client_version = client_sub.add_parser("version")
    p_client_version.set_defaults(func=version)

    p_server = sub.add_parser("server", help="Aggregation server commands")
    server_sub = p_server.add_subparsers(dest="server_cmd", required=True)
    p_server_serve = server_sub.add_parser("serve", parents=[db_parent])
    p_server_serve.add_argument("--host")
    p_server_serve.add_argument("--port", type=int)
    p_server_serve.add_argument("--server-db")
    p_server_serve.add_argument("--allow-remote", action="store_true", help="Allow binding to a non-loopback host")
    p_server_serve.set_defaults(func=server_serve)

    p_server_replay = server_sub.add_parser("replay-broadcast", parents=[db_parent])
    p_server_replay.add_argument("--server-db")
    p_server_replay.add_argument("--payload-id", type=int)
    p_server_replay.add_argument("--since")
    p_server_replay.add_argument("--until")
    p_server_replay.add_argument("--replay-status")
    p_server_replay.add_argument("--limit", type=int)
    p_server_replay.add_argument("--format", choices=("table", "json"), default="table")
    p_server_replay.set_defaults(func=replay_broadcast)

    p_server_version = server_sub.add_parser("version")
    p_server_version.set_defaults(func=version)

    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
