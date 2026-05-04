import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import codex_usage_observer as app
import codex_usage_tracker.core as core


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

    def test_serve_payload_log_line_includes_ingestion_details(self):
        line = app.serve_payload_log_line(
            path="/v1/logs",
            content_type="application/json",
            body_bytes=1536,
            shape="resourceLogs",
            raw_body_retained=False,
            events=2,
            raw_id=42,
        )

        self.assertIn("received /v1/logs", line)
        self.assertIn("payload=1.5 KiB", line)
        self.assertIn("shape=resourceLogs", line)
        self.assertIn("content_type=application/json", line)
        self.assertIn("raw_body=metadata-only", line)
        self.assertIn("usage_events=2", line)
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
            raw_count, inserted = app.reindex_database(con, config)
            for preserved_raw_id, received_at, event in preserved_events:
                app.insert_usage(con, preserved_raw_id, received_at, [event])
            cleared_payloads = app.cleanup_stored_data(con, config)
            con.commit()

            events = {row["model"]: row for row in con.execute("select * from usage_events").fetchall()}
            body = con.execute("select body from raw_payloads where id = ?", (raw_id,)).fetchone()["body"]

            self.assertEqual(raw_count, 2)
            self.assertEqual(inserted, 1)
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
    def test_sync_marks_successful_rows_synced(self):
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
                        "cached_tokens": 0,
                        "reasoning_tokens": 0,
                        "attributes_json": "{}",
                    }
                ],
            )
            config = app.AppConfig(
                server=app.RemoteServerConfig(endpoint="http://server", api_key="secret")
            )

            with patch.object(core, "post_usage_batch", return_value={"accepted": 1, "duplicates": 0}):
                attempted, synced, error = app.sync_pending_usage(con, config)

            row = con.execute("select synced_at, last_sync_error from usage_events").fetchone()
            self.assertEqual(attempted, 1)
            self.assertEqual(synced, 1)
            self.assertIsNone(error)
            self.assertIsNotNone(row["synced_at"])
            self.assertIsNone(row["last_sync_error"])
            con.close()

    def test_sync_failure_leaves_rows_queued(self):
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
                        "cached_tokens": 0,
                        "reasoning_tokens": 0,
                        "attributes_json": "{}",
                    }
                ],
            )
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


class ServerHttpTests(unittest.TestCase):
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
            self.assertEqual(totals, {"laptop": 3, "desktop": 3})
            con.close()

    def test_admin_ui_creates_renames_revokes_and_hashes_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "server.sqlite"
            con = app.connect_server(db)
            token = app.create_client_token(con, "laptop", "Laptop")
            body = app.ServerReceiver.render_admin(object(), con, token=token)
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


if __name__ == "__main__":
    unittest.main()
