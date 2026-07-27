import json
import os
import tempfile
import unittest
from pathlib import Path

from src.kibana_incident_grouper import group_sanitized_events, issue_signature
from src.kibana_sanitizer import sanitize_hit
from src.log_incident_inbox import LogIncidentInbox


TEST_KEY = b"local-test-hmac-key-that-is-at-least-32-bytes"


def error_hit(document_id="error-1"):
    return {
        "_index": "logs-synthetic",
        "_id": document_id,
        "_source": {
            "@timestamp": "2026-07-27T00:00:00Z",
            "stream": "stdout",
            "message": (
                "[2026-07-27 08:00:00.000] [TID: trace-demo] ERROR [worker-1] "
                "com.example.CalculatorService:42 - multiply returned an invalid result"
            ),
            "kubernetes": {
                "namespace_name": "synthetic",
                "container_name": "calculator",
                "labels": {"app_kubernetes_io/name": "calculator"},
            },
        },
    }


def write_summary(root: Path, *, document_id="error-1") -> Path:
    run = root / "poll"
    candidate_dir = run / "candidate-01"
    candidate_dir.mkdir(parents=True)
    event = sanitize_hit(error_hit(document_id), TEST_KEY)
    incident = group_sanitized_events([event])[0]
    artifact = candidate_dir / "sanitized-incident.json"
    artifact.write_text(json.dumps(incident), encoding="utf-8")
    signature = issue_signature(incident)
    summary = {
        "schema_version": "kibana-issue-connector/v2",
        "selection": {"scanned_hits": 1},
        "candidates": [
            {
                "artifact": str(artifact),
                "incident_ref": incident["source"]["incident_ref"],
                "services": ["calculator"],
                "event_count": 1,
                "first_seen_at": incident["source"]["first_seen_at"],
                "last_seen_at": incident["source"]["last_seen_at"],
                "grouping_strategy": incident["grouping"]["strategy"],
                "issue_signature": signature,
            }
        ],
    }
    summary_path = run / "summary.json"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    return summary_path


class LogIncidentInboxTest(unittest.TestCase):
    def test_ingest_is_persistent_owner_only_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox_path = root / "state" / "inbox.json"
            inbox = LogIncidentInbox(inbox_path)
            summary = write_summary(root)

            first = inbox.ingest_summary(summary)
            second = inbox.ingest_summary(summary)
            records = inbox.list()
            mode = os.stat(inbox_path).st_mode & 0o777
            persisted = inbox_path.read_text(encoding="utf-8")

        self.assertEqual({"candidates": 1, "added": 1, "deduplicated": 0}, first)
        self.assertEqual({"candidates": 1, "added": 0, "deduplicated": 1}, second)
        self.assertEqual(1, len(records))
        self.assertEqual("pending", records[0]["status"])
        self.assertEqual(2, records[0]["occurrence_count"])
        self.assertEqual(0o600, mode)
        self.assertNotIn("raw-document-id", persisted)

    def test_context_is_sanitized_and_resets_a_blocked_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = LogIncidentInbox(root / "inbox.json")
            inbox.ingest_summary(write_summary(root))
            incident_id = inbox.list()[0]["incident_id"]
            inbox.update(
                incident_id,
                status="blocked",
                workflow_run_id="20260727T000000Z-1234abcd",
                failure={"code": "generation_blocked"},
            )

            updated = inbox.add_context(
                incident_id,
                "预期 multiply(2, 3) 返回 6，并覆盖负数和零。",
            )

        self.assertEqual("pending", updated["status"])
        self.assertIsNone(updated["workflow_run_id"])
        self.assertIn("multiply", updated["evidence"]["facts"]["human_context"])

    def test_ignore_and_snooze_are_stable_local_states(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = LogIncidentInbox(root / "inbox.json")
            inbox.ingest_summary(write_summary(root))
            incident_id = inbox.list()[0]["incident_id"]

            snoozed = inbox.snooze(incident_id)
            ignored = inbox.ignore(incident_id)

        self.assertEqual("snoozed", snoozed["status"])
        self.assertEqual("ignored", ignored["status"])

    def test_context_with_credentials_is_rejected_without_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox_path = root / "inbox.json"
            inbox = LogIncidentInbox(inbox_path)
            inbox.ingest_summary(write_summary(root))
            incident_id = inbox.list()[0]["incident_id"]

            with self.assertRaisesRegex(ValueError, "敏感信息"):
                inbox.add_context(
                    incident_id,
                    "验收标准如下，password=do-not-store-this-value",
                )
            persisted = inbox_path.read_text(encoding="utf-8")

        self.assertNotIn("do-not-store-this-value", persisted)


if __name__ == "__main__":
    unittest.main()
