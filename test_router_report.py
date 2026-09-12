import json
import tempfile
import unittest
from pathlib import Path

from router_report import load_records, summarize


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
        self.assertNotIn("do not expose", json.dumps(report))
