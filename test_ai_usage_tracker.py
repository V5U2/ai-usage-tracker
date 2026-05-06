import argparse
import datetime as dt
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import ai_usage_tracker as app
import ai_usage_tracker.aggregation_server as aggregation_server
import ai_usage_tracker.client as client_module
import ai_usage_tracker.collector as collector
import ai_usage_tracker.core as core
import ai_usage_tracker.server as server_module


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


def claude_code_metric_payload(points):
    return {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "claude-code"}},
                        {"key": "session.id", "value": {"stringValue": "claude-session-1"}},
                    ]
                },
                "scopeMetrics": [
                    {
                        "metrics": [
                            {
                                "name": name,
                                "sum": {
                                    "dataPoints": [
                                        {
                                            "asDouble" if isinstance(value, float) else "asInt": str(value),
                                            "attributes": [
                                                {"key": key, "value": {"stringValue": str(attr_value)}}
                                                for key, attr_value in attrs.items()
                                            ],
                                        }
                                    ]
                                },
                            }
                            for name, value, attrs in points
                        ]
                    }
                ],
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

    def test_component_alias_modules_expose_expected_surfaces(self):
        self.assertIs(client_module.Receiver, collector.Receiver)
        self.assertIs(server_module.ServerReceiver, aggregation_server.ServerReceiver)

    def test_version_commands_print_package_version(self):
        for argv in (
            ["ai_usage_tracker.py", "version"],
            ["ai_usage_tracker.py", "client", "version"],
            ["ai_usage_tracker.py", "server", "version"],
        ):
            with self.subTest(argv=argv):
                out = io.StringIO()
                with patch.object(app.sys, "argv", argv), redirect_stdout(out):
                    self.assertEqual(app.main(), 0)
                self.assertEqual(out.getvalue().strip(), app.version_text())

    def test_version_text_hides_commit_for_plain_releases(self):
        self.assertEqual(app.version_text("0.4.0", "abcdef1234567890"), "AI Usage Tracker - v0.4.0")
        self.assertEqual(app.version_text("v0.4.0", "abcdef1234567890"), "AI Usage Tracker - v0.4.0")

    def test_version_text_shows_commit_for_edge_versions(self):
        self.assertEqual(app.version_text("edge", "abcdef1234567890"), "AI Usage Tracker - edge - (abcdef123456)")
        self.assertEqual(app.version_text("0.4.0-edge", "abcdef1234567890"), "AI Usage Tracker - v0.4.0-edge - (abcdef123456)")

    def test_default_paths_use_generic_ai_usage_names(self):
        self.assertEqual(core.DEFAULT_DB, Path(os.environ.get("AI_USAGE_DB") or "ai_usage.sqlite"))
        self.assertEqual(core.DEFAULT_SERVER_DB, Path(os.environ.get("AI_USAGE_SERVER_DB") or "ai_usage_server.sqlite"))
        self.assertEqual(core.DEFAULT_CONFIG, Path(os.environ.get("AI_USAGE_CONFIG") or "ai_usage_tracker.toml"))

    def test_blank_env_values_fall_back_to_generic_defaults(self):
        with patch.dict(
            os.environ,
            {
                "AI_USAGE_DB": "",
                "AI_USAGE_SERVER_DB": "",
                "AI_USAGE_CONFIG": "",
                "AI_USAGE_MAX_BODY_BYTES": "",
            },
        ):
            self.assertEqual(core.env_value("AI_USAGE_DB", "ai_usage.sqlite"), "ai_usage.sqlite")
            self.assertEqual(core.env_value("AI_USAGE_SERVER_DB", "ai_usage_server.sqlite"), "ai_usage_server.sqlite")
            self.assertEqual(core.env_value("AI_USAGE_CONFIG", "ai_usage_tracker.toml"), "ai_usage_tracker.toml")
            self.assertEqual(core.env_value("AI_USAGE_MAX_BODY_BYTES", "52428800"), "52428800")


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

    def test_can_estimate_openai_api_cost_when_enabled(self):
        config = app.AppConfig(
            pricing=app.PricingConfig(
                estimate_openai_api_costs=True,
                include_reasoning_tokens_as_output=True,
            )
        )

        event = app.usage_from_attrs(
            "logs",
            "response.completed",
            {
                "model": "gpt-5.4-mini",
                "input_tokens": "1000000",
                "cached_tokens": "250000",
                "output_tokens": "100000",
                "reasoning_tokens": "50000",
            },
            config,
        )

        self.assertIsNotNone(event)
        self.assertAlmostEqual(event["cost_value"], 1.25625)
        self.assertEqual(event["cost_unit"], "USD")

    def test_openai_api_cost_estimation_is_opt_in_and_preserves_reported_cost(self):
        event = app.usage_from_attrs(
            "logs",
            "response.completed",
            {
                "model": "gpt-5.4-mini",
                "input_tokens": "1000000",
                "output_tokens": "100000",
            },
        )
        self.assertEqual(event["cost_value"], 0)
        self.assertIsNone(event["cost_unit"])

        config = app.AppConfig(pricing=app.PricingConfig(estimate_openai_api_costs=True))
        reported = app.usage_from_attrs(
            "logs",
            "response.completed",
            {
                "model": "gpt-5.4-mini",
                "input_tokens": "1000000",
                "output_tokens": "100000",
                "cost_usd": "0.42",
            },
            config,
        )
        self.assertEqual(reported["cost_value"], 0.42)
        self.assertEqual(reported["cost_unit"], "USD")

    def test_openai_api_cost_estimation_preserves_reported_zero_cost(self):
        config = app.AppConfig(pricing=app.PricingConfig(estimate_openai_api_costs=True))

        event = app.usage_from_attrs(
            "logs",
            "response.completed",
            {
                "model": "gpt-5.4-mini",
                "input_tokens": "1000000",
                "output_tokens": "100000",
                "cost_usd": "0",
            },
            config,
        )

        self.assertEqual(event["cost_value"], 0)
        self.assertEqual(event["cost_unit"], "USD")

    def test_backfill_missing_costs_estimates_existing_openai_rows(self):
        config = app.AppConfig(pricing=app.PricingConfig(estimate_openai_api_costs=True))
        with tempfile.TemporaryDirectory() as tmp:
            con = app.connect(Path(tmp) / "usage.sqlite")
            con.execute(
                """
                insert into raw_payloads(received_at, path, content_type, body)
                values (?, ?, ?, X'')
                """,
                ("2026-05-04T01:02:03+00:00", "/v1/logs", "application/json"),
            )
            app.insert_usage(
                con,
                1,
                "2026-05-04T01:02:03+00:00",
                [
                    {
                        "signal": "logs",
                        "event_name": "response.completed",
                        "model": "gpt-5.4-mini",
                        "session_id": None,
                        "thread_id": None,
                        "input_tokens": 1000000,
                        "output_tokens": 100000,
                        "total_tokens": 1100000,
                        "cached_tokens": 0,
                        "reasoning_tokens": 0,
                        "cost_value": 0,
                        "cost_unit": None,
                        "attributes_json": "{}",
                    }
                ],
            )

            updated = app.backfill_missing_costs(con, config)
            row = con.execute("select cost_value, cost_unit from usage_events").fetchone()
            con.close()

        self.assertEqual(updated, 1)
        self.assertAlmostEqual(row["cost_value"], 1.2)
        self.assertEqual(row["cost_unit"], "USD")

    def test_backfill_missing_costs_preserves_reported_zero_unit(self):
        config = app.AppConfig(pricing=app.PricingConfig(estimate_openai_api_costs=True))
        with tempfile.TemporaryDirectory() as tmp:
            con = app.connect(Path(tmp) / "usage.sqlite")
            con.execute(
                """
                insert into raw_payloads(received_at, path, content_type, body)
                values (?, ?, ?, X'')
                """,
                ("2026-05-04T01:02:03+00:00", "/v1/logs", "application/json"),
            )
            app.insert_usage(
                con,
                1,
                "2026-05-04T01:02:03+00:00",
                [
                    {
                        "signal": "logs",
                        "event_name": "response.completed",
                        "model": "gpt-5.4-mini",
                        "session_id": None,
                        "thread_id": None,
                        "input_tokens": 1000000,
                        "output_tokens": 100000,
                        "total_tokens": 1100000,
                        "cached_tokens": 0,
                        "reasoning_tokens": 0,
                        "cost_value": 0,
                        "cost_unit": "USD",
                        "attributes_json": "{}",
                    }
                ],
            )

            updated = app.backfill_missing_costs(con, config)
            row = con.execute("select cost_value, cost_unit from usage_events").fetchone()
            con.close()

        self.assertEqual(updated, 0)
        self.assertEqual(row["cost_value"], 0)
        self.assertEqual(row["cost_unit"], "USD")

    def test_openai_api_cost_estimation_does_not_require_model_storage(self):
        config = app.AppConfig(
            storage=app.StorageConfig(model=False),
            pricing=app.PricingConfig(estimate_openai_api_costs=True),
        )

        event = app.usage_from_attrs(
            "logs",
            "response.completed",
            {
                "model": "gpt-5.4-mini",
                "input_tokens": "1000000",
                "output_tokens": "100000",
            },
            config,
        )

        self.assertIsNone(event["model"])
        self.assertAlmostEqual(event["cost_value"], 1.2)
        self.assertEqual(event["cost_unit"], "USD")

    def test_can_estimate_claude_api_cost_when_enabled(self):
        config = app.AppConfig(pricing=app.PricingConfig(estimate_claude_api_costs=True))

        event = app.usage_from_attrs(
            "logs",
            "response.completed",
            {
                "model": "claude-sonnet-4-5-20250929",
                "input_tokens": "1000000",
                "cache_read_input_tokens": "250000",
                "cache_creation_input_tokens": "100000",
                "output_tokens": "100000",
            },
            config,
        )

        self.assertIsNotNone(event)
        self.assertAlmostEqual(event["cost_value"], 3.9)
        self.assertEqual(event["cost_unit"], "USD")

    def test_claude_api_cost_estimation_matches_hyphenated_model_family(self):
        config = app.AppConfig(pricing=app.PricingConfig(estimate_claude_api_costs=True))

        event = app.usage_from_attrs(
            "logs",
            "response.completed",
            {
                "model": "claude-haiku-4-5-20251001",
                "input_tokens": "1000000",
                "output_tokens": "100000",
            },
            config,
        )

        self.assertIsNotNone(event)
        self.assertAlmostEqual(event["cost_value"], 1.5)
        self.assertEqual(event["cost_unit"], "USD")

    def test_claude_code_token_metric_does_not_estimate_duplicate_cost(self):
        config = app.AppConfig(pricing=app.PricingConfig(estimate_claude_api_costs=True))

        event = app.usage_from_attrs(
            "metrics",
            "claude_code.token.usage",
            {
                "model": "claude-haiku-4-5-20251001",
                "input_tokens": "1000000",
                "output_tokens": "100000",
            },
            config,
        )

        self.assertIsNotNone(event)
        self.assertEqual(event["cost_value"], 0)
        self.assertIsNone(event["cost_unit"])

    def test_friendly_model_name_uses_matched_claude_price_family(self):
        self.assertEqual(
            app.friendly_model_name("au.anthropic.claude-sonnet-4-5-20250929-v1:0"),
            "claude-sonnet-4.5",
        )
        self.assertEqual(app.friendly_model_name("claude-sonnet-4-5-20250929"), "claude-sonnet-4.5")

    def test_claude_api_cost_estimation_is_opt_in_and_preserves_reported_cost(self):
        event = app.usage_from_attrs(
            "metrics",
            "claude_code.token.usage",
            {
                "model": "claude-haiku-4-5-20251001",
                "input_tokens": "1000000",
                "output_tokens": "100000",
            },
        )
        self.assertEqual(event["cost_value"], 0)
        self.assertIsNone(event["cost_unit"])

        config = app.AppConfig(pricing=app.PricingConfig(estimate_claude_api_costs=True))
        reported = app.usage_from_attrs(
            "metrics",
            "claude_code.token.usage",
            {
                "model": "claude-haiku-4-5-20251001",
                "input_tokens": "1000000",
                "output_tokens": "100000",
                "cost_usd": "0.19",
            },
            config,
        )
        self.assertEqual(reported["cost_value"], 0.19)
        self.assertEqual(reported["cost_unit"], "USD")

    def test_extracts_observed_openrouter_broadcast_fields(self):
        body = json.dumps(observed_openrouter_trace_payload()).encode()
        config = app.AppConfig(openrouter_broadcast=app.OpenRouterBroadcastConfig(enabled=True, api_key="orb_secret"))

        event = app.normalize_openrouter_broadcast(body, config)[0]

        self.assertEqual(event["model"], "moonshotai/kimi-k2.5")
        self.assertEqual(event["total_tokens"], 15111)
        self.assertAlmostEqual(event["cost_value"], 0.00639196)
        self.assertEqual(event["cost_unit"], "USD")
        self.assertIsNone(event["workspace_label"])
        self.assertEqual(event["api_key_label"], "prod-key")
        self.assertEqual(event["provider_name"], "Chutes")
        attrs = json.loads(event["attributes_json"])
        self.assertEqual(attrs["trace.metadata.openrouter.entity_id"], "[redacted]")

    def test_openrouter_source_label_falls_back_to_api_key_name(self):
        body = json.dumps(observed_openrouter_trace_payload()).encode()
        config = app.AppConfig(openrouter_broadcast=app.OpenRouterBroadcastConfig(enabled=True, api_key="orb_secret"))
        with tempfile.TemporaryDirectory() as tmp:
            con = app.connect_server(Path(tmp) / "server.sqlite")
            self.assertEqual(app.ingest_openrouter_broadcast(con, body, config), (1, 0))
            con.commit()
            args = argparse.Namespace(
                group_by="provider-source-model",
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

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["source_provider"], "OpenRouter")
            self.assertEqual(rows[0]["source_label"], "prod-key")
            self.assertEqual(rows[0]["model"], "moonshotai/kimi-k2.5")

    def test_extracts_claude_code_token_and_cost_metrics(self):
        payload = claude_code_metric_payload(
            [
                ("claude_code.token.usage", 9, {"type": "input", "model": "claude-opus-4-20250514"}),
                ("claude_code.token.usage", 4, {"type": "output", "model": "claude-opus-4-20250514"}),
                ("claude_code.token.usage", 3, {"type": "cacheRead", "model": "claude-opus-4-20250514"}),
                ("claude_code.cost.usage", 0.012, {"model": "claude-opus-4-20250514"}),
            ]
        )

        events = app.extract_usage("/v1/metrics", json.dumps(payload).encode())

        self.assertEqual(len(events), 4)
        by_name_and_type = {(event["event_name"], json.loads(event["attributes_json"]).get("type")): event for event in events}
        input_event = by_name_and_type[("claude_code.token.usage", "input")]
        output_event = by_name_and_type[("claude_code.token.usage", "output")]
        cache_event = by_name_and_type[("claude_code.token.usage", "cacheRead")]
        cost_event = by_name_and_type[("claude_code.cost.usage", None)]
        self.assertEqual(input_event["model"], "claude-opus-4-20250514")
        self.assertEqual(input_event["session_id"], "claude-session-1")
        self.assertEqual(input_event["input_tokens"], 9)
        self.assertEqual(input_event["total_tokens"], 9)
        self.assertEqual(output_event["output_tokens"], 4)
        self.assertEqual(output_event["total_tokens"], 4)
        self.assertEqual(cache_event["cached_tokens"], 3)
        self.assertEqual(cache_event["total_tokens"], 0)
        self.assertEqual(cost_event["cost_value"], 0.012)
        self.assertEqual(cost_event["cost_unit"], "USD")

    def test_claude_code_logs_and_traces_are_not_counted_as_usage(self):
        log_body = json.dumps(
            log_payload(
                {
                    "service.name": "claude-code",
                    "event.name": "api_request",
                    "model": "claude-sonnet-4-5-20250929",
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "cost_usd": 0.1,
                }
            )
        ).encode()
        trace_body = json.dumps(
            {
                "resourceSpans": [
                    {
                        "resource": {
                            "attributes": [{"key": "service.name", "value": {"stringValue": "claude-code"}}]
                        },
                        "scopeSpans": [
                            {
                                "spans": [
                                    {
                                        "traceId": "trace-1",
                                        "spanId": "span-1",
                                        "name": "claude_code.llm_request",
                                        "attributes": [
                                            {"key": "model", "value": {"stringValue": "claude-sonnet-4-5-20250929"}},
                                            {"key": "input_tokens", "value": {"intValue": "10"}},
                                            {"key": "output_tokens", "value": {"intValue": "2"}},
                                            {"key": "cost_usd", "value": {"doubleValue": "0.1"}},
                                        ],
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }
        ).encode()

        self.assertEqual(app.extract_usage("/v1/logs", log_body), [])
        self.assertEqual(app.extract_usage("/v1/traces", trace_body), [])

    def test_log_record_top_level_trace_context_is_extracted(self):
        payload = log_payload(
            {
                "event.name": "response.completed",
                "model": "gpt-test",
                "input_tokens": 1,
            }
        )
        record = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
        record["traceId"] = "trace-top"
        record["spanId"] = "span-top"

        event = app.extract_usage("/v1/logs", json.dumps(payload).encode())[0]

        self.assertEqual(event["trace_id"], "trace-top")
        self.assertEqual(event["span_id"], "span-top")

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

    def test_extracts_claude_code_tool_events(self):
        payload = log_payload(
            {
                "event.name": "tool_result",
                "model": "claude-opus-4-20250514",
                "tool_name": "Bash",
                "tool_use_id": "toolu_1",
                "success": "true",
                "duration_ms": "125",
            }
        )

        events = app.extract_tool_events("/v1/logs", json.dumps(payload).encode())

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_name"], "tool_result")
        self.assertEqual(events[0]["model"], "claude-opus-4-20250514")
        self.assertEqual(events[0]["tool_name"], "Bash")
        self.assertEqual(events[0]["call_id"], "toolu_1")
        self.assertEqual(events[0]["duration_ms"], 125)

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

    def test_load_config_reads_pricing_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                """
[pricing]
estimate_openai_api_costs = true
estimate_claude_api_costs = true
include_reasoning_tokens_as_output = false
report_openrouter_credits_as_usd = true
""",
                encoding="utf-8",
            )

            config = app.load_config(config_path)

            self.assertTrue(config.pricing.estimate_openai_api_costs)
            self.assertTrue(config.pricing.estimate_claude_api_costs)
            self.assertFalse(config.pricing.include_reasoning_tokens_as_output)
            self.assertTrue(config.pricing.report_openrouter_credits_as_usd)

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

    def test_claude_code_retry_deduplication_uses_trace_span_ids(self):
        """Test that Claude Code events with trace_id+span_id deduplicate retries."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "usage.sqlite"
            con = app.connect(db)
            app.ensure_usage_metadata_schema(con)
            app.ensure_client_sync_schema(con)

            # Simulate Claude Code sending an event with trace_id and span_id
            payload = {
                "resourceMetrics": [
                    {
                        "resource": {
                            "attributes": [
                                {"key": "session_id", "value": {"stringValue": "claude-session-1"}},
                                {"key": "trace_id", "value": {"stringValue": "abc123"}},
                            ]
                        },
                        "scopeMetrics": [
                            {
                                "metrics": [
                                    {
                                        "name": "claude_code.token.usage",
                                        "sum": {
                                            "dataPoints": [
                                                {
                                                    "attributes": [
                                                        {"key": "type", "value": {"stringValue": "input"}},
                                                        {"key": "model", "value": {"stringValue": "claude-sonnet-4.5"}},
                                                        {"key": "trace_id", "value": {"stringValue": "abc123"}},
                                                        {"key": "span_id", "value": {"stringValue": "def456"}},
                                                    ],
                                                    "asInt": 100,
                                                }
                                            ]
                                        },
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }

            # Insert the same event twice (simulating a retry)
            for _ in range(2):
                raw_id = app.insert_payload(con, "/v1/metrics", "application/json", json.dumps(payload).encode())
                events = app.extract_usage("/v1/metrics", json.dumps(payload).encode())
                app.insert_usage(con, raw_id, "2026-05-05T12:00:00+00:00", events)

            # Verify only one event was stored
            rows = list(con.execute("select client_event_id, input_tokens from usage_events"))
            self.assertEqual(len(rows), 1, "Should only have one event despite retry")
            self.assertEqual(rows[0][1], 100)
            # Verify the client_event_id uses trace_id and span_id
            self.assertIn("abc123", rows[0][0], "client_event_id should include trace_id")
            self.assertIn("def456", rows[0][0], "client_event_id should include span_id")

    def test_claude_code_token_metric_points_with_same_span_are_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "usage.sqlite"
            con = app.connect(db)
            app.ensure_usage_metadata_schema(con)
            app.ensure_client_sync_schema(con)
            payload = claude_code_metric_payload(
                [
                    (
                        "claude_code.token.usage",
                        100,
                        {
                            "type": "input",
                            "model": "claude-sonnet-4.5",
                            "trace_id": "trace-1",
                            "span_id": "span-1",
                        },
                    ),
                    (
                        "claude_code.token.usage",
                        20,
                        {
                            "type": "output",
                            "model": "claude-sonnet-4.5",
                            "trace_id": "trace-1",
                            "span_id": "span-1",
                        },
                    ),
                    (
                        "claude_code.token.usage",
                        30,
                        {
                            "type": "cacheRead",
                            "model": "claude-sonnet-4.5",
                            "trace_id": "trace-1",
                            "span_id": "span-1",
                        },
                    ),
                ]
            )
            body = json.dumps(payload).encode()

            for _ in range(2):
                raw_id = app.insert_payload(con, "/v1/metrics", "application/json", body)
                app.insert_usage(con, raw_id, "2026-05-05T12:00:00+00:00", app.extract_usage("/v1/metrics", body))

            rows = list(
                con.execute(
                    """
                    select client_event_id, input_tokens, output_tokens, cached_tokens
                    from usage_events
                    order by client_event_id
                    """
                )
            )
            con.close()

        self.assertEqual(len(rows), 3)
        self.assertEqual(sum(row["input_tokens"] for row in rows), 100)
        self.assertEqual(sum(row["output_tokens"] for row in rows), 20)
        self.assertEqual(sum(row["cached_tokens"] for row in rows), 30)
        self.assertTrue(all("trace-1" in row["client_event_id"] for row in rows))
        self.assertTrue(any(row["client_event_id"].endswith(":input") for row in rows))
        self.assertTrue(any(row["client_event_id"].endswith(":output") for row in rows))
        self.assertTrue(any(row["client_event_id"].endswith(":cacheRead") for row in rows))


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

    def test_mark_all_for_resync_makes_current_target_pending_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "usage.sqlite"
            con = app.connect(db)
            self.insert_sync_event(con)
            self.insert_sync_tool_event(con)
            config = app.AppConfig(
                server=app.RemoteServerConfig(endpoint="http://server", api_key="secret")
            )

            with patch.object(
                core,
                "post_usage_batch",
                return_value={
                    "accepted": 1,
                    "duplicates": 0,
                    "accepted_tool_events": 1,
                    "duplicate_tool_events": 0,
                },
            ):
                self.assertEqual(app.sync_pending_usage(con, config), (2, 2, None))

            self.assertEqual(app.pending_sync_rows(con, config, 10), [])
            self.assertEqual(app.pending_tool_sync_rows(con, config, 10), [])
            self.assertEqual(app.mark_all_for_resync(con, config), 2)
            self.assertEqual(len(app.pending_sync_rows(con, config, 10)), 1)
            self.assertEqual(len(app.pending_tool_sync_rows(con, config, 10)), 1)
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

    def test_receiver_treats_trace_span_retry_as_idempotent(self):
        payload = claude_code_metric_payload(
            [
                (
                    "claude_code.token.usage",
                    100,
                    {
                        "type": "input",
                        "model": "claude-sonnet-4.5",
                        "trace_id": "trace-1",
                        "span_id": "span-1",
                    },
                )
            ]
        )
        body = json.dumps(payload).encode()

        def run_once(db: Path) -> list[int]:
            handler = object.__new__(app.Receiver)
            handler.db_path = db
            handler.app_config = app.AppConfig()
            handler.path = "/v1/metrics"
            handler.headers = {"content-length": str(len(body)), "content-type": "application/json"}
            handler.rfile = io.BytesIO(body)
            handler.wfile = io.BytesIO()
            responses: list[int] = []
            handler.send_response = responses.append
            handler.send_header = lambda _key, _value: None
            handler.end_headers = lambda: None
            handler.do_POST()
            return responses

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "usage.sqlite"
            first = run_once(db)
            second = run_once(db)
            con = app.connect(db)
            usage_count = con.execute("select count(*) from usage_events").fetchone()[0]
            raw_count = con.execute("select count(*) from raw_payloads").fetchone()[0]
            con.close()

        self.assertEqual(first, [200])
        self.assertEqual(second, [200])
        self.assertEqual(usage_count, 1)
        self.assertEqual(raw_count, 2)

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
            "dashboard": '<nav><div class="nav-primary"><a href="/dashboard" class="active">Dashboard</a><a href="/reports">Token Usage</a><a href="/tools">Tool Usage</a></div><div class="nav-actions"><button type="button" class="theme-toggle" title="Toggle dark mode" aria-label="Toggle dark mode" onclick="aitToggleTheme()">Dark</button><a href="/admin">Admin</a></div></nav>',
            "admin": '<nav><div class="nav-primary"><a href="/dashboard">Dashboard</a><a href="/reports">Token Usage</a><a href="/tools">Tool Usage</a></div><div class="nav-actions"><button type="button" class="theme-toggle" title="Toggle dark mode" aria-label="Toggle dark mode" onclick="aitToggleTheme()">Dark</button><a href="/admin" class="active">Admin</a></div></nav>',
            "usage": '<nav><div class="nav-primary"><a href="/dashboard">Dashboard</a><a href="/reports" class="active">Token Usage</a><a href="/tools">Tool Usage</a></div><div class="nav-actions"><button type="button" class="theme-toggle" title="Toggle dark mode" aria-label="Toggle dark mode" onclick="aitToggleTheme()">Dark</button><a href="/admin">Admin</a></div></nav>',
            "tools": '<nav><div class="nav-primary"><a href="/dashboard">Dashboard</a><a href="/reports">Token Usage</a><a href="/tools" class="active">Tool Usage</a></div><div class="nav-actions"><button type="button" class="theme-toggle" title="Toggle dark mode" aria-label="Toggle dark mode" onclick="aitToggleTheme()">Dark</button><a href="/admin">Admin</a></div></nav>',
        }[active]
        self.assertIn(expected, body)
        self.assertIn('href="/admin"', body)
        self.assertIn('href="/dashboard"', body)
        self.assertIn('href="/reports"', body)
        self.assertIn('href="/tools"', body)
        self.assertIn('<meta name="viewport" content="width=device-width, initial-scale=1">', body)
        self.assertIn('html[data-theme="dark"]', body)
        self.assertIn('localStorage.getItem("ait-theme")', body)
        self.assertIn('rel="icon" type="image/svg+xml"', body)
        self.assertIn("M5%209.2H7V19H5V9.2", body)
        self.assertIn(f'href="{app.REPO_URL}"', body)
        self.assertIn(app.version_html(), body)
        self.assertIn('class="app-footer"', body)

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

    def test_collector_ingest_updates_duplicate_usage_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "server.sqlite"
            token = "unused"
            first_event = {
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
                "cost_value": 0,
                "attributes_json": "{}",
            }
            updated_event = dict(first_event)
            updated_event.update({"cost_value": 0.123, "cost_unit": "USD", "attributes_json": '{"updated": true}'})
            payloads = [
                {"client_name": "laptop", "events": [first_event]},
                {"client_name": "laptop", "events": [updated_event]},
            ]
            responses = []

            def run_request(payload):
                body = json.dumps(payload).encode()
                handler = object.__new__(app.ServerReceiver)
                handler.db_path = db
                handler.app_config = app.AppConfig()
                handler.path = "/api/v1/usage-events"
                handler.headers = {
                    "content-length": str(len(body)),
                    "content-type": "application/json",
                    "authorization": f"Bearer {token}",
                }
                handler.rfile = io.BytesIO(body)
                handler.wfile = io.BytesIO()
                handler.send_response = responses.append
                handler.send_header = lambda _key, _value: None
                handler.end_headers = lambda: None
                handler.do_POST()

            con = app.connect_server(db)
            token = app.create_client_token(con, "laptop", "Laptop")
            con.commit()
            con.close()

            run_request(payloads[0])
            run_request(payloads[1])

            con = app.connect_server(db)
            rows = con.execute("select * from usage_events").fetchall()
            con.close()

            self.assertEqual(responses, [200, 200])
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["client_event_id"], "evt-1")
            self.assertAlmostEqual(rows[0]["cost_value"], 0.123)
            self.assertEqual(rows[0]["cost_unit"], "USD")
            self.assertEqual(rows[0]["attributes_json"], '{"updated": true}')

    def test_collector_ingest_estimates_missing_cost_on_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "server.sqlite"
            token = "unused"
            body = json.dumps(
                {
                    "client_name": "laptop",
                    "events": [
                        {
                            "client_event_id": "evt-1",
                            "received_at": "2026-05-04T01:02:03+00:00",
                            "signal": "logs",
                            "event_name": "response.completed",
                            "model": "gpt-5.4-mini",
                            "input_tokens": 1000000,
                            "output_tokens": 100000,
                            "total_tokens": 1100000,
                            "cached_tokens": 0,
                            "reasoning_tokens": 0,
                            "cost_value": 0,
                            "attributes_json": "{}",
                        }
                    ],
                }
            ).encode()
            handler = object.__new__(app.ServerReceiver)
            handler.db_path = db
            handler.app_config = app.AppConfig(pricing=app.PricingConfig(estimate_openai_api_costs=True))
            handler.path = "/api/v1/usage-events"
            handler.headers = {
                "content-length": str(len(body)),
                "content-type": "application/json",
                "authorization": f"Bearer {token}",
            }
            handler.rfile = io.BytesIO(body)
            handler.wfile = io.BytesIO()
            responses = []
            handler.send_response = responses.append
            handler.send_header = lambda _key, _value: None
            handler.end_headers = lambda: None

            con = app.connect_server(db)
            token = app.create_client_token(con, "laptop", "Laptop")
            con.commit()
            con.close()

            handler.headers["authorization"] = f"Bearer {token}"
            handler.do_POST()

            con = app.connect_server(db)
            row = con.execute("select cost_value, cost_unit from usage_events").fetchone()
            con.close()

            self.assertEqual(responses, [200])
            self.assertAlmostEqual(row["cost_value"], 1.2)
            self.assertEqual(row["cost_unit"], "USD")

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
            self.assertEqual(rows[0]["cost_unit"], "USD")
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
            con.execute(
                """
                update usage_events
                set cost_value = 0,
                    cost_unit = null,
                    workspace_label = null,
                    api_key_label = null,
                    provider_name = null
                where client_name = ?
                """,
                (app.OPENROUTER_BROADCAST_CLIENT,),
            )

            result = app.replay_broadcast_payloads(con, config, replay_status="ingested")
            con.commit()

            row_count = con.execute("select count(*) from usage_events").fetchone()[0]
            usage = con.execute(
                """
                select cost_value, cost_unit, workspace_label, api_key_label, provider_name
                from usage_events
                where client_name = ?
                """,
                (app.OPENROUTER_BROADCAST_CLIENT,),
            ).fetchone()
            payload = con.execute("select replay_status, replayed_at, last_error from broadcast_payloads").fetchone()
            con.close()

            self.assertEqual(result, {"payloads": 1, "accepted": 0, "duplicates": 1, "errors": 0})
            self.assertEqual(row_count, 1)
            self.assertAlmostEqual(usage["cost_value"], 0.0123)
            self.assertEqual(usage["cost_unit"], "USD")
            self.assertEqual(usage["workspace_label"], "agents")
            self.assertEqual(usage["api_key_label"], "prod-key")
            self.assertEqual(usage["provider_name"], "OpenAI")
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
            self.assertIn('<option value="provider-source-model" selected>provider-source-model</option>', body)
            self.assertNotIn("OpenRouter by workspace", body)
            self.assertNotIn("/reports?group_by=workspace&source_kind=openrouter_broadcast", body)
            self.assertNotIn("/reports?group_by=api-key&source_kind=openrouter_broadcast", body)
            self.assertNotIn("/reports?group_by=provider&source_kind=openrouter_broadcast", body)
            self.assertIn("<th>provider</th>", body)
            self.assertIn("<th>source</th>", body)
            self.assertIn("<th>model</th>", body)
            self.assertLess(body.index("<th>total</th>"), body.index("<th>input</th>"))
            self.assertLess(body.index("<th>input</th>"), body.index("<th>output</th>"))
            self.assertLess(body.index("<th>output</th>"), body.index("<th>cached</th>"))
            self.assertLess(body.index("<th>cached</th>"), body.index("<th>reason</th>"))
            self.assertLess(body.index("<th>reason</th>"), body.index("<th>cost</th>"))
            self.assertLess(body.index("<th>cost</th>"), body.index("<th>unit</th>"))
            self.assertLess(body.index("<th>unit</th>"), body.index("<th>last</th>"))
            self.assertNotIn("<th>events</th>", body)
            self.assertIn('name="since" type="date"', body)
            self.assertIn('name="until" type="date"', body)
            self.assertIn('name="source_provider"', body)
            self.assertIn('name="source_label"', body)
            self.assertNotIn("Tool events", body)
            self.assertNotIn('name="source_kind"', body)
            self.assertNotIn('name="workspace_label"', body)
            self.assertNotIn('name="api_key_label"', body)
            self.assertIn("Collector", body)
            self.assertIn("Collectors", body)
            self.assertIn("<td>Local OTEL</td>", body)
            self.assertIn("<td>Laptop</td>", body)
            self.assertNotIn("<td>laptop</td>", body)
            self.assertIn("<td>gpt-test</td>", body)
            self.assertIn("<td class=\"num\">15</td>", body)
            self.assertIn('data-utc="2026-05-04T01:02:03+00:00"', body)
            self.assertIn("formatBrowserTimes", body)
            con.close()

    def test_reports_page_stats_follow_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = app.connect_server(Path(tmp) / "server.sqlite")
            for client, model, total in (("laptop", "gpt-test", 15), ("desktop", "gpt-other", 100)):
                app.create_client_token(con, client, client.title())
                app.ingest_usage_events(
                    con,
                    client,
                    [
                        {
                            "client_event_id": f"{client}-usage",
                            "received_at": "2026-05-04T01:02:03+00:00",
                            "signal": "logs",
                            "model": model,
                            "total_tokens": total,
                            "attributes_json": "{}",
                        }
                    ],
                )
            con.commit()

            body = app.ServerReceiver.render_reports(object(), con, {"model": ["gpt-test"]})
            con.close()

            self.assertIn('<div class="label">Total tokens</div><div class="value">15</div>', body)
            self.assertIn('<div class="label">Collectors</div><div class="value">1</div>', body)
            self.assertNotIn('<div class="label">Total tokens</div><div class="value">115</div>', body)

    def test_dashboard_renders_daily_weekly_and_monthly_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "server.sqlite"
            con = app.connect_server(db)
            app.create_client_token(con, "laptop", "Laptop")
            today = dt.datetime.now(dt.timezone.utc).replace(hour=1, minute=2, second=3, microsecond=0).isoformat()
            app.ingest_usage_events(
                con,
                "laptop",
                [
                    {
                        "client_event_id": "today-1",
                        "received_at": today,
                        "signal": "logs",
                        "event_name": "codex.usage",
                        "model": "gpt-test",
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "total_tokens": 15,
                        "cost_value": 0.25,
                        "cost_unit": "USD",
                        "attributes_json": "{}",
                    }
                ],
            )
            con.commit()

            body = app.ServerReceiver.render_dashboard(object(), con)

            self.assertIn("Daily Dashboard", body)
            self.assertSharedNav(body, "dashboard")
            self.assertIn("Today's tokens", body)
            self.assertIn("Today's cost", body)
            self.assertIn("0.25 USD", body)
            self.assertNotIn("Today's events", body)
            self.assertNotIn("Today's input", body)
            self.assertNotIn("Today's output", body)
            self.assertIn("Last 7 days tokens", body)
            self.assertIn("Last 30 days cost", body)
            self.assertIn("Today's Usage By Source", body)
            self.assertIn("Today's Usage By Provider", body)
            self.assertIn('class="table-scroll"', body)
            self.assertIn("minmax(min(100%, 24rem), 1fr)", body)
            self.assertIn("<td>Codex</td>", body)
            self.assertIn("<td>Laptop</td>", body)
            self.assertNotIn("<th>events</th>", body)
            con.close()

    def test_server_default_usage_report_groups_by_provider_source_model(self):
        body = json.dumps(openrouter_trace_payload()).encode()
        config = app.AppConfig(openrouter_broadcast=app.OpenRouterBroadcastConfig(enabled=True, api_key="orb_secret"))
        with tempfile.TemporaryDirectory() as tmp:
            con = app.connect_server(Path(tmp) / "server.sqlite")
            app.create_client_token(con, "laptop", "Laptop")
            app.ingest_usage_events(
                con,
                "laptop",
                [
                    {
                        "client_event_id": "ai-1",
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
            self.assertEqual(app.ingest_openrouter_broadcast(con, body, config), (1, 0))
            con.commit()

            args = app.ServerReceiver.reports_args({})
            rows = app.server_report_rows(con, args)
            con.close()

            self.assertEqual(args.group_by, "provider-source-model")
            grouped = {(row["source_provider"], row["source_label"], row["model"]): row["total_tokens"] for row in rows}
            self.assertEqual(grouped[("Local OTEL", "Laptop", "gpt-test")], 15)
            self.assertEqual(grouped[("OpenRouter", "agents", "openai/gpt-4o")], 17)
            self.assertEqual(app.server_default_columns("provider-source-model")[:3], ("source_provider", "source_label", "model"))

    def test_server_report_groups_priced_claude_models_by_friendly_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = app.connect_server(Path(tmp) / "server.sqlite")
            app.create_client_token(con, "laptop", "Laptop")
            for index, model in enumerate(
                (
                    "au.anthropic.claude-sonnet-4-5-20250929-v1:0",
                    "claude-sonnet-4-5-20250929",
                ),
                start=1,
            ):
                app.ingest_usage_events(
                    con,
                    "laptop",
                    [
                        {
                            "client_event_id": f"claude-{index}",
                            "received_at": "2026-05-04T01:02:03+00:00",
                            "signal": "metrics",
                            "event_name": "claude_code.token.usage",
                            "model": model,
                            "input_tokens": 10,
                            "output_tokens": 5,
                            "total_tokens": 15,
                            "cost_value": 0.01,
                            "cost_unit": "USD",
                            "attributes_json": "{}",
                        }
                    ],
                )
            con.commit()
            args = argparse.Namespace(
                group_by="provider-source-model",
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
            filtered_args = argparse.Namespace(**{**vars(args), "model": "claude-sonnet-4.5"})
            filtered_rows = app.server_report_rows(con, filtered_args)
            con.close()

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["model"], "claude-sonnet-4.5")
            self.assertEqual(rows[0]["events"], 2)
            self.assertEqual(rows[0]["total_tokens"], 30)
            self.assertEqual(len(filtered_rows), 1)
            self.assertEqual(filtered_rows[0]["events"], 2)

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
            self.assertEqual(rows[0]["cost_unit"], "USD")

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

            self.assertEqual(len(rows), 1)
            self.assertIsNone(rows[0]["cost_value"])
            self.assertEqual(rows[0]["cost_unit"], "mixed")

    def test_reports_page_shows_cost_breakdown_for_mixed_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = app.connect_server(Path(tmp) / "server.sqlite")
            for event_id, cost_value, cost_unit in (
                ("api-cost", 2.5, "USD"),
                ("openrouter-cost", 0.125, "credits"),
            ):
                app.ingest_usage_events(
                    con,
                    "client-a",
                    [
                        {
                            "client_event_id": event_id,
                            "received_at": "2026-05-04T01:02:03+00:00",
                            "signal": "traces",
                            "model": "gpt-test",
                            "input_tokens": 1,
                            "output_tokens": 1,
                            "total_tokens": 2,
                            "cost_value": cost_value,
                            "cost_unit": cost_unit,
                            "attributes_json": "{}",
                        }
                    ],
                )
            con.commit()

            stats = app.server_stats_dict(con)
            body = app.ServerReceiver.render_reports(object(), con, {})
            con.close()

            self.assertEqual(stats["cost_unit"], "mixed")
            self.assertEqual(
                stats["cost_totals"],
                [{"cost_unit": "USD", "cost_value": 2.5}, {"cost_unit": "credits", "cost_value": 0.125}],
            )
            self.assertIn('<div class="label">Cost</div><div class="value">2.5 USD + 0.125 credits</div>', body)
            self.assertNotIn('<div class="label">Cost</div><div class="value"> mixed</div>', body)

    def test_server_can_report_openrouter_credits_as_usd_without_rewriting_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = app.connect_server(Path(tmp) / "server.sqlite")
            for event_id, cost_value, cost_unit, source_kind in (
                ("api-cost", 2.5, "USD", "logs"),
                ("openrouter-cost", 0.125, "credits", "openrouter_broadcast"),
            ):
                app.ingest_usage_events(
                    con,
                    "client-a",
                    [
                        {
                            "client_event_id": event_id,
                            "received_at": "2026-05-04T01:02:03+00:00",
                            "signal": "traces",
                            "model": "gpt-test",
                            "source_kind": source_kind,
                            "input_tokens": 1,
                            "output_tokens": 1,
                            "total_tokens": 2,
                            "cost_value": cost_value,
                            "cost_unit": cost_unit,
                            "attributes_json": "{}",
                        }
                    ],
                )
            con.commit()
            args = argparse.Namespace(
                group_by="total",
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
            config = app.AppConfig(pricing=app.PricingConfig(report_openrouter_credits_as_usd=True))

            rows = app.server_report_rows(con, args, config)
            stats = app.server_stats_dict(con, config)
            stored = con.execute(
                "select cost_value, cost_unit from usage_events where client_event_id = 'openrouter-cost'"
            ).fetchone()
            con.close()

            self.assertEqual(len(rows), 1)
            self.assertAlmostEqual(rows[0]["cost_value"], 2.625)
            self.assertEqual(rows[0]["cost_unit"], "USD")
            self.assertAlmostEqual(stats["cost_value"], 2.625)
            self.assertEqual(stats["cost_unit"], "USD")
            self.assertEqual(stats["cost_totals"], [{"cost_unit": "USD", "cost_value": 2.625}])
            self.assertEqual(stored["cost_unit"], "credits")

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

    def test_server_reports_ignore_zero_cost_rows_when_selecting_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = app.connect_server(Path(tmp) / "server.sqlite")
            for event_id, cost_value, cost_unit in (
                ("estimated-cost", 1.5, "USD"),
                ("unknown-zero", 0, None),
            ):
                app.ingest_usage_events(
                    con,
                    "client-a",
                    [
                        {
                            "client_event_id": event_id,
                            "received_at": "2026-05-04T01:02:03+00:00",
                            "signal": "logs",
                            "model": "gpt-test",
                            "input_tokens": 1,
                            "output_tokens": 1,
                            "total_tokens": 2,
                            "cost_value": cost_value,
                            "cost_unit": cost_unit,
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
            total_rows = app.server_report_rows(con, argparse.Namespace(group_by="total", **base))
            provider_rows = app.server_report_rows(con, argparse.Namespace(group_by="provider-source-model", **base))
            stats = app.server_stats_dict(con)
            con.close()

            for rows in (total_rows, provider_rows):
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["cost_value"], 1.5)
                self.assertEqual(rows[0]["cost_unit"], "USD")
            self.assertEqual(stats["cost_value"], 1.5)
            self.assertEqual(stats["cost_unit"], "USD")

    def test_server_reports_treat_nonzero_unitless_cost_as_mixed(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = app.connect_server(Path(tmp) / "server.sqlite")
            for event_id, cost_value, cost_unit in (
                ("estimated-cost", 1.5, "USD"),
                ("unknown-unit", 2.0, None),
                ("unknown-zero", 0, None),
            ):
                app.ingest_usage_events(
                    con,
                    "client-a",
                    [
                        {
                            "client_event_id": event_id,
                            "received_at": "2026-05-04T01:02:03+00:00",
                            "signal": "logs",
                            "model": "gpt-test",
                            "input_tokens": 1,
                            "output_tokens": 1,
                            "total_tokens": 2,
                            "cost_value": cost_value,
                            "cost_unit": cost_unit,
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
            rows = app.server_report_rows(con, argparse.Namespace(group_by="provider-source-model", **base))
            stats = app.server_stats_dict(con)
            con.close()

            self.assertEqual(len(rows), 1)
            self.assertIsNone(rows[0]["cost_value"])
            self.assertEqual(rows[0]["cost_unit"], "mixed")
            self.assertIsNone(stats["cost_value"])
            self.assertEqual(stats["cost_unit"], "mixed")

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
                        "client_tool_event_id": "tool-decision",
                        "received_at": "2026-05-04T01:02:04+00:00",
                        "signal": "logs",
                        "event_name": "codex.tool_decision",
                        "tool_name": "exec_command",
                        "decision": "accept",
                        "source": "config",
                        "success": None,
                        "attributes_json": "{}",
                    },
                    {
                        "client_tool_event_id": "tool-1",
                        "received_at": "2026-05-04T01:02:03+00:00",
                        "signal": "logs",
                        "event_name": "codex.tool_result",
                        "model": "gpt-5.5",
                        "session_id": "session-1",
                        "tool_name": "exec_command",
                        "call_id": "call-1",
                        "decision": "accept",
                        "source": "config",
                        "success": "true",
                        "duration_ms": 42,
                        "mcp_server": "",
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
            args = app.ServerReceiver.tool_reports_args({})
            grouped_rows = app.server_tool_report_rows(con, args)
            recent_rows = app.server_tool_recent_rows(con, args)

            self.assertIn("Tool Reports", body)
            self.assertSharedNav(body, "tools")
            self.assertIn('<option value="client-tool" selected>client-tool</option>', body)
            self.assertIn("Grouped totals", body)
            self.assertIn("Recent tool calls", body)
            self.assertIn("By collector/tool", body)
            self.assertIn("<th>provider</th>", body)
            self.assertIn("<th>collector</th>", body)
            self.assertIn('name="source_provider"', body)
            self.assertIn('name="success"', body)
            self.assertIn('name="decision"', body)
            self.assertIn('name="source"', body)
            self.assertIn('name="mcp_server"', body)
            self.assertNotIn('name="session_id"', body)
            self.assertNotIn('name="event_name"', body)
            self.assertNotIn("All tool events", body)
            self.assertIn('class="status ok"', body)
            self.assertNotIn('class="status neutral"', body)
            self.assertIn("<td>Laptop</td>", body)
            self.assertNotIn("<td>laptop</td>", body)
            self.assertIn("<td>Codex</td>", body)
            self.assertIn("<td>exec_command</td>", body)
            self.assertIn("<td class=\"num\">42</td>", body)
            self.assertNotIn("session-1", body)
            self.assertNotIn("call-1", body)
            self.assertIn('data-utc="2026-05-04T01:02:03+00:00"', body)
            self.assertEqual(len(grouped_rows), 1)
            self.assertEqual(grouped_rows[0]["source_provider"], "Codex")
            self.assertEqual(len(recent_rows), 1)
            self.assertEqual(recent_rows[0]["source_received_at"], "2026-05-04T01:02:03+00:00")
            self.assertEqual(recent_rows[0]["source_provider"], "Codex")
            self.assertIn("Intl.DateTimeFormat", body)
            con.close()

    def test_tool_report_stats_follow_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = app.connect_server(Path(tmp) / "server.sqlite")
            app.create_client_token(con, "laptop", "Laptop")
            app.ingest_tool_events(
                con,
                "laptop",
                [
                    {
                        "client_tool_event_id": "ok-tool",
                        "received_at": "2026-05-04T01:02:03+00:00",
                        "signal": "logs",
                        "event_name": "codex.tool_result",
                        "tool_name": "Bash",
                        "success": "true",
                        "duration_ms": 42,
                        "attributes_json": "{}",
                    },
                    {
                        "client_tool_event_id": "fail-tool",
                        "received_at": "2026-05-04T01:02:04+00:00",
                        "signal": "logs",
                        "event_name": "codex.tool_result",
                        "tool_name": "Bash",
                        "success": "false",
                        "duration_ms": 100,
                        "attributes_json": "{}",
                    },
                ],
            )
            con.commit()

            body = app.ServerReceiver.render_tool_reports(object(), con, {"success": ["true"]})
            con.close()

            self.assertIn('<div class="label">Matching events</div><div class="value">1</div>', body)
            self.assertIn('<div class="label">Successes</div><div class="value">1</div>', body)
            self.assertIn('<div class="label">Failures</div><div class="value">0</div>', body)
            self.assertIn('<div class="label">Duration</div><div class="value">42</div>', body)

    def test_tool_reports_can_include_decisions_and_results(self):
        args = app.ServerReceiver.tool_reports_args({"group_by": ["event"]})

        self.assertEqual(args.event_name, "")
        self.assertEqual(args.group_by, "event")

    def test_tool_reports_label_claude_code_from_service_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "server.sqlite"
            con = app.connect_server(db)
            app.create_client_token(con, "workstation", "Workstation")
            app.ingest_tool_events(
                con,
                "workstation",
                [
                    {
                        "client_tool_event_id": "claude-bash-1",
                        "received_at": "2026-05-04T01:02:03+00:00",
                        "signal": "logs",
                        "event_name": "tool_result",
                        "tool_name": "Bash",
                        "success": "true",
                        "duration_ms": 28,
                        "attributes_json": json.dumps(
                            {
                                "service.name": "claude-code",
                                "service.version": "2.1.128",
                            }
                        ),
                    }
                ],
            )
            con.commit()

            args = app.ServerReceiver.tool_reports_args({})
            grouped_rows = app.server_tool_report_rows(con, args)
            recent_rows = app.server_tool_recent_rows(con, args)
            con.close()

            self.assertEqual(grouped_rows[0]["source_provider"], "Claude Code")
            self.assertEqual(recent_rows[0]["source_provider"], "Claude Code")

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
            self.assertIn("Collector Admin", body)
            self.assertIn("Create Collector Token", body)
            self.assertIn("OpenRouter Broadcast", body)
            self.assertIn("not configured", body)
            self.assertIn("Collector name", body)
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

    def test_admin_ui_shows_openrouter_configured_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "server.sqlite"
            con = app.connect_server(db)
            config = app.AppConfig(
                openrouter_broadcast=app.OpenRouterBroadcastConfig(
                    enabled=True,
                    api_key="orb_secret",
                    required_header_name="X-Test",
                    required_header_value="secret",
                    retain_payload_body=False,
                )
            )

            receiver = type("ReceiverForTest", (), {"app_config": config})()
            body = app.ServerReceiver.render_admin(receiver, con)
            con.close()

            self.assertIn("OpenRouter Broadcast", body)
            self.assertIn("configured", body)
            self.assertIn('title="Shows whether the server will accept OpenRouter Broadcast ingestion requests."', body)
            self.assertIn('<div class="label">Enabled</div><div class="value">yes</div>', body)
            self.assertIn('<div class="label">API key</div><div class="value">yes</div>', body)
            self.assertIn('<div class="label">Extra header</div><div class="value">yes</div>', body)
            self.assertIn('<div class="label">Retain payloads</div><div class="value">no</div>', body)
            self.assertNotIn("orb_secret", body)
            self.assertNotIn("secret", body)

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
