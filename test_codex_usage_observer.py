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

    def test_config_can_omit_raw_payload_body(self):
        config = app.AppConfig(storage=app.StorageConfig(raw_payload_body=False))
        with tempfile.TemporaryDirectory() as tmp:
            con = app.connect(Path(tmp) / "usage.sqlite")
            raw_id = app.insert_payload(con, "/v1/logs", "application/json", b'{"secret": true}', config)
            body = con.execute("select body from raw_payloads where id = ?", (raw_id,)).fetchone()["body"]

            self.assertEqual(bytes(body), b"")
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
            self.assertEqual(config.server.batch_size, 23)
            self.assertEqual(config.server.timeout_seconds, 7)
            self.assertEqual(config.central.host, "0.0.0.0")
            self.assertEqual(config.central.port, 18418)
            self.assertEqual(config.central.db, "aggregation.sqlite")
            self.assertEqual(config.central.admin_api_key, "admin")

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
            payload = log_payload(
                {
                    "event.name": "response.completed",
                    "model": "gpt-test",
                    "input_tokens": 4,
                    "output_tokens": 6,
                }
            )
            raw_id = app.insert_payload(con, "/v1/logs", "application/json", json.dumps(payload).encode())
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


class ServerHttpTests(unittest.TestCase):
    def assertSharedNav(self, body, active):
        expected = {
            "admin": '<nav><a href="/admin" class="active">Admin</a><a href="/reports">Usage</a><a href="/tools">Tools</a></nav>',
            "usage": '<nav><a href="/admin">Admin</a><a href="/reports" class="active">Usage</a><a href="/tools">Tools</a></nav>',
            "tools": '<nav><a href="/admin">Admin</a><a href="/reports">Usage</a><a href="/tools" class="active">Tools</a></nav>',
        }[active]
        self.assertIn(expected, body)
        self.assertIn('href="/admin"', body)
        self.assertIn('href="/reports"', body)
        self.assertIn('href="/tools"', body)

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
            self.assertIn("<td>Laptop</td>", body)
            self.assertNotIn("<td>laptop</td>", body)
            self.assertIn("<td>gpt-test</td>", body)
            self.assertIn("<td class=\"num\">15</td>", body)
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
