import json
import os
import tempfile
import unittest
from pathlib import Path

from router_report import current_policy_records, load_records, summarize


class RouterReportTests(unittest.TestCase):
    def test_summary_uses_only_audit_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "adaptive-1.jsonl"
            rows = [
                {"event": "classifier", "model": "gpt-5.6-terra", "effort": "medium",
                 "duration_ms": 100, "total_tokens": 200, "count": 2, "prompt": "do not expose"},
                {"event": "route", "tier": "NORMAL", "model": "gpt-5.6-terra", "effort": "medium"},
                {"event": "classifier_fallback", "stage": "source_read"},
                {"event": "turn_completed", "status": "failed"},
                {"event": "escalated"},
                {"event": "footer", "saved_percent": 42},
                {"event": "idle_restore_failed"},
            ]
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\nnot json\n", encoding="utf-8")
            report = summarize(load_records(temp))
        self.assertEqual(report["routes"], {"gpt-5.6-terra/medium": 1})
        self.assertEqual(report["classifier"], {"successes": 1, "average_duration_ms": 100,
                                                  "average_total_tokens": 200, "planned_subagents": 2})
        self.assertEqual(report["fallbacks"], {"source_read": 1})
        self.assertEqual(report["turn_statuses"], {"failed": 1})
        self.assertEqual((report["escalations"], report["idle_restore_failures"],
                          report["average_saved_percent"]), (1, 1, 42))
        self.assertEqual((report["metrics"]["fallback_rate_percent"],
                          report["metrics"]["turn_failure_rate_percent"]), (50.0, 100.0))
        self.assertFalse(report["readiness"]["ready"])
        self.assertNotIn("do not expose", json.dumps(report))

    def test_latest_uses_only_the_most_recent_process_log(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old = root / "adaptive-999.jsonl"
            new = root / "adaptive-1.jsonl"
            old.write_text('{"event":"route","model":"old"}\n', encoding="utf-8")
            new.write_text('{"event":"route","model":"new"}\n', encoding="utf-8")
            os.utime(old, (1, 1))
            os.utime(new, (2, 2))
            report = summarize(load_records(root, latest=True))
        self.assertEqual(report["routes"], {"new/None": 1})

    def test_weighted_savings_and_current_policy_filter(self):
        rows = [
            {"event": "footer", "actual_units": 1, "baseline_units": 10, "saved_percent": 90,
             "policy_fingerprint": "old"},
            {"event": "footer", "actual_units": 90, "baseline_units": 100, "saved_percent": 10,
             "policy_fingerprint": "new"},
            {"event": "footer", "actual_units": 999, "saved_percent": 1,
             "policy_fingerprint": "new"},
            {"event": "route", "model": "new", "effort": "low", "task_type": "lookup",
             "policy_fingerprint": "new"},
        ]
        self.assertEqual(summarize(rows)["average_saved_percent"], 17)
        current = summarize(current_policy_records(rows))
        self.assertEqual((current["records"], current["routes"]), (3, {"new/low": 1}))

    def test_schema_two_counts_only_backend_accepted_routes(self):
        rows = [
            {"event": "route", "schema_version": 2, "model": "rejected", "effort": "high"},
            {"event": "turn_started", "schema_version": 2, "model": "accepted", "effort": "low",
             "tier": "FAST", "task_type": "lookup"},
        ]
        report = summarize(rows)
        self.assertEqual(report["routes"], {"accepted/low": 1})
