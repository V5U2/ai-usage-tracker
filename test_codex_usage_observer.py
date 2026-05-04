import argparse
import json
import tempfile
import unittest
from pathlib import Path

import codex_usage_observer as app


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


if __name__ == "__main__":
    unittest.main()
