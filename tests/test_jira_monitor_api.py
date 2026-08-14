"""Tests for the Jira monitor HTTP API helpers."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src import jira_monitor_api


class ShadowLogReadTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.log = Path(self.dir.name) / "shadow.jsonl"

    def write_lines(self, records):
        self.log.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
            encoding="utf-8",
        )

    def test_missing_file_returns_empty(self):
        self.assertEqual(jira_monitor_api._read_shadow_issues(self.log), [])

    def test_keeps_latest_record_per_issue_newest_first(self):
        self.write_lines([
            {"ts": "2026-08-14T08:00:00+00:00", "issue": "KEYB-1", "decision": "RESOLVED"},
            {"ts": "2026-08-14T09:00:00+00:00", "issue": "KEYB-2", "decision": "NEEDS_CONTEXT"},
            {"ts": "2026-08-14T10:00:00+00:00", "issue": "KEYB-1", "decision": "BLOCKED_SENSITIVE"},
            {"ts": "2026-08-14T11:00:00+00:00", "project": "KEYB", "decision": "WATERMARK_INIT"},
        ])
        issues = jira_monitor_api._read_shadow_issues(self.log)
        self.assertEqual([i["issue"] for i in issues], ["KEYB-1", "KEYB-2"])
        self.assertEqual(issues[0]["decision"], "BLOCKED_SENSITIVE")

    def test_skips_malformed_lines(self):
        self.log.write_text('{"ts":"2026-08-14T08:00:00+00:00","issue":"A-1"}\nnot json\n', encoding="utf-8")
        issues = jira_monitor_api._read_shadow_issues(self.log)
        self.assertEqual(len(issues), 1)


class ProjectsViewTest(unittest.TestCase):
    def test_projects_view_shape(self):
        config = {
            "projects": {
                "KEYB": {
                    "enabled": True,
                    "auto_dispatch": True,
                    "issue_types": ["新需求"],
                    "repositories": [{"repository": "wzf12400/ai-pr-sandbox"}],
                    "max_dispatch_per_poll": 2,
                }
            }
        }
        view = jira_monitor_api._projects_view(config)
        self.assertEqual(len(view), 1)
        self.assertEqual(view[0]["key"], "KEYB")
        self.assertTrue(view[0]["enabled"])
        self.assertTrue(view[0]["autoDispatch"])
        self.assertEqual(view[0]["repositories"], ["wzf12400/ai-pr-sandbox"])
        self.assertEqual(view[0]["maxDispatchPerPoll"], 2)


if __name__ == "__main__":
    unittest.main()
