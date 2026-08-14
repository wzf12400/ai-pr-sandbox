import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from src.code_execution_preapproval import (
    load_code_execution_preapproval_policy,
)
from tests.test_copilot_code_modifier import REPOSITORY, policy_payload


class CodeExecutionPreapprovalPolicyTest(unittest.TestCase):
    def _fixture(self, root: Path, source_types=None):
        issue_policy_path = root / "issue-publication.json"
        issue_policy_path.write_text('{"reviewed":true}\n', encoding="utf-8")
        code_policy_path = root / "issue-code-policy.json"
        code_policy_path.write_text(json.dumps(policy_payload()), encoding="utf-8")
        issue_digest = hashlib.sha256(issue_policy_path.read_bytes()).hexdigest()
        code_digest = hashlib.sha256(code_policy_path.read_bytes()).hexdigest()
        preapproval_path = root / "preapproval.json"
        preapproval_path.write_text(
            json.dumps(
                {
                    "schema_version": "code-execution-preapproval-policy/v1",
                    "policy_id": "example-log-jira-preapproval-v1",
                    "repository": REPOSITORY,
                    "issue_publication_policy_sha256": issue_digest,
                    "issue_code_policy_sha256": code_digest,
                    "allowed_source_types": source_types or ["LOG", "JIRA"],
                    "required_labels": ["ai-code-approved"],
                    "max_issues_per_run": 1,
                }
            ),
            encoding="utf-8",
        )
        confirmed = hashlib.sha256(preapproval_path.read_bytes()).hexdigest()
        return preapproval_path, confirmed, issue_policy_path, code_policy_path

    def test_binds_company_preapproval_to_both_reviewed_policies(self):
        with tempfile.TemporaryDirectory() as directory:
            inputs = self._fixture(Path(directory))

            policy = load_code_execution_preapproval_policy(*inputs)

        self.assertEqual(REPOSITORY, policy.repository)
        self.assertEqual(("ai-code-approved",), policy.labels_for("LOG", "created"))
        self.assertEqual((), policy.labels_for("NATURAL_LANGUAGE", "created"))
        self.assertEqual((), policy.labels_for("LOG", "deduplicated"))

    def test_policy_digest_change_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = self._fixture(root)
            inputs[0].write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "SHA-256 confirmation"):
                load_code_execution_preapproval_policy(*inputs)

    def test_issue_code_policy_change_invalidates_preapproval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = self._fixture(root)
            changed = policy_payload()
            changed["required_labels"] = ["different-approval"]
            inputs[3].write_text(json.dumps(changed), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "not bound"):
                load_code_execution_preapproval_policy(*inputs)

    def test_natural_language_can_be_added_to_automatic_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            inputs = self._fixture(Path(directory), ["LOG", "NATURAL_LANGUAGE"])

            policy = load_code_execution_preapproval_policy(*inputs)

        self.assertEqual(("ai-code-approved",), policy.labels_for("NATURAL_LANGUAGE", "created"))
        self.assertEqual((), policy.labels_for("NATURAL_LANGUAGE", "deduplicated"))

    def test_unknown_source_type_still_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            inputs = self._fixture(Path(directory), ["MANUAL"])

            with self.assertRaisesRegex(ValueError, "invalid source type"):
                load_code_execution_preapproval_policy(*inputs)


if __name__ == "__main__":
    unittest.main()
