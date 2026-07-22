import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.natural_language_issue_automation import main
from tests.test_repository_issue_automation import (
    FakeIssueClient,
    FakeSearchAdapter,
    generation,
)
from tests.test_repository_resolver import scope_payload


class NaturalLanguageIssueAutomationTest(unittest.TestCase):
    def test_one_natural_language_command_can_create_policy_approved_issue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scope_path = root / "scope.json"
            scope_path.write_text(json.dumps(scope_payload()), encoding="utf-8")
            scope_digest = hashlib.sha256(scope_path.read_bytes()).hexdigest()
            policy_path = root / "policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "schema_version": "repository-auto-publish-policy/v1",
                        "policy_id": "synthetic-auto-policy",
                        "scope_id": "synthetic-routing-probe",
                        "scope_sha256": scope_digest,
                        "provider": "github_cli",
                        "max_issues_per_run": 1,
                        "allowed_generation_states": ["ready_for_human_review"],
                        "allowed_adapters": ["github-tree-probe"],
                    }
                ),
                encoding="utf-8",
            )
            policy_digest = hashlib.sha256(policy_path.read_bytes()).hexdigest()
            generated = generation()
            generated["draft"]["evidence"][1]["source_paths"] = ["$.facts.code_method"]
            client = FakeIssueClient()

            with mock.patch(
                "src.natural_language_issue_automation._gateway_config",
                return_value=mock.Mock(model="generator", review_model="reviewer"),
            ), mock.patch(
                "src.natural_language_issue_automation.ai_issue_generator.generate_issue",
                return_value=generated,
            ), mock.patch(
                "src.natural_language_issue_automation.GitHubCLIRepositoryTreeProbeAdapter",
                return_value=FakeSearchAdapter(),
            ), mock.patch(
                "src.natural_language_issue_automation.GitHubCLIIssueClient",
                return_value=client,
            ):
                exit_code = main(
                    [
                        "--description",
                        (
                            "com.example.routing.SyntheticRoutingController.routeIssue "
                            "调用 /v1/synthetic-routing 时抛出 SyntheticRoutingException"
                        ),
                        "--scope",
                        str(scope_path),
                        "--auto-policy",
                        str(policy_path),
                        "--confirmed-policy-sha256",
                        policy_digest,
                        "--adapter",
                        "github-tree-probe",
                        "--auto-publish",
                        "--output-dir",
                        str(root / "output"),
                        "--name",
                        "run-1",
                    ]
                )

            self.assertEqual(0, exit_code)
            automation = json.loads(
                (root / "output" / "run-1" / "automation.json").read_text()
            )
            evidence = json.loads(
                (root / "output" / "run-1" / "evidence.json").read_text()
            )
            self.assertEqual("created", automation["publication"]["status"])
            self.assertEqual(
                "com.example.routing.SyntheticRoutingController",
                evidence["facts"]["qualified_class"],
            )
            self.assertEqual("routeIssue", evidence["facts"]["code_method"])
            self.assertEqual(1, len(client.create_calls))


if __name__ == "__main__":
    unittest.main()
