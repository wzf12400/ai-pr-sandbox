import json
import tempfile
import unittest
from pathlib import Path

from src.kibana_incident_grouper import group_sanitized_events, issue_signature
from src.kibana_sanitizer import sanitize_hit
from src.log_incident_inbox import LogIncidentInbox
from src.log_task_ingestor import (
    ControlPlaneLogClient,
    LogTaskIngestionConfig,
    LogTaskIngestionError,
    _load_local_source_settings,
    build_log_task_payload,
    configure_local_source,
    run_once,
    submit_summary_to_control_plane,
)


TEST_KEY = b"local-test-hmac-key-that-is-at-least-32-bytes"
DISCOVER_URL = (
    "https://logs.example.invalid/_dashboards/app/discover#/"
    "?_g=(time:(from:now-2h,to:now))"
    "&_a=(index:ee351460-8261-11f0-bb8a-4fb3796753f3)"
)


def _write_summary(
    root: Path,
    *,
    run_name: str,
    document_id: str,
    trace: str,
    timestamp: str,
    user_id: str,
) -> Path:
    run = root / run_name
    candidate_dir = run / "candidate-01"
    candidate_dir.mkdir(parents=True)
    hit = {
        "_index": "logs-synthetic",
        "_id": document_id,
        "_source": {
            "@timestamp": timestamp,
            "stream": "stdout",
            "message": (
                f"[2026-08-10 08:00:00.000] [TID: {trace}] ERROR [worker] "
                "com.example.CalculatorService:42 - "
                f"CommonParams(userId={user_id}) "
                "request_path=/v1/calculator/divide "
                "java.lang.IllegalStateException "
                "at com.example.CalculatorService.divide(CalculatorService.java:42)"
            ),
            "kubernetes": {
                "namespace_name": "synthetic",
                "container_name": "calculator",
                "labels": {"app_kubernetes_io/name": "calculator"},
            },
        },
    }
    event = sanitize_hit(hit, TEST_KEY, include_aggregation_refs=True)
    incident = group_sanitized_events([event])[0]
    artifact = candidate_dir / "sanitized-incident.json"
    artifact.write_text(json.dumps(incident), encoding="utf-8")
    signature = issue_signature(incident)
    summary = {
        "schema_version": "kibana-issue-connector/v2",
        "source": {
            "base_url": "https://logs.example.invalid/_dashboards",
            "data_view_id": "ee351460-8261-11f0-bb8a-4fb3796753f3",
            "time_from": "now-2h",
            "time_to": "now",
        },
        "query": {
            "cursor_commit_deferred": True,
            "batch_completed_through": timestamp,
            "effective_time_to": timestamp,
            "backlog_remaining": False,
        },
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


class FakeControlPlaneClient:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.payloads = []

    def create_task(self, payload):
        self.payloads.append(payload)
        if self.fail:
            raise LogTaskIngestionError("synthetic control-plane failure")
        return {
            "id": "3f08ea61-71b4-42de-bc8e-608a18bba522",
            "sourceType": "LOG",
            "status": "PENDING",
            "logIncident": {
                "sourceReference": payload["logIncident"]["sourceReference"]
            },
        }


class LogTaskIngestorTest(unittest.TestCase):
    def test_configure_writes_only_public_settings_and_delegates_password(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "log-platform.json"
            calls = []
            configure_local_source(
                DISCOVER_URL,
                "readonly",
                path=path,
                password_storer=lambda url, username: calls.append((url, username)),
            )
            persisted = path.read_text(encoding="utf-8")
            mode = path.stat().st_mode & 0o777

        self.assertEqual([(DISCOVER_URL, "readonly")], calls)
        self.assertEqual(0o600, mode)
        self.assertNotIn("password", persisted.casefold())
        self.assertEqual("readonly", json.loads(persisted)["username"])

    def test_loads_only_owner_private_local_source_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "log-platform.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": "local-log-platform-source/v1",
                        "discover_url": DISCOVER_URL,
                        "username": "readonly",
                    }
                ),
                encoding="utf-8",
            )
            path.chmod(0o600)
            loaded = _load_local_source_settings(path)
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "invalid"):
                _load_local_source_settings(path)

        self.assertEqual("readonly", loaded["username"])
        self.assertEqual(DISCOVER_URL, loaded["discover_url"])

    def test_builds_control_plane_payload_from_cumulative_sanitized_inbox(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = LogIncidentInbox(root / "state" / "inbox.json")
            first = _write_summary(
                root,
                run_name="poll-1",
                document_id="error-1",
                trace="trace-1",
                timestamp="2026-08-10T00:00:00Z",
                user_id="private-user-1",
            )
            second = _write_summary(
                root,
                run_name="poll-2",
                document_id="error-2",
                trace="trace-2",
                timestamp="2026-08-10T01:00:00Z",
                user_id="private-user-2",
            )
            inbox.ingest_summary(first)
            inbox.ingest_summary(second)
            payload = build_log_task_payload(inbox.list()[0])

        incident = payload["logIncident"]
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertEqual("LOG", payload["sourceType"])
        self.assertIn("calculator", payload["input"])
        self.assertIn("illegalstateexception", payload["input"])
        self.assertEqual(1, incident["currentScanEventCount"])
        self.assertEqual(2, incident["historicalEventCount"])
        self.assertEqual(2, incident["incidentGroupCount"])
        self.assertEqual("2026-08-10T00:00:00Z", incident["firstSeenAt"])
        self.assertEqual("2026-08-10T01:00:00Z", incident["lastSeenAt"])
        self.assertEqual(["/v1/calculator/divide"], incident["affectedEndpoints"])
        self.assertNotIn("private-user", serialized)
        self.assertNotIn("user_ref:", serialized)
        self.assertNotIn("trace_ref:", serialized)

    def test_reuses_acknowledged_inbox_task_instead_of_posting_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary_path = _write_summary(
                root,
                run_name="poll",
                document_id="error-1",
                trace="trace-1",
                timestamp="2026-08-10T00:00:00Z",
                user_id="private-user-1",
            )
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            inbox = LogIncidentInbox(root / "state" / "inbox.json")
            client = FakeControlPlaneClient()

            first = submit_summary_to_control_plane(
                summary_path, summary, inbox, client
            )
            second = submit_summary_to_control_plane(
                summary_path, summary, inbox, client
            )

        self.assertEqual(1, len(client.payloads))
        self.assertEqual(1, len(first["submitted_task_ids"]))
        self.assertEqual(first["submitted_task_ids"], second["reused_task_ids"])

    def test_cursor_is_not_committed_when_control_plane_submission_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary_path = _write_summary(
                root,
                run_name="poll",
                document_id="error-1",
                trace="trace-1",
                timestamp="2026-08-10T00:00:00Z",
                user_id="private-user-1",
            )
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            commits = []

            def poller(**_kwargs):
                return summary_path, summary

            def committer(**kwargs):
                commits.append(kwargs)

            config = LogTaskIngestionConfig(
                discover_url=DISCOVER_URL,
                username="readonly",
                password="runtime-only-password",
                output_path=root / "output",
                key_path=root / "key.json",
                scan_state_path=root / "cursor.json",
                inbox_path=root / "inbox.json",
            )
            with self.assertRaisesRegex(
                LogTaskIngestionError, "synthetic control-plane failure"
            ):
                run_once(
                    config,
                    client=FakeControlPlaneClient(fail=True),
                    poller=poller,
                    cursor_committer=committer,
                )

        self.assertEqual([], commits)

    def test_cursor_is_committed_after_control_plane_acknowledgement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary_path = _write_summary(
                root,
                run_name="poll",
                document_id="error-1",
                trace="trace-1",
                timestamp="2026-08-10T00:00:00Z",
                user_id="private-user-1",
            )
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            commits = []

            result = run_once(
                LogTaskIngestionConfig(
                    discover_url=DISCOVER_URL,
                    username="readonly",
                    password="runtime-only-password",
                    output_path=root / "output",
                    key_path=root / "key.json",
                    scan_state_path=root / "cursor.json",
                    inbox_path=root / "inbox.json",
                ),
                client=FakeControlPlaneClient(),
                poller=lambda **_kwargs: (summary_path, summary),
                cursor_committer=lambda **kwargs: commits.append(kwargs),
            )

        self.assertTrue(result["cursor_committed"])
        self.assertEqual(1, len(result["submitted_task_ids"]))
        self.assertEqual(1, len(commits))

    def test_control_plane_client_rejects_non_loopback_url(self):
        with self.assertRaisesRegex(ValueError, "local machine"):
            ControlPlaneLogClient("http://example.com:8080", 5)


if __name__ == "__main__":
    unittest.main()
