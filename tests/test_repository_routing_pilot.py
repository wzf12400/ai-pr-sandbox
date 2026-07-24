import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from src.repository_routing_pilot import (
    PILOT_POLICY_VERSION,
    SnapshotEntry,
    SnapshotManifest,
    extract_query_terms,
    load_snapshot_manifest,
    predict_records,
)


REPOSITORIES = ("example-org/alpha", "example-org/beta")


def input_record(case_ref, statement, repositories=REPOSITORIES, status="eligible"):
    if status == "blocked":
        statement = "[BLOCKED_BY_LOCAL_SENSITIVE_DATA_POLICY]"
    return {
        "schema_version": "repository-routing-benchmark-input/v1",
        "case_ref": f"swebench_ref:{case_ref * 32}",
        "source_type": "public_github_issue",
        "problem_statement": statement,
        "problem_sha256": hashlib.sha256(statement.encode()).hexdigest(),
        "candidate_repositories": list(repositories),
        "preflight": {"status": status, "redacted_categories": []},
        "derived_from": None,
        "answer_fields_present": False,
    }


class RepositoryRoutingPilotTest(unittest.TestCase):
    def make_manifest(self, root):
        root = Path(root)
        alpha = root / "alpha"
        beta = root / "beta"
        alpha.mkdir()
        beta.mkdir()
        (alpha / "routing_worker.py").write_text(
            "class RoutingWorker:\n"
            "    def retry_delivery(self):\n"
            "        raise DeliveryRetryError()\n",
            encoding="utf-8",
        )
        (beta / "scheduler.py").write_text(
            "class Scheduler:\n"
            "    def enqueue_job(self):\n"
            "        return QueueResult()\n",
            encoding="utf-8",
        )
        (alpha / "__init__.py").write_text("", encoding="utf-8")
        (beta / "__init__.py").write_text("", encoding="utf-8")
        return SnapshotManifest(
            "current_head_proxy",
            "2026-07-24T00:00:00Z",
            (
                SnapshotEntry(REPOSITORIES[0], alpha, "a" * 40),
                SnapshotEntry(REPOSITORIES[1], beta, "b" * 40),
            ),
        )

    def test_extracts_code_identifiers_without_general_prose(self):
        terms = extract_query_terms(
            "`RoutingWorker.retry_delivery()` raises DeliveryRetryError "
            "in src/routing_worker.py."
        )

        values = {term.value for term in terms}
        self.assertIn("RoutingWorker", values)
        self.assertIn("retry_delivery", values)
        self.assertIn("DeliveryRetryError", values)
        self.assertNotIn("raises", values)

    def test_predicts_resolved_unknown_ambiguous_and_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = self.make_manifest(directory)
            records = [
                input_record(
                    "1",
                    "`RoutingWorker.retry_delivery()` raises DeliveryRetryError.",
                ),
                input_record("2", "The application behaves unexpectedly."),
                input_record(
                    "3",
                    "`SharedWorker.retry_delivery()` raises SharedError.",
                ),
                input_record("4", "", status="blocked"),
            ]
            for entry in manifest.entries:
                (entry.path / "shared.py").write_text(
                    "class SharedWorker:\n"
                    "    def retry_delivery(self):\n"
                    "        raise SharedError()\n",
                    encoding="utf-8",
                )

            predictions, audit = predict_records(records, manifest)

        self.assertEqual(
            ["resolved", "unknown", "ambiguous", "blocked"],
            [prediction["status"] for prediction in predictions],
        )
        self.assertEqual(REPOSITORIES[0], predictions[0]["selected_repository"])
        self.assertEqual(PILOT_POLICY_VERSION, predictions[0]["policy_version"])
        self.assertFalse(audit["private_labels_loaded"])
        self.assertFalse(audit["raw_problem_statements_persisted"])
        self.assertFalse(audit["historical_snapshot"])

    def test_explicit_candidate_package_name_is_independent_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = self.make_manifest(directory)
            records = [
                input_record(
                    "6",
                    "Alpha fails when `RoutingWorker.retry_delivery()` is called.",
                )
            ]

            predictions, _audit = predict_records(records, manifest)

        self.assertEqual("resolved", predictions[0]["status"])
        self.assertEqual(REPOSITORIES[0], predictions[0]["selected_repository"])

    def test_gold_removed_candidate_scope_stays_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = self.make_manifest(directory)
            (manifest.entries[1].path / "routing_worker.py").write_text(
                "class RoutingWorker:\n"
                "    def retry_delivery(self):\n"
                "        raise DeliveryRetryError()\n",
                encoding="utf-8",
            )
            records = [
                input_record(
                    "5",
                    "Alpha `RoutingWorker.retry_delivery()` raises DeliveryRetryError.",
                    repositories=(REPOSITORIES[1],),
                )
            ]

            predictions, audit = predict_records(records, manifest)

        self.assertEqual("unknown", predictions[0]["status"])
        self.assertIsNone(predictions[0]["selected_repository"])
        self.assertIsNotNone(audit["case_audit"][0]["outside_scope_alias_ref"])

    def test_manifest_rejects_duplicate_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot"
            snapshot.mkdir()
            payload = {
                "schema_version": "repository-routing-snapshot-manifest/v1",
                "snapshot_kind": "current_head_proxy",
                "captured_at": "2026-07-24T00:00:00Z",
                "repositories": [
                    {
                        "repository": REPOSITORIES[0],
                        "path": str(snapshot),
                        "commit": "a" * 40,
                    },
                    {
                        "repository": REPOSITORIES[0].upper(),
                        "path": str(snapshot),
                        "commit": "b" * 40,
                    },
                ],
            }
            path = root / "manifest.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "invalid or duplicated"):
                load_snapshot_manifest(path)


if __name__ == "__main__":
    unittest.main()
