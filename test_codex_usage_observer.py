import argparse
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import codex_usage_observer as app
import codex_usage_tracker.aggregation_server as aggregation_server
import codex_usage_tracker.client as client_compat
import codex_usage_tracker.collector as collector
import codex_usage_tracker.core as core
import codex_usage_tracker.server as server_compat


def log_payload(attrs, body=None):
    record = {
        "attributes": [
            {"key": key, "value": {"stringValue": str(value)}}
            for key, value in attrs.items()
        ]
    }
    if body is not None:
        record["body"] = {"stringValue": body}
    return {
        "resourceLogs": [
            {
                "resource": {"attributes": [{"key": "session_id", "value": {"stringValue": "s1"}}]},
                "scopeLogs": [{"logRecords": [record]}],
            }
        ]
    }


def openrouter_trace_payload(trace_id="trace-1", span_id="span-1", prompt=12, completion=5):
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "openrouter"}}]},
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": trace_id,
                                "spanId": span_id,
                                "name": "chat",
                                "startTimeUnixNano": "1777856523000000000",
                                "attributes": [
                                    {"key": "gen_ai.request.model", "value": {"stringValue": "openai/gpt-4o"}},
                                    {"key": "gen_ai.usage.input_tokens", "value": {"intValue": str(prompt)}},
                                    {"key": "gen_ai.usage.output_tokens", "value": {"intValue": str(completion)}},
                                    {"key": "gen_ai.usage.total_tokens", "value": {"intValue": str(prompt + completion)}},
                                    {"key": "gen_ai.usage.cost", "value": {"doubleValue": "0.0123"}},
                                    {"key": "openrouter.provider_name", "value": {"stringValue": "OpenAI"}},
                                    {"key": "trace.metadata.workspace", "value": {"stringValue": "agents"}},
                                    {"key": "trace.metadata.api_key_label", "value": {"stringValue": "prod-key"}},
                                    {"key": "openrouter.api_key", "value": {"stringValue": "sk-secret"}},
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }


def openrouter_trace_payload_with_time(nanos):
    payload = openrouter_trace_payload()
    payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["startTimeUnixNano"] = str(nanos)
    return payload


def observed_openrouter_trace_payload():
    payload = openrouter_trace_payload()
    attrs = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]
    attrs[:] = [
        {"key": "gen_ai.request.model", "value": {"stringValue": "moonshotai/kimi-k2.5"}},
        {"key": "gen_ai.provider.name", "value": {"stringValue": "moonshotai"}},
        {"key": "gen_ai.system", "value": {"stringValue": "moonshotai"}},
        {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "15041"}},
        {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "70"}},
        {"key": "gen_ai.usage.total_tokens", "value": {"intValue": "15111"}},
        {"key": "gen_ai.usage.total_cost", "value": {"doubleValue": "0.00639196"}},
        {"key": "trace.metadata.openrouter.api_key_name", "value": {"stringValue": "prod-key"}},
        {"key": "trace.metadata.openrouter.entity_id", "value": {"stringValue": "org_123"}},
        {"key": "trace.metadata.openrouter.provider_name", "value": {"stringValue": "Chutes"}},
        {"key": "trace.metadata.openrouter.provider_slug", "value": {"stringValue": "chutes/int4"}},
    ]
    return payload


class ComponentLayoutTests(unittest.TestCase):
    def test_component_packages_expose_expected_surfaces(self):
        self.assertIs(collector.Receiver, core.Receiver)
        self.assertIs(collector.serve, core.serve)
        self.assertIs(collector.sync_all_pending_usage, core.sync_all_pending_usage)
        self.assertIs(aggregation_server.ServerReceiver, core.ServerReceiver)
        self.assertIs(aggregation_server.server_serve, core.server_serve)
        self.assertIs(aggregation_server.server_report_rows, core.server_report_rows)

    def test_legacy_component_modules_remain_compatible(self):
        self.assertIs(client_compat.Receiver, collector.Receiver)
        self.assertIs(server_compat.ServerReceiver, aggregation_server.ServerReceiver)


class ExtractionTests(unittest.TestCase):
    def test_extracts_token_usage_from_log_attributes(self):
        payload = log_payload(
            {
                "event.name": "response.completed",
                "model": "gpt-test",
                "input_tokens": 11,
                "output_tokens": 7,
            }
        )

        events = app.extract_usage("/v1/logs", json.dumps(payload).encode())

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["model"], "gpt-test")
        self.assertEqual(events[0]["session_id"], "s1")
        self.assertEqual(events[0]["input_tokens"], 11)
        self.assertEqual(events[0]["output_tokens"], 7)
        self.assertEqual(events[0]["total_tokens"], 18)

    def test_extracts_nested_json_usage_from_log_body(self):
        payload = log_payload(
            {"event.name": "response.completed", "model": "gpt-test"},
            body=json.dumps({"usage": {"input_tokens": 3, "output_tokens": 5, "cached_tokens": 2}}),
        )

        events = app.extract_usage("/v1/logs", json.dumps(payload).encode())

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["input_tokens"], 3)
        self.assertEqual(events[0]["output_tokens"], 5)
        self.assertEqual(events[0]["cached_tokens"], 2)
        self.assertEqual(events[0]["total_tokens"], 8)

    def test_cost_unit_matches_selected_cost_alias(self):
        event = app.usage_from_attrs(
            "traces",
            "chat",
            {
                "gen_ai.usage.input_tokens": 1,
                "gen_ai.usage.output_tokens": 1,
                "cost": "99",
                "gen_ai.usage.cost_usd": "0.25",
            },
        )

        self.assertIsNotNone(event)
        self.assertEqual(event["cost_value"], 0.25)
        self.assertEqual(event["cost_unit"], "USD")

    def test_extracts_observed_openrouter_broadcast_fields(self):
        body = json.dumps(observed_openrouter_trace_payload()).encode()
        config = app.AppConfig(openrouter_broadcast=app.OpenRouterBroadcastConfig(enabled=True, api_key="orb_secret"))

        event = app.normalize_openrouter_broadcast(body, config)[0]

        self.assertEqual(event["model"], "moonshotai/kimi-k2.5")
        self.assertEqual(event["total_tokens"], 15111)
        self.assertAlmostEqual(event["cost_value"], 0.00639196)
        self.assertEqual(event["cost_unit"], "credits")
        self.assertEqual(event["workspace_label"], "org_123")
        self.assertEqual(event["api_key_label"], "prod-key")
        self.assertEqual(event["provider_name"], "Chutes")

    def test_redacts_account_metadata_but_keeps_token_counts(self):
        payload = log_payload(
            {
                "event.name": "response.completed",
                "model": "gpt-test",
                "user.email": "person@example.com",
                "user.account_id": "acct_123",
                "input_token_count": 13,
                "output_token_count": 2,
            }
        )

        event = app.extract_usage("/v1/logs", json.dumps(payload).encode())[0]
        attrs = json.loads(event["attributes_json"])

        self.assertEqual(attrs["user.email"], "[redacted]")
        self.assertEqual(attrs["user.account_id"], "[redacted]")
        self.assertEqual(attrs["input_token_count"], "13")
        self.assertEqual(attrs["output_token_count"], "2")

    def test_config_can_omit_attributes_and_identity_columns(self):
        config = app.AppConfig(
            storage=app.StorageConfig(
                extracted_attributes="none",
                model=False,
                session_id=False,
                thread_id=False,
            )
        )
        payload = log_payload(
            {
                "event.name": "response.completed",
                "model": "gpt-test",
                "session.id": "session-1",
                "thread.id": "thread-1",
                "input_tokens": 11,
                "output_tokens": 7,
            }
        )

        event = app.extract_usage("/v1/logs", json.dumps(payload).encode(), config)[0]

        self.assertIsNone(event["model"])
        self.assertIsNone(event["session_id"])
        self.assertIsNone(event["thread_id"])
        self.assertEqual(event["attributes_json"], "{}")

    def test_extracts_tool_result_events(self):
        payload = log_payload(
            {
                "event.name": "codex.tool_result",
                "model": "gpt-test",
                "conversation.id": "conv-1",
                "tool_name": "exec_command",
                "call_id": "call-1",
                "success": "true",
                "duration_ms": "63",
                "arguments": '{"cmd":"pwd"}',
                "output": "secret-ish command output",
            }
        )

        events = app.extract_tool_events("/v1/logs", json.dumps(payload).encode())

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["tool_name"], "exec_command")
        self.assertEqual(events[0]["session_id"], "s1")
        self.assertEqual(events[0]["success"], "true")
        self.assertEqual(events[0]["duration_ms"], 63)
        attrs = json.loads(events[0]["attributes_json"])
        self.assertNotIn("arguments", attrs)
        self.assertNotIn("output", attrs)

    def test_raw_payload_body_is_disabled_by_default(self):
        config = app.AppConfig()
        with tempfile.TemporaryDirectory() as tmp:
            con = app.connect(Path(tmp) / "usage.sqlite")
            raw_id = app.insert_payload(con, "/v1/logs", "application/json", b'{"secret": true}', config)
            body = con.execute("select body from raw_payloads where id = ?", (raw_id,)).fetchone()["body"]

            self.assertEqual(bytes(body), b"")
            con.close()

    def test_config_can_keep_raw_payload_body(self):
        config = app.AppConfig(storage=app.StorageConfig(raw_payload_body=True))
        with tempfile.TemporaryDirectory() as tmp:
            con = app.connect(Path(tmp) / "usage.sqlite")
            raw_id = app.insert_payload(con, "/v1/logs", "application/json", b'{"secret": true}', config)
            body = con.execute("select body from raw_payloads where id = ?", (raw_id,)).fetchone()["body"]

            self.assertEqual(bytes(body), b'{"secret": true}')
            con.close()

    def test_load_config_reads_client_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text('client_name = "work-laptop"\n', encoding="utf-8")

            config = app.load_config(config_path)

            self.assertEqual(config.client_name, "work-laptop")

    def test_load_config_uses_clear_component_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                """
client_name = "work-laptop"

[collector]
endpoint = "http://usage-server:18418"
api_key = "ait_test"
cloudflare_access_client_id = "cf-client-id"
cloudflare_access_client_secret = "cf-client-secret"
batch_size = 23
timeout_seconds = 7

[aggregation_server]
host = "0.0.0.0"
port = 18418
db = "aggregation.sqlite"
admin_api_key = "admin"
""",
                encoding="utf-8",
            )

            config = app.load_config(config_path)

            self.assertEqual(config.server.endpoint, "http://usage-server:18418")
            self.assertEqual(config.server.api_key, "ait_test")
            self.assertEqual(config.server.cloudflare_access_client_id, "cf-client-id")
            self.assertEqual(config.server.cloudflare_access_client_secret, "cf-client-secret")
            self.assertEqual(config.server.batch_size, 23)
            self.assertEqual(config.server.timeout_seconds, 7)
            self.assertEqual(config.central.host, "0.0.0.0")
            self.assertEqual(config.central.port, 18418)
            self.assertEqual(config.central.db, "aggregation.sqlite")
            self.assertEqual(config.central.admin_api_key, "admin")

    def test_load_config_reads_openrouter_broadcast_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                """
[openrouter_broadcast]
enabled = true
api_key = "orb_secret"
required_header_name = "X-OpenRouter-Broadcast-Secret"
required_header_value = "extra-secret"
retain_payload_body = true
""",
                encoding="utf-8",
            )

            config = app.load_config(config_path)

            self.assertTrue(config.openrouter_broadcast.enabled)
            self.assertEqual(config.openrouter_broadcast.api_key, "orb_secret")
            self.assertEqual(config.openrouter_broadcast.required_header_name, "X-OpenRouter-Broadcast-Secret")
            self.assertEqual(config.openrouter_broadcast.required_header_value, "extra-secret")
            self.assertTrue(config.openrouter_broadcast.retain_payload_body)

    def test_load_config_keeps_legacy_server_section_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                """
[server]
endpoint = "http://legacy-server:8318"
api_key = "legacy-client-token"

[central_server]
host = "0.0.0.0"
port = 8318
db = "legacy-server.sqlite"
""",
                encoding="utf-8",
            )

            config = app.load_config(config_path)

            self.assertEqual(config.server.endpoint, "http://legacy-server:8318")
            self.assertEqual(config.server.api_key, "legacy-client-token")
            self.assertEqual(config.central.host, "0.0.0.0")
            self.assertEqual(config.central.port, 8318)
            self.assertEqual(config.central.db, "legacy-server.sqlite")

    def test_load_config_clear_sections_override_legacy_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                """
[server]
endpoint = "http://legacy-server:8318"

[collector]
endpoint = "http://usage-server:18418"

[central_server]
port = 8318

[aggregation_server]
port = 18418
""",
                encoding="utf-8",
            )

            config = app.load_config(config_path)

            self.assertEqual(config.server.endpoint, "http://usage-server:18418")
            self.assertEqual(config.central.port, 18418)

    def test_serve_payload_log_line_includes_ingestion_details(self):
        line = app.serve_payload_log_line(
            path="/v1/logs",
            content_type="application/json",
            body_bytes=1536,
            shape="resourceLogs",
            raw_body_retained=False,
            events=2,
            tool_events=3,
            raw_id=42,
        )

        self.assertIn("received /v1/logs", line)
        self.assertIn("payload=1.5 KiB", line)
        self.assertIn("shape=resourceLogs", line)
        self.assertIn("content_type=application/json", line)
        self.assertIn("raw_body=metadata-only", line)
        self.assertIn("usage_events=2", line)
        self.assertIn("tool_events=3", line)
        self.assertIn("raw_id=42", line)


class DatabaseReportTests(unittest.TestCase):
    def test_report_rows_group_by_day_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "usage.sqlite"
            con = app.connect(db)
            raw_id = app.insert_payload(con, "/v1/logs", "application/json", b"{}")
            app.insert_usage(
                con,
                raw_id,
                "2026-05-04T01:02:03+00:00",
                [
                    {
                        "signal": "logs",
                        "event_name": "response.completed",
                        "model": "gpt-test",
                        "session_id": "s1",
                        "thread_id": None,
                        "input_tokens": 4,
                        "output_tokens": 6,
                        "total_tokens": 10,
                        "cached_tokens": 1,
                        "reasoning_tokens": 2,
                        "attributes_json": "{}",
                    }
                ],
            )
            con.commit()

            args = argparse.Namespace(
                group_by="day-model",
                since=None,
                until=None,
                model=None,
                session_id=None,
                limit=100,
            )
            rows = app.usage_report_rows(con, args)

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["period"], "2026-05-04")
            self.assertEqual(rows[0]["model"], "gpt-test")
            self.assertEqual(rows[0]["total_tokens"], 10)
            con.close()

    def test_tool_report_rows_group_by_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "usage.sqlite"
            con = app.connect(db)
            raw_id = app.insert_payload(con, "/v1/logs", "application/json", b"{}")
            app.insert_tool_events(
                con,
                raw_id,
                "2026-05-04T01:02:03+00:00",
                [
                    {
                        "signal": "logs",
                        "event_name": "codex.tool_result",
                        "model": "gpt-test",
                        "session_id": "s1",
                        "thread_id": None,
                        "tool_name": "exec_command",
                        "call_id": "call-1",
                        "decision": None,
                        "source": None,
                        "success": "true",
                        "duration_ms": 10,
                        "mcp_server": "",
                        "attributes_json": "{}",
                    },
                    {
                        "signal": "logs",
                        "event_name": "codex.tool_result",
                        "model": "gpt-test",
                        "session_id": "s1",
                        "thread_id": None,
                        "tool_name": "exec_command",
                        "call_id": "call-2",
                        "decision": None,
                        "source": None,
                        "success": "false",
                        "duration_ms": 20,
                        "mcp_server": "",
                        "attributes_json": "{}",
                    },
                ],
            )
            con.commit()

            args = argparse.Namespace(
                group_by="tool",
                since=None,
                until=None,
                tool_name=None,
                session_id=None,
                event_name="codex.tool_result",
                limit=100,
            )
            rows = app.tool_report_rows(con, args)

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["tool_name"], "exec_command")
            self.assertEqual(rows[0]["tool_events"], 2)
            self.assertEqual(rows[0]["successes"], 1)
            self.assertEqual(rows[0]["failures"], 1)
            self.assertEqual(rows[0]["total_duration_ms"], 30)
            con.close()

    def test_cleanup_reindexes_before_clearing_raw_payloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "usage.sqlite"
            con = app.connect(db)
            config = app.AppConfig(
                storage=app.StorageConfig(
                    raw_payload_body=False,
                    extracted_attributes="none",
                )
            )
            raw_config = app.AppConfig(storage=app.StorageConfig(raw_payload_body=True))
            payload = log_payload(
                {
                    "event.name": "response.completed",
                    "model": "gpt-test",
                    "input_tokens": 4,
                    "output_tokens": 6,
                }
            )
            raw_id = app.insert_payload(con, "/v1/logs", "application/json", json.dumps(payload).encode(), raw_config)
            empty_raw_id = app.insert_payload(con, "/v1/logs", "application/json", b"", config)
            app.insert_usage(
                con,
                raw_id,
                "2026-05-04T01:02:03+00:00",
                [
                    {
                        "signal": "logs",
                        "event_name": "stale",
                        "model": "stale-model",
                        "session_id": "stale-session",
                        "thread_id": None,
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "total_tokens": 2,
                        "cached_tokens": 0,
                        "reasoning_tokens": 0,
                        "attributes_json": '{"stale": true}',
                    }
                ],
            )
            app.insert_usage(
                con,
                empty_raw_id,
                "2026-05-04T02:00:00+00:00",
                [
                    {
                        "signal": "logs",
                        "event_name": "already-clean",
                        "model": "gpt-clean",
                        "session_id": "clean-session",
                        "thread_id": None,
                        "input_tokens": 2,
                        "output_tokens": 3,
                        "total_tokens": 5,
                        "cached_tokens": 0,
                        "reasoning_tokens": 0,
                        "attributes_json": '{"authorization": "token", "kept": true}',
                    }
                ],
            )

            preserved_events = app.usage_events_without_reindexable_raw(con, config)
            raw_count, inserted, inserted_tool_events = app.reindex_database(con, config)
            for preserved_raw_id, received_at, event in preserved_events:
                app.insert_usage(con, preserved_raw_id, received_at, [event])
            cleared_payloads = app.cleanup_stored_data(con, config)
            con.commit()

            events = {row["model"]: row for row in con.execute("select * from usage_events").fetchall()}
            body = con.execute("select body from raw_payloads where id = ?", (raw_id,)).fetchone()["body"]

            self.assertEqual(raw_count, 2)
            self.assertEqual(inserted, 1)
            self.assertEqual(inserted_tool_events, 0)
            self.assertEqual(len(preserved_events), 1)
            self.assertEqual(cleared_payloads, 1)
            self.assertEqual(bytes(body), b"")
            self.assertEqual(set(events), {"gpt-test", "gpt-clean"})
            self.assertEqual(events["gpt-test"]["total_tokens"], 10)
            self.assertEqual(events["gpt-test"]["attributes_json"], "{}")
            self.assertEqual(events["gpt-clean"]["total_tokens"], 5)
            self.assertEqual(events["gpt-clean"]["attributes_json"], "{}")
            con.close()


class ClientSyncTests(unittest.TestCase):
    def insert_sync_event(self, con):
        raw_id = app.insert_payload(con, "/v1/logs", "application/json", b"{}")
        app.insert_usage(
            con,
            raw_id,
            "2026-05-04T01:02:03+00:00",
            [
                {
                    "signal": "logs",
                    "event_name": "response.completed",
                    "model": "gpt-test",
                    "session_id": "s1",
                    "thread_id": None,
                    "input_tokens": 4,
                    "output_tokens": 6,
                    "total_tokens": 10,
                    "cached_tokens": 0,
                    "reasoning_tokens": 0,
                    "attributes_json": "{}",
                }
            ],
        )

    def insert_sync_tool_event(self, con):
        raw_id = app.insert_payload(con, "/v1/logs", "application/json", b"{}")
        app.insert_tool_events(
            con,
            raw_id,
            "2026-05-04T01:02:04+00:00",
            [
                {
                    "signal": "logs",
                    "event_name": "codex.tool_result",
                    "model": "gpt-test",
                    "session_id": "s1",
                    "thread_id": None,
                    "tool_name": "exec_command",
                    "call_id": "call-1",
                    "decision": None,
                    "source": None,
                    "success": "true",
                    "duration_ms": 42,
                    "mcp_server": "",
                    "attributes_json": "{}",
                }
            ],
        )

    def test_sync_marks_successful_rows_synced(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "usage.sqlite"
            con = app.connect(db)
            self.insert_sync_event(con)
            config = app.AppConfig(
                server=app.RemoteServerConfig(endpoint="http://server", api_key="secret")
            )

            with patch.object(core, "post_usage_batch", return_value={"accepted": 1, "duplicates": 0}):
                attempted, synced, error = app.sync_pending_usage(con, config)

            row = con.execute("select synced_at, synced_server_key, last_sync_error from usage_events").fetchone()
            self.assertEqual(attempted, 1)
            self.assertEqual(synced, 1)
            self.assertIsNone(error)
            self.assertIsNotNone(row["synced_at"])
            self.assertEqual(row["synced_server_key"], app.sync_server_key(config))
            self.assertIsNone(row["last_sync_error"])

            attempted, synced, error = app.sync_pending_usage(con, config)
            self.assertEqual((attempted, synced, error), (0, 0, None))
            con.close()

    def test_sync_resends_history_when_server_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "usage.sqlite"
            con = app.connect(db)
            self.insert_sync_event(con)
            first = app.AppConfig(
                server=app.RemoteServerConfig(endpoint="http://server-a", api_key="secret-a")
            )
            second = app.AppConfig(
                server=app.RemoteServerConfig(endpoint="http://server-b", api_key="secret-b")
            )

            with patch.object(core, "post_usage_batch", return_value={"accepted": 1, "duplicates": 0}) as post:
                self.assertEqual(app.sync_pending_usage(con, first), (1, 1, None))
                self.assertEqual(app.sync_pending_usage(con, second), (1, 1, None))

            row = con.execute("select synced_server_key from usage_events").fetchone()
            self.assertEqual(post.call_count, 2)
            self.assertEqual(row["synced_server_key"], app.sync_server_key(second))
            con.close()

    def test_sync_all_drains_multiple_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "usage.sqlite"
            con = app.connect(db)
            for _ in range(3):
                self.insert_sync_event(con)
            config = app.AppConfig(
                server=app.RemoteServerConfig(endpoint="http://server", api_key="secret", batch_size=2)
            )

            with patch.object(
                core,
                "post_usage_batch",
                side_effect=lambda _config, rows, tool_rows=(): {
                    "accepted": len(rows),
                    "duplicates": 0,
                    "accepted_tool_events": len(tool_rows),
                    "duplicate_tool_events": 0,
                },
            ) as post:
                attempted, synced, error = app.sync_all_pending_usage(con, config)

            pending = app.pending_sync_rows(con, config, 10)
            self.assertEqual((attempted, synced, error), (3, 3, None))
            self.assertEqual(post.call_count, 2)
            self.assertEqual(pending, [])
            con.close()

    def test_sync_failure_leaves_rows_queued(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "usage.sqlite"
            con = app.connect(db)
            self.insert_sync_event(con)
            config = app.AppConfig(
                server=app.RemoteServerConfig(endpoint="http://server", api_key="secret")
            )

            with patch.object(core, "post_usage_batch", side_effect=OSError("offline")):
                attempted, synced, error = app.sync_pending_usage(con, config)

            row = con.execute("select synced_at, sync_attempts, last_sync_error from usage_events").fetchone()
            self.assertEqual(attempted, 1)
            self.assertEqual(synced, 0)
            self.assertIn("offline", error)
            self.assertIsNone(row["synced_at"])
            self.assertEqual(row["sync_attempts"], 1)
            self.assertIn("offline", row["last_sync_error"])
            con.close()

    def test_sync_status_counts_current_target_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "usage.sqlite"
            con = app.connect(db)
            self.insert_sync_event(con)
            self.insert_sync_event(con)
            first = app.AppConfig(server=app.RemoteServerConfig(endpoint="http://server-a", api_key="secret-a"))
            second = app.AppConfig(server=app.RemoteServerConfig(endpoint="http://server-b", api_key="secret-b"))

            first_key = app.sync_server_key(first)
            second_key = app.sync_server_key(second)
            con.execute(
                "update usage_events set synced_at = ?, synced_server_key = ? where id = 1",
                ("2026-05-04T01:03:00+00:00", first_key),
            )
            con.execute(
                "update usage_events set synced_at = ?, synced_server_key = ? where id = 2",
                ("2026-05-04T01:04:00+00:00", second_key),
            )

            rows = app.sync_status_rows(con, second)
            usage = next(row for row in rows if row["table_name"] == "usage_events")
            tools = next(row for row in rows if row["table_name"] == "tool_events")

            self.assertEqual(usage["total_rows"], 2)
            self.assertEqual(usage["synced_rows"], 1)
            self.assertEqual(usage["pending_rows"], 1)
            self.assertEqual(usage["last_synced_at"], "2026-05-04T01:04:00+00:00")
            self.assertEqual(tools["total_rows"], 0)
            con.close()

    def test_sync_marks_tool_rows_synced(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "usage.sqlite"
            con = app.connect(db)
            self.insert_sync_tool_event(con)
            config = app.AppConfig(
                server=app.RemoteServerConfig(endpoint="http://server", api_key="secret")
            )

            with patch.object(
                core,
                "post_usage_batch",
                return_value={"accepted": 0, "duplicates": 0, "accepted_tool_events": 1, "duplicate_tool_events": 0},
            ):
                attempted, synced, error = app.sync_pending_usage(con, config)

            row = con.execute("select synced_at, synced_server_key, last_sync_error from tool_events").fetchone()
            self.assertEqual((attempted, synced, error), (1, 1, None))
            self.assertIsNotNone(row["synced_at"])
            self.assertEqual(row["synced_server_key"], app.sync_server_key(config))
            self.assertIsNone(row["last_sync_error"])
            con.close()

    def test_receiver_forwards_tool_only_payloads(self):
        payload = log_payload(
            {
                "event.name": "codex.tool_result",
                "model": "gpt-test",
                "tool_name": "exec_command",
                "call_id": "call-1",
                "success": "true",
                "duration_ms": "42",
            }
        )
        body = json.dumps(payload).encode()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "usage.sqlite"
            handler = object.__new__(app.Receiver)
            handler.db_path = db
            handler.app_config = app.AppConfig(
                server=app.RemoteServerConfig(endpoint="http://server", api_key="secret")
            )
            handler.path = "/v1/logs"
            handler.headers = {"content-length": str(len(body)), "content-type": "application/json"}
            handler.rfile = io.BytesIO(body)
            handler.wfile = io.BytesIO()
            responses = []
            handler.send_response = responses.append
            handler.send_header = lambda _key, _value: None
            handler.end_headers = lambda: None

            with patch.object(
                core,
                "post_usage_batch",
                return_value={"accepted": 0, "duplicates": 0, "accepted_tool_events": 1, "duplicate_tool_events": 0},
            ) as post:
                handler.do_POST()

            con = app.connect(db)
            usage_count = con.execute("select count(*) from usage_events").fetchone()[0]
            tool_row = con.execute("select synced_at, synced_server_key from tool_events").fetchone()
            con.close()

            self.assertEqual(responses, [200])
            self.assertEqual(usage_count, 0)
            self.assertIsNotNone(tool_row)
            self.assertIsNotNone(tool_row["synced_at"])
            self.assertEqual(tool_row["synced_server_key"], app.sync_server_key(handler.app_config))
            self.assertEqual(post.call_count, 1)

    def test_receiver_event_driven_sync_uses_configured_batch_size(self):
        payload = log_payload(
            {
                "event.name": "response.completed",
                "model": "gpt-test",
                "input_tokens": 1,
                "output_tokens": 1,
            }
        )
        body = json.dumps(payload).encode()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "usage.sqlite"
            con = app.connect(db)
            for _ in range(4):
                self.insert_sync_event(con)
            con.commit()
            con.close()

            handler = object.__new__(app.Receiver)
            handler.db_path = db
            handler.app_config = app.AppConfig(
                server=app.RemoteServerConfig(endpoint="http://server", api_key="secret", batch_size=5)
            )
            handler.path = "/v1/logs"
            handler.headers = {"content-length": str(len(body)), "content-type": "application/json"}
            handler.rfile = io.BytesIO(body)
            handler.wfile = io.BytesIO()
            responses = []
            handler.send_response = responses.append
            handler.send_header = lambda _key, _value: None
            handler.end_headers = lambda: None

            with patch.object(
                core,
                "post_usage_batch",
                return_value={"accepted": 5, "duplicates": 0, "accepted_tool_events": 0, "duplicate_tool_events": 0},
            ) as post:
                handler.do_POST()

            synced_rows = post.call_args.args[1]
            con = app.connect(db)
            pending = app.pending_sync_rows(con, handler.app_config, 10)
            con.close()

            self.assertEqual(responses, [200])
            self.assertEqual(len(synced_rows), 5)
            self.assertEqual(pending, [])

    def test_post_usage_batch_sends_cloudflare_access_headers_when_configured(self):
        config = app.AppConfig(
            server=app.RemoteServerConfig(
                endpoint="https://usage.example.com",
                api_key="ait_test",
                cloudflare_access_client_id="cf-client-id",
                cloudflare_access_client_secret="cf-client-secret",
            )
        )

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"accepted": 0, "duplicates": 0}'

        captured = {}

        def fake_urlopen(req, timeout):
            captured["url"] = req.full_url
            captured["timeout"] = timeout
            captured["headers"] = dict(req.header_items())
            return FakeResponse()

        with patch.object(core.request, "urlopen", side_effect=fake_urlopen):
            result = app.post_usage_batch(config, [])

        self.assertEqual(result, {"accepted": 0, "duplicates": 0})
        self.assertEqual(captured["url"], "https://usage.example.com/api/v1/usage-events")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer ait_test")
        self.assertEqual(captured["headers"]["User-agent"], "ai-usage-tracker-collector/0.1")
        self.assertEqual(captured["headers"]["Cf-access-client-id"], "cf-client-id")
        self.assertEqual(captured["headers"]["Cf-access-client-secret"], "cf-client-secret")

    def test_post_usage_batch_omits_cloudflare_access_headers_by_default(self):
        config = app.AppConfig(
            server=app.RemoteServerConfig(endpoint="https://usage.example.com", api_key="ait_test")
        )

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"accepted": 0, "duplicates": 0}'

        captured = {}

        def fake_urlopen(req, timeout):
            captured["headers"] = dict(req.header_items())
            return FakeResponse()

        with patch.object(core.request, "urlopen", side_effect=fake_urlopen):
            app.post_usage_batch(config, [])

        self.assertNotIn("Cf-access-client-id", captured["headers"])
        self.assertNotIn("Cf-access-client-secret", captured["headers"])


class ServerHttpTests(unittest.TestCase):
    def assertSharedNav(self, body, active):
        expected = {
            "admin": '<nav><div class="nav-primary"><a href="/reports">Token Usage</a><a href="/tools">Tool Usage</a></div><div class="nav-actions"><button type="button" class="theme-toggle" title="Toggle dark mode" aria-label="Toggle dark mode" onclick="aitToggleTheme()">Dark</button><a href="/admin" class="active">Admin</a></div></nav>',
            "usage": '<nav><div class="nav-primary"><a href="/reports" class="active">Token Usage</a><a href="/tools">Tool Usage</a></div><div class="nav-actions"><button type="button" class="theme-toggle" title="Toggle dark mode" aria-label="Toggle dark mode" onclick="aitToggleTheme()">Dark</button><a href="/admin">Admin</a></div></nav>',
            "tools": '<nav><div class="nav-primary"><a href="/reports">Token Usage</a><a href="/tools" class="active">Tool Usage</a></div><div class="nav-actions"><button type="button" class="theme-toggle" title="Toggle dark mode" aria-label="Toggle dark mode" onclick="aitToggleTheme()">Dark</button><a href="/admin">Admin</a></div></nav>',
        }[active]
        self.assertIn(expected, body)
        self.assertIn('href="/admin"', body)
        self.assertIn('href="/reports"', body)
        self.assertIn('href="/tools"', body)
        self.assertIn('html[data-theme="dark"]', body)
        self.assertIn('localStorage.getItem("ait-theme")', body)
        self.assertIn('rel="icon" type="image/svg+xml"', body)
        self.assertIn("M5%209.2H7V19H5V9.2", body)

    def test_server_ingest_auth_duplicate_and_revoked_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "server.sqlite"
            con = app.connect_server(db)
            token = app.create_client_token(con, "laptop", "Laptop")
            event = {
                "client_event_id": "evt-1",
                "received_at": "2026-05-04T01:02:03+00:00",
                "signal": "logs",
                "event_name": "response.completed",
                "model": "gpt-test",
                "input_tokens": 1,
                "output_tokens": 2,
                "total_tokens": 3,
                "cached_tokens": 0,
                "reasoning_tokens": 0,
                "attributes_json": "{}",
            }

            self.assertFalse(app.authenticate_client(con, "laptop", None))
            self.assertTrue(app.authenticate_client(con, "laptop", token))
            self.assertFalse(app.authenticate_client(con, "desktop", token))

            self.assertEqual(app.ingest_usage_events(con, "laptop", [event]), (1, 0))
            self.assertEqual(app.ingest_usage_events(con, "laptop", [event]), (0, 1))
            app.revoke_client(con, "laptop")
            self.assertFalse(app.authenticate_client(con, "laptop", token))
            con.close()

    def test_openrouter_broadcast_ingest_auth_dedupe_and_fields(self):
        body = json.dumps(openrouter_trace_payload()).encode()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "server.sqlite"
            handler = object.__new__(app.ServerReceiver)
            handler.db_path = db
            handler.app_config = app.AppConfig(
                openrouter_broadcast=app.OpenRouterBroadcastConfig(enabled=True, api_key="orb_secret")
            )
            handler.path = "/v1/traces"
            handler.headers = {
                "content-length": str(len(body)),
                "content-type": "application/json",
                "authorization": "Bearer orb_secret",
            }
            handler.rfile = io.BytesIO(body)
            handler.wfile = io.BytesIO()
            responses = []
            handler.send_response = responses.append
            handler.send_header = lambda _key, _value: None
            handler.end_headers = lambda: None

            handler.do_POST()
            handler.rfile = io.BytesIO(body)
            handler.wfile = io.BytesIO()
            handler.do_POST()

            con = app.connect_server(db)
            rows = con.execute("select * from usage_events").fetchall()
            payloads = con.execute("select replay_status, length(body) as body_len from broadcast_payloads").fetchall()
            con.close()

            self.assertEqual(responses, [200, 200])
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["client_name"], "openrouter-broadcast")
            self.assertEqual(rows[0]["client_event_id"], "orb:trace-1:span-1")
            self.assertEqual(rows[0]["source_received_at"], "2026-05-04T01:02:03+00:00")
            self.assertEqual(rows[0]["source_kind"], "openrouter_broadcast")
            self.assertEqual(rows[0]["workspace_label"], "agents")
            self.assertEqual(rows[0]["api_key_label"], "prod-key")
            self.assertEqual(rows[0]["provider_name"], "OpenAI")
            self.assertAlmostEqual(rows[0]["cost_value"], 0.0123)
            self.assertEqual(rows[0]["cost_unit"], "credits")
            self.assertEqual([row["replay_status"] for row in payloads], ["ingested", "ingested"])
            self.assertTrue(all(row["body_len"] > 0 for row in payloads))

    def test_openrouter_broadcast_rejects_invalid_auth(self):
        body = json.dumps(openrouter_trace_payload()).encode()
        with tempfile.TemporaryDirectory() as tmp:
            handler = object.__new__(app.ServerReceiver)
            handler.db_path = Path(tmp) / "server.sqlite"
            handler.app_config = app.AppConfig(
                openrouter_broadcast=app.OpenRouterBroadcastConfig(enabled=True, api_key="orb_secret")
            )
            handler.path = "/v1/traces"
            handler.headers = {
                "content-length": str(len(body)),
                "content-type": "application/json",
                "authorization": "Bearer wrong",
            }
            handler.rfile = io.BytesIO(body)
            handler.wfile = io.BytesIO()
            responses = []
            handler.send_response = responses.append
            handler.send_header = lambda _key, _value: None
            handler.end_headers = lambda: None

            handler.do_POST()

            self.assertEqual(responses, [401])

    def test_openrouter_broadcast_requires_configured_extra_header(self):
        body = json.dumps(openrouter_trace_payload()).encode()
        with tempfile.TemporaryDirectory() as tmp:
            handler = object.__new__(app.ServerReceiver)
            handler.db_path = Path(tmp) / "server.sqlite"
            handler.app_config = app.AppConfig(
                openrouter_broadcast=app.OpenRouterBroadcastConfig(
                    enabled=True,
                    api_key="orb_secret",
                    required_header_name="X-OpenRouter-Broadcast-Secret",
                    required_header_value="extra-secret",
                )
            )
            handler.path = "/v1/traces"
            handler.headers = {
                "content-length": str(len(body)),
                "content-type": "application/json",
                "authorization": "Bearer orb_secret",
            }
            handler.rfile = io.BytesIO(body)
            handler.wfile = io.BytesIO()
            responses = []
            handler.send_response = responses.append
            handler.send_header = lambda _key, _value: None
            handler.end_headers = lambda: None

            handler.do_POST()
            handler.headers = {
                "content-length": str(len(body)),
                "content-type": "application/json",
                "authorization": "Bearer orb_secret",
                "X-OpenRouter-Broadcast-Secret": "extra-secret",
            }
            handler.rfile = io.BytesIO(body)
            handler.wfile = io.BytesIO()
            handler.do_POST()

            self.assertEqual(responses, [401, 200])

    def test_replay_broadcast_payloads_is_idempotent_and_updates_metadata(self):
        body = json.dumps(openrouter_trace_payload()).encode()
        config = app.AppConfig(openrouter_broadcast=app.OpenRouterBroadcastConfig(enabled=True, api_key="orb_secret"))
        with tempfile.TemporaryDirectory() as tmp:
            con = app.connect_server(Path(tmp) / "server.sqlite")
            app.insert_broadcast_payload(con, "/v1/traces", "application/json", body, config, status="ingested")
            self.assertEqual(app.ingest_openrouter_broadcast(con, body, config), (1, 0))

            result = app.replay_broadcast_payloads(con, config, replay_status="ingested")
            con.commit()

            row_count = con.execute("select count(*) from usage_events").fetchone()[0]
            payload = con.execute("select replay_status, replayed_at, last_error from broadcast_payloads").fetchone()
            con.close()

            self.assertEqual(result, {"payloads": 1, "accepted": 0, "duplicates": 1, "errors": 0})
            self.assertEqual(row_count, 1)
            self.assertEqual(payload["replay_status"], "replayed")
            self.assertIsNotNone(payload["replayed_at"])
            self.assertIsNone(payload["last_error"])

    def test_replay_broadcast_payload_filters_normalize_dates(self):
        body = json.dumps(openrouter_trace_payload()).encode()
        config = app.AppConfig(openrouter_broadcast=app.OpenRouterBroadcastConfig(enabled=True, api_key="orb_secret"))
        with tempfile.TemporaryDirectory() as tmp:
            con = app.connect_server(Path(tmp) / "server.sqlite")
            app.insert_broadcast_payload(con, "/v1/traces", "application/json", body, config, status="ingested")
            con.execute(
                "update broadcast_payloads set received_at = ?",
                ("2026-05-04T01:02:03+00:00",),
            )

            result = app.replay_broadcast_payloads(
                con,
                config,
                since="2026-05-04T01:02:03Z",
                until="2026-05-04",
                replay_status="ingested",
            )
            con.close()

            self.assertEqual(result, {"payloads": 1, "accepted": 1, "duplicates": 0, "errors": 0})

    def test_openrouter_reserved_client_name_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = app.connect_server(Path(tmp) / "server.sqlite")
            with self.assertRaises(ValueError):
                app.create_client_token(con, "openrouter-broadcast", "OpenRouter")
            con.close()

    def test_collector_ingest_rejects_reserved_openrouter_client_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "server.sqlite"
            con = app.connect_server(db)
            con.execute(
                """
                insert into clients(client_name, display_name, token_hash, created_at, updated_at)
                values (?, ?, ?, ?, ?)
                """,
                ("openrouter-broadcast", "Bad", app.hash_token("bad-token"), app.now_iso(), app.now_iso()),
            )
            con.commit()
            con.close()
            body = json.dumps({"client_name": "openrouter-broadcast", "events": []}).encode()
            handler = object.__new__(app.ServerReceiver)
            handler.db_path = db
            handler.app_config = app.AppConfig()
            handler.path = "/api/v1/usage-events"
            handler.headers = {
                "content-length": str(len(body)),
                "content-type": "application/json",
                "authorization": "Bearer bad-token",
            }
            handler.rfile = io.BytesIO(body)
            handler.wfile = io.BytesIO()
            responses = []
            handler.send_response = responses.append
            handler.send_header = lambda _key, _value: None
            handler.end_headers = lambda: None

            handler.do_POST()

            self.assertEqual(responses, [400])

    def test_openrouter_broadcast_detects_orphaned_reserved_usage_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = app.connect_server(Path(tmp) / "server.sqlite")
            app.ingest_usage_events(
                con,
                "openrouter-broadcast",
                [
                    {
                        "client_event_id": "legacy-1",
                        "received_at": "2026-05-04T01:02:03+00:00",
                        "signal": "logs",
                        "model": "legacy",
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "total_tokens": 2,
                        "attributes_json": "{}",
                    }
                ],
            )
            con.commit()

            with self.assertRaises(ValueError):
                app.ensure_no_openrouter_client_conflict(con)
            con.close()

    def test_replay_broadcast_payloads_rejects_reserved_usage_conflict(self):
        body = json.dumps(openrouter_trace_payload()).encode()
        config = app.AppConfig(openrouter_broadcast=app.OpenRouterBroadcastConfig(enabled=True, api_key="orb_secret"))
        with tempfile.TemporaryDirectory() as tmp:
            con = app.connect_server(Path(tmp) / "server.sqlite")
            app.insert_broadcast_payload(con, "/v1/traces", "application/json", body, config, status="ingested")
            app.ingest_usage_events(
                con,
                "openrouter-broadcast",
                [
                    {
                        "client_event_id": "legacy-1",
                        "received_at": "2026-05-04T01:02:03+00:00",
                        "signal": "logs",
                        "model": "legacy",
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "total_tokens": 2,
                        "attributes_json": "{}",
                    }
                ],
            )
            con.commit()

            with self.assertRaises(ValueError):
                app.replay_broadcast_payloads(con, config, replay_status="ingested")
            con.close()

    def test_server_ingests_and_reports_tool_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "server.sqlite"
            con = app.connect_server(db)
            app.create_client_token(con, "laptop", "Laptop")
            event = {
                "client_tool_event_id": "tool-1",
                "received_at": "2026-05-04T01:02:03+00:00",
                "signal": "logs",
                "event_name": "codex.tool_result",
                "model": "gpt-test",
                "session_id": "s1",
                "tool_name": "exec_command",
                "call_id": "call-1",
                "success": "true",
                "duration_ms": 42,
                "mcp_server": "",
                "attributes_json": "{}",
            }

            self.assertEqual(app.ingest_tool_events(con, "laptop", [event]), (1, 0))
            self.assertEqual(app.ingest_tool_events(con, "laptop", [event]), (0, 1))
            con.commit()

            args = argparse.Namespace(
                group_by="client-tool",
                since=None,
                until=None,
                tool_name=None,
                session_id=None,
                client_name=None,
                event_name="codex.tool_result",
                limit=100,
            )
            rows = app.server_tool_report_rows(con, args)

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["client_name"], "Laptop")
            self.assertEqual(rows[0]["tool_name"], "exec_command")
            self.assertEqual(rows[0]["tool_events"], 1)
            self.assertEqual(rows[0]["successes"], 1)
            self.assertEqual(rows[0]["total_duration_ms"], 42)
            self.assertEqual(app.server_stats_dict(con)["tool_events"], 1)
            con.close()

    def test_server_report_api_aggregates_across_clients(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "server.sqlite"
            con = app.connect_server(db)
            for client in ("laptop", "desktop"):
                app.create_client_token(con, client, client.title())
                app.ingest_usage_events(
                    con,
                    client,
                    [
                        {
                            "client_event_id": f"{client}-1",
                            "received_at": "2026-05-04T01:02:03+00:00",
                            "signal": "logs",
                            "model": "gpt-test",
                            "input_tokens": 1,
                            "output_tokens": 2,
                            "total_tokens": 3,
                            "attributes_json": "{}",
                        }
                    ],
                )
            con.commit()
            args = argparse.Namespace(
                group_by="client",
                since=None,
                until=None,
                model=None,
                session_id=None,
                client_name=None,
                source_kind=None,
                workspace_label=None,
                api_key_label=None,
                provider_name=None,
                limit=100,
            )
            rows = app.server_report_rows(con, args)
            totals = {row["client_name"]: row["total_tokens"] for row in rows}
            self.assertEqual(totals, {"Laptop": 3, "Desktop": 3})
            con.close()

    def test_reports_page_renders_usage_totals(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "server.sqlite"
            con = app.connect_server(db)
            app.create_client_token(con, "laptop", "Laptop")
            app.ingest_usage_events(
                con,
                "laptop",
                [
                    {
                        "client_event_id": "laptop-1",
                        "received_at": "2026-05-04T01:02:03+00:00",
                        "signal": "logs",
                        "model": "gpt-test",
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "total_tokens": 15,
                        "attributes_json": "{}",
                    }
                ],
            )
            con.commit()

            body = app.ServerReceiver.render_reports(
                object(),
                con,
                {},
            )

            self.assertIn("Usage Reports", body)
            self.assertSharedNav(body, "usage")
            self.assertIn('<option value="client-model" selected>client-model</option>', body)
            self.assertIn("/reports?group_by=workspace&source_kind=openrouter_broadcast", body)
            self.assertIn("/reports?group_by=api-key&source_kind=openrouter_broadcast", body)
            self.assertIn("<td>Laptop</td>", body)
            self.assertNotIn("<td>laptop</td>", body)
            self.assertIn("<td>gpt-test</td>", body)
            self.assertIn("<td class=\"num\">15</td>", body)
            self.assertIn('data-utc="2026-05-04T01:02:03+00:00"', body)
            self.assertIn("formatBrowserTimes", body)
            con.close()

    def test_server_reports_group_openrouter_by_workspace(self):
        body = json.dumps(openrouter_trace_payload()).encode()
        config = app.AppConfig(openrouter_broadcast=app.OpenRouterBroadcastConfig(enabled=True, api_key="orb_secret"))
        with tempfile.TemporaryDirectory() as tmp:
            con = app.connect_server(Path(tmp) / "server.sqlite")
            self.assertEqual(app.ingest_openrouter_broadcast(con, body, config), (1, 0))
            con.commit()
            args = argparse.Namespace(
                group_by="workspace",
                since=None,
                until=None,
                model=None,
                session_id=None,
                client_name=None,
                source_kind="openrouter_broadcast",
                workspace_label=None,
                api_key_label=None,
                provider_name=None,
                limit=100,
            )
            rows = app.server_report_rows(con, args)
            con.close()

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["workspace_label"], "agents")
            self.assertEqual(rows[0]["total_tokens"], 17)
            self.assertAlmostEqual(rows[0]["cost_value"], 0.0123)
            self.assertEqual(rows[0]["cost_unit"], "credits")

    def test_server_reports_do_not_mix_cost_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = app.connect_server(Path(tmp) / "server.sqlite")
            for unit in ("credits", "USD"):
                app.ingest_usage_events(
                    con,
                    "client-a",
                    [
                        {
                            "client_event_id": f"evt-{unit}",
                            "received_at": "2026-05-04T01:02:03+00:00",
                            "signal": "traces",
                            "model": "gpt-test",
                            "input_tokens": 1,
                            "output_tokens": 1,
                            "total_tokens": 2,
                            "cost_value": 1.5,
                            "cost_unit": unit,
                            "attributes_json": "{}",
                        }
                    ],
                )
            con.commit()
            args = argparse.Namespace(
                group_by="client",
                since=None,
                until=None,
                model=None,
                session_id=None,
                client_name=None,
                source_kind=None,
                workspace_label=None,
                api_key_label=None,
                provider_name=None,
                limit=100,
            )
            rows = app.server_report_rows(con, args)
            con.close()

            self.assertEqual({row["cost_unit"] for row in rows}, {"credits", "USD"})
            self.assertTrue(all(row["cost_value"] == 1.5 for row in rows))

    def test_server_reports_do_not_split_hidden_cost_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = app.connect_server(Path(tmp) / "server.sqlite")
            for unit in ("credits", "USD"):
                app.ingest_usage_events(
                    con,
                    "client-a",
                    [
                        {
                            "client_event_id": f"hidden-cost-{unit}",
                            "received_at": "2026-05-04T01:02:03+00:00",
                            "signal": "traces",
                            "model": "gpt-test",
                            "session_id": "session-1",
                            "input_tokens": 1,
                            "output_tokens": 1,
                            "total_tokens": 2,
                            "cost_value": 1.5,
                            "cost_unit": unit,
                            "attributes_json": "{}",
                        }
                    ],
                )
            con.commit()
            base = {
                "since": None,
                "until": None,
                "model": None,
                "session_id": None,
                "client_name": None,
                "source_kind": None,
                "workspace_label": None,
                "api_key_label": None,
                "provider_name": None,
                "limit": 100,
            }

            for group_by in ("total", "model", "session"):
                rows = app.server_report_rows(con, argparse.Namespace(group_by=group_by, **base))
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["total_tokens"], 4)
                self.assertIsNone(rows[0]["cost_value"])
                self.assertEqual(rows[0]["cost_unit"], "mixed")
            con.close()

    def test_server_report_filters_preserve_subsecond_precision(self):
        body = json.dumps(openrouter_trace_payload_with_time(1777856523123456000)).encode()
        config = app.AppConfig(openrouter_broadcast=app.OpenRouterBroadcastConfig(enabled=True, api_key="orb_secret"))
        with tempfile.TemporaryDirectory() as tmp:
            con = app.connect_server(Path(tmp) / "server.sqlite")
            app.ingest_openrouter_broadcast(con, body, config)
            con.commit()

            before = argparse.Namespace(
                group_by="workspace",
                since="2026-05-04T01:02:03.500000Z",
                until=None,
                model=None,
                session_id=None,
                client_name=None,
                source_kind="openrouter_broadcast",
                workspace_label=None,
                api_key_label=None,
                provider_name=None,
                limit=100,
            )
            through = argparse.Namespace(
                group_by="workspace",
                since=None,
                until="2026-05-04T01:02:03.500000Z",
                model=None,
                session_id=None,
                client_name=None,
                source_kind="openrouter_broadcast",
                workspace_label=None,
                api_key_label=None,
                provider_name=None,
                limit=100,
            )

            self.assertEqual(app.server_report_rows(con, before), [])
            self.assertEqual(len(app.server_report_rows(con, through)), 1)
            con.close()

    def test_date_only_until_includes_subsecond_end_of_day(self):
        body = json.dumps(openrouter_trace_payload_with_time(1777939199123456000)).encode()
        config = app.AppConfig(openrouter_broadcast=app.OpenRouterBroadcastConfig(enabled=True, api_key="orb_secret"))
        with tempfile.TemporaryDirectory() as tmp:
            con = app.connect_server(Path(tmp) / "server.sqlite")
            app.ingest_openrouter_broadcast(con, body, config)
            con.commit()
            args = argparse.Namespace(
                group_by="workspace",
                since=None,
                until="2026-05-04",
                model=None,
                session_id=None,
                client_name=None,
                source_kind="openrouter_broadcast",
                workspace_label=None,
                api_key_label=None,
                provider_name=None,
                limit=100,
            )

            self.assertEqual(len(app.server_report_rows(con, args)), 1)
            con.close()

    def test_tool_reports_page_renders_tool_totals(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "server.sqlite"
            con = app.connect_server(db)
            app.create_client_token(con, "laptop", "Laptop")
            app.ingest_tool_events(
                con,
                "laptop",
                [
                    {
                        "client_tool_event_id": "tool-1",
                        "received_at": "2026-05-04T01:02:03+00:00",
                        "signal": "logs",
                        "event_name": "codex.tool_result",
                        "tool_name": "exec_command",
                        "success": "true",
                        "duration_ms": 42,
                        "attributes_json": "{}",
                    }
                ],
            )
            con.commit()

            body = app.ServerReceiver.render_tool_reports(
                object(),
                con,
                {},
            )

            self.assertIn("Tool Reports", body)
            self.assertSharedNav(body, "tools")
            self.assertIn('<option value="client-tool" selected>client-tool</option>', body)
            self.assertIn("Grouped totals", body)
            self.assertIn("Recent tool calls", body)
            self.assertIn('class="status ok"', body)
            self.assertIn("<td>Laptop</td>", body)
            self.assertNotIn("<td>laptop</td>", body)
            self.assertIn("<td>exec_command</td>", body)
            self.assertIn("<td class=\"num\">42</td>", body)
            self.assertIn('data-utc="2026-05-04T01:02:03+00:00"', body)
            self.assertIn("Intl.DateTimeFormat", body)
            con.close()

    def test_tool_reports_can_include_decisions_and_results(self):
        args = app.ServerReceiver.tool_reports_args({"event_name": [""], "group_by": ["event"]})

        self.assertEqual(args.event_name, "")
        self.assertEqual(args.group_by, "event")

    def test_server_supports_client_model_grouping(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "server.sqlite"
            con = app.connect_server(db)
            for client, model, total in (
                ("laptop", "gpt-test", 3),
                ("laptop", "gpt-test", 4),
                ("desktop", "gpt-test", 5),
            ):
                if not con.execute(
                    "select 1 from clients where client_name = ?",
                    (client,),
                ).fetchone():
                    app.create_client_token(con, client, client.title())
                app.ingest_usage_events(
                    con,
                    client,
                    [
                        {
                            "client_event_id": f"{client}-{total}",
                            "received_at": "2026-05-04T01:02:03+00:00",
                            "signal": "logs",
                            "model": model,
                            "total_tokens": total,
                            "attributes_json": "{}",
                        }
                    ],
                )
            con.commit()

            args = argparse.Namespace(
                group_by="client-model",
                since=None,
                until=None,
                model=None,
                session_id=None,
                client_name=None,
                limit=100,
            )
            rows = app.server_report_rows(con, args)
            totals = {(row["client_name"], row["model"]): row["total_tokens"] for row in rows}

            self.assertEqual(totals, {("Laptop", "gpt-test"): 7, ("Desktop", "gpt-test"): 5})
            self.assertEqual(app.server_default_columns("client-model")[:2], ("client_name", "model"))
            con.close()

    def test_admin_ui_creates_renames_revokes_and_hashes_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "server.sqlite"
            con = app.connect_server(db)
            token = app.create_client_token(con, "laptop", "Laptop")
            body = app.ServerReceiver.render_admin(object(), con, token=token)
            self.assertSharedNav(body, "admin")
            self.assertIn("New token, shown once", body)
            self.assertIn(token, body)
            self.assertIn('class="browser-time" data-utc=', body)

            row = con.execute("select display_name, token_hash, revoked_at from clients where client_name = 'laptop'").fetchone()
            self.assertEqual(row["display_name"], "Laptop")
            self.assertNotEqual(row["token_hash"], token)
            self.assertEqual(row["token_hash"], app.hash_token(token))

            app.rename_client(con, "laptop", "Work Laptop")
            app.revoke_client(con, "laptop")
            con.commit()
            row = con.execute("select display_name, revoked_at from clients where client_name = 'laptop'").fetchone()
            self.assertEqual(row["display_name"], "Work Laptop")
            self.assertIsNotNone(row["revoked_at"])
            con.close()

    def test_only_revoked_clients_can_be_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "server.sqlite"
            con = app.connect_server(db)
            app.create_client_token(con, "active", "Active")
            app.create_client_token(con, "revoked", "Revoked")
            con.commit()

            active_body = app.ServerReceiver.render_admin(object(), con)
            self.assertIn('action="/admin/clients/revoke"', active_body)
            self.assertNotIn('action="/admin/clients/delete"', active_body)
            self.assertFalse(app.delete_revoked_client(con, "active"))

            app.revoke_client(con, "revoked")
            con.commit()
            revoked_body = app.ServerReceiver.render_admin(object(), con)
            self.assertIn('action="/admin/clients/delete"', revoked_body)
            self.assertTrue(app.delete_revoked_client(con, "revoked"))
            con.commit()

            clients = [row["client_name"] for row in con.execute("select client_name from clients order by client_name")]
            self.assertEqual(clients, ["active"])
            con.close()


if __name__ == "__main__":
    unittest.main()
