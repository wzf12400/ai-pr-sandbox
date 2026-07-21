import copy
import unittest

from src.ai_issue_generator import compact_evidence
from src.kibana_incident_grouper import event_signatures, group_sanitized_events
from src.kibana_sanitizer import sanitize_hit


TEST_KEY = b"local-test-hmac-key-that-is-at-least-32-bytes"


def incident_hit(
    document_id: str,
    timestamp: str,
    service: str,
    logger: str,
    line: int,
    body: str,
    trace: str = "-",
):
    return {
        "_index": "logs-synthetic",
        "_id": document_id,
        "_source": {
            "@timestamp": timestamp,
            "stream": "stdout",
            "message": (
                f"[2026-07-21 15:34:44.765] [TID: {trace}] ERROR [worker-1] "
                f"com.example.{logger}:{line} - {body}"
            ),
            "kubernetes": {
                "namespace_name": "synthetic",
                "container_name": service,
                "labels": {"app_kubernetes_io/name": service},
            },
        },
    }


class KibanaIncidentGrouperTest(unittest.TestCase):
    def test_extracts_exception_and_call_chain_signatures(self):
        event = sanitize_hit(
            incident_hit(
                "call-chain-error",
                "2026-07-21T07:34:38.952Z",
                "assistant-service",
                "AssistantChatCommand",
                140,
                (
                    "AssistantServiceImpl.chat:93 -> "
                    "AssistantController.chat:71 java.lang.NullPointerException"
                ),
            ),
            TEST_KEY,
        )

        signatures = event_signatures(event)

        self.assertIn("frame:assistantserviceimpl.chat", signatures)
        self.assertIn("frame:assistantcontroller.chat", signatures)
        self.assertIn("exception:nullpointerexception", signatures)

    def test_groups_same_s3_failure_but_not_unrelated_same_time_logs(self):
        events = [
            sanitize_hit(
                incident_hit(
                    "aws-access-error",
                    "2026-07-21T07:34:44.765Z",
                    "asset-service",
                    "ObjectStorageUtils",
                    248,
                    "Amazon S3 returned 403 InvalidAccessKeyId",
                ),
                TEST_KEY,
            ),
            sanitize_hit(
                incident_hit(
                    "icon-upload-error",
                    "2026-07-21T07:34:44.765Z",
                    "asset-service",
                    "AssetUploadServiceImpl",
                    108,
                    "Fail to upload icon to S3",
                ),
                TEST_KEY,
            ),
            sanitize_hit(
                incident_hit(
                    "database-collation-error",
                    "2026-07-21T07:34:44.765Z",
                    "asset-service",
                    "ResourceMapper",
                    51,
                    "java.sql.SQLException: Illegal mix of collations",
                ),
                TEST_KEY,
            ),
            sanitize_hit(
                incident_hit(
                    "other-service-s3-error",
                    "2026-07-21T07:34:44.765Z",
                    "another-service",
                    "ObjectStorageUtils",
                    248,
                    "Amazon S3 returned 403 InvalidAccessKeyId",
                ),
                TEST_KEY,
            ),
        ]

        incidents = group_sanitized_events(events)
        grouped = [item for item in incidents if item["incident"]["event_count"] == 2]

        self.assertEqual(len(incidents), 3)
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0]["grouping"]["strategy"], "fallback_similarity")
        self.assertEqual(
            grouped[0]["grouping"]["links"][0]["rule"],
            "same_service_exact_timestamp_and_signature",
        )
        self.assertEqual(
            grouped[0]["grouping"]["links"][0]["shared_signatures"],
            ["system:s3"],
        )

    def test_trace_ref_has_priority_across_services_and_time(self):
        first = sanitize_hit(
            incident_hit(
                "trace-first",
                "2026-07-21T07:34:44.765Z",
                "frontend-api",
                "RequestController",
                71,
                "request failed",
                trace="trace-shared",
            ),
            TEST_KEY,
        )
        second = sanitize_hit(
            incident_hit(
                "trace-second",
                "2026-07-21T07:35:30.000Z",
                "backend-service",
                "RequestService",
                93,
                "downstream failed",
                trace="trace-shared",
            ),
            TEST_KEY,
        )

        incidents = group_sanitized_events([first, second])

        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["grouping"]["strategy"], "trace_ref")
        self.assertEqual(incidents[0]["incident"]["event_count"], 2)

    def test_complete_link_prevents_transitive_bridge_grouping(self):
        first = sanitize_hit(
            incident_hit(
                "bridge-first",
                "2026-07-21T07:34:44.000Z",
                "asset-service",
                "FirstService",
                10,
                "Amazon S3 upload failed",
            ),
            TEST_KEY,
        )
        bridge = sanitize_hit(
            incident_hit(
                "bridge-middle",
                "2026-07-21T07:34:44.000Z",
                "asset-service",
                "MiddleService",
                20,
                "Amazon S3 and Redis operation failed",
            ),
            TEST_KEY,
        )
        last = sanitize_hit(
            incident_hit(
                "bridge-last",
                "2026-07-21T07:34:44.000Z",
                "asset-service",
                "LastService",
                30,
                "Redis operation failed",
            ),
            TEST_KEY,
        )

        incidents = group_sanitized_events([first, bridge, last])

        self.assertEqual(sorted(item["incident"]["event_count"] for item in incidents), [1, 2])

    def test_compact_evidence_preserves_group_audit_without_raw_hits(self):
        events = [
            sanitize_hit(
                incident_hit(
                    "compact-first",
                    "2026-07-21T07:34:44.765Z",
                    "asset-service",
                    "ObjectStorageUtils",
                    248,
                    "Amazon S3 returned 403 InvalidAccessKeyId",
                ),
                TEST_KEY,
            ),
            sanitize_hit(
                incident_hit(
                    "compact-second",
                    "2026-07-21T07:34:44.765Z",
                    "asset-service",
                    "AssetUploadServiceImpl",
                    108,
                    "Fail to upload icon to S3",
                ),
                TEST_KEY,
            ),
        ]
        incident = group_sanitized_events(events)[0]

        compact = compact_evidence(incident)

        self.assertEqual(compact["source"]["reference"], incident["source"]["incident_ref"])
        self.assertEqual(compact["event"]["event_count"], 2)
        self.assertEqual(len(compact["event"]["observations"]), 2)
        self.assertEqual(compact["event"]["grouping"]["shared_signatures"], ["system:s3"])

        ineligible = copy.deepcopy(incident)
        ineligible["members"][0]["sanitization"]["ai_allowed"] = False
        with self.assertRaisesRegex(ValueError, "ineligible member"):
            compact_evidence(ineligible)


if __name__ == "__main__":
    unittest.main()
