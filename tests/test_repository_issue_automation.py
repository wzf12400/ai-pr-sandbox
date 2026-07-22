import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from src.repository_issue_automation import (
    RepositoryAutoPublishPolicy,
    automate_repository_issue,
    issue_fingerprint,
    load_auto_publish_policy,
    render_automated_issue_body,
)
from src.repository_resolver import (
    RepositoryEntry,
    RepositorySearchScope,
    SearchHits,
    SearchLimits,
)
from tests.test_repository_resolver import (
    CLASS_NAME,
    METHOD_NAME,
    QUALIFIED_CLASS,
    REPOSITORIES,
    issue_result,
)


def scope():
    return RepositorySearchScope(
        "synthetic-routing-probe",
        tuple(RepositoryEntry(repository, True, "main", ("probe",)) for repository in REPOSITORIES),
        SearchLimits(12, 3, 5),
    )


def policy():
    return RepositoryAutoPublishPolicy(
        policy_id="synthetic-auto-policy",
        policy_sha256="b" * 64,
        scope_id="synthetic-routing-probe",
        scope_sha256="c" * 64,
        provider="github_cli",
        max_issues_per_run=1,
        allowed_generation_states=frozenset(
            {"ready_for_human_review", "needs_human_context"}
        ),
        allowed_adapters=frozenset({"github-code-search", "github-tree-probe"}),
    )


def generation():
    result = issue_result()
    result["draft"]["request_type"] = "Bug"
    result["draft"]["severity"] = "Unknown"
    return result


class FakeSearchAdapter:
    def __init__(self):
        self.calls = []

    def search(self, repository, term, max_hits):
        self.calls.append((repository, term, max_hits))
        if repository == REPOSITORIES[0] and term in {
            QUALIFIED_CLASS,
            CLASS_NAME,
            METHOD_NAME,
        }:
            return SearchHits(frozenset({"src/SyntheticRoutingController.java"}))
        return SearchHits(frozenset())


class FakeIssueClient:
    def __init__(self, issues=None):
        self.issues = list(issues or [])
        self.list_calls = []
        self.create_calls = []

    def list_issues(self, repository, limit):
        self.list_calls.append((repository, limit))
        return self.issues

    def create_issue(self, repository, title, body):
        self.create_calls.append((repository, title, body))
        return f"https://github.com/{repository}/issues/1"


class RepositoryIssueAutomationTest(unittest.TestCase):
    def test_dry_run_approves_but_does_not_publish(self):
        client = FakeIssueClient()

        result = automate_repository_issue(
            generation(),
            {"safety": {"ai_allowed": True, "security_review_required": False}},
            scope(),
            FakeSearchAdapter(),
            "github-tree-probe",
            policy(),
            client,
            False,
        )

        self.assertTrue(result["approval"]["approved"])
        self.assertEqual("new_issue", result["issue_match"]["status"])
        self.assertEqual("approved_not_published", result["publication"]["status"])
        self.assertEqual([], client.create_calls)

    def test_approved_program_creates_one_issue_in_resolved_repository(self):
        client = FakeIssueClient()

        result = automate_repository_issue(
            generation(),
            {"safety": {"ai_allowed": True, "security_review_required": False}},
            scope(),
            FakeSearchAdapter(),
            "github-tree-probe",
            policy(),
            client,
            True,
        )

        self.assertEqual("created", result["publication"]["status"])
        self.assertEqual(REPOSITORIES[0], result["publication"]["repository"])
        self.assertEqual(1, len(client.create_calls))
        self.assertIn("repository-issue-fingerprint/v1", client.create_calls[0][2])
        self.assertNotIn("src/SyntheticRoutingController.java", json.dumps(result))

    def test_exact_fingerprint_is_deduplicated_without_write(self):
        generated = generation()
        body, _ = render_automated_issue_body(generated, REPOSITORIES[0], policy())
        existing = {
            "number": 7,
            "title": generated["draft"]["title"],
            "body": body,
            "url": f"https://github.com/{REPOSITORIES[0]}/issues/7",
            "state": "OPEN",
        }
        client = FakeIssueClient([existing])

        result = automate_repository_issue(
            generated,
            {"safety": {"ai_allowed": True, "security_review_required": False}},
            scope(),
            FakeSearchAdapter(),
            "github-tree-probe",
            policy(),
            client,
            True,
        )

        self.assertEqual("deduplicated", result["publication"]["status"])
        self.assertEqual(existing["url"], result["publication"]["issue_url"])
        self.assertEqual([], client.create_calls)
        self.assertFalse(result["issue_match"]["raw_issue_bodies_persisted"])

    def test_security_review_blocks_before_issue_search(self):
        client = FakeIssueClient()
        search = FakeSearchAdapter()

        result = automate_repository_issue(
            generation(),
            {"safety": {"ai_allowed": True, "security_review_required": True}},
            scope(),
            search,
            "github-tree-probe",
            policy(),
            client,
            True,
        )

        self.assertFalse(result["approval"]["approved"])
        self.assertEqual("blocked", result["publication"]["status"])
        self.assertEqual([], client.list_calls)
        self.assertEqual([], client.create_calls)
        self.assertEqual([], search.calls)

    def test_unapproved_adapter_blocks_before_issue_search(self):
        restricted = policy()
        restricted = RepositoryAutoPublishPolicy(
            **{
                **restricted.__dict__,
                "allowed_adapters": frozenset({"github-code-search"}),
            }
        )
        client = FakeIssueClient()
        search = FakeSearchAdapter()

        result = automate_repository_issue(
            generation(),
            {"safety": {"ai_allowed": True, "security_review_required": False}},
            scope(),
            search,
            "github-tree-probe",
            restricted,
            client,
            True,
        )

        self.assertFalse(result["approval"]["approved"])
        self.assertEqual([], client.list_calls)
        self.assertEqual([], search.calls)

    def test_ai_action_authorization_blocks_before_repository_search(self):
        generated = generation()
        generated["policy"]["publication_allowed"] = True
        client = FakeIssueClient()
        search = FakeSearchAdapter()

        result = automate_repository_issue(
            generated,
            {"safety": {"ai_allowed": True, "security_review_required": False}},
            scope(),
            search,
            "github-tree-probe",
            policy(),
            client,
            True,
        )

        self.assertFalse(result["approval"]["approved"])
        self.assertFalse(result["approval"]["rules"]["ai_did_not_authorize_actions"])
        self.assertEqual([], search.calls)

    def test_fingerprint_is_stable_for_the_same_structured_issue(self):
        first = issue_fingerprint(generation(), REPOSITORIES[0])
        second = issue_fingerprint(generation(), REPOSITORIES[0])

        self.assertEqual(first, second)
        self.assertRegex(first, r"^[0-9a-f]{64}$")

    def test_policy_digest_and_scope_digest_are_both_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scope_path = root / "scope.json"
            scope_payload = {
                "schema_version": "repository-search-scope/v1",
                "scope_id": "synthetic-routing-probe",
                "provider": "github",
                "repositories": [],
                "limits": {},
            }
            scope_path.write_text(json.dumps(scope_payload), encoding="utf-8")
            scope_digest = hashlib.sha256(scope_path.read_bytes()).hexdigest()
            policy_path = root / "policy.json"
            policy_payload = {
                "schema_version": "repository-auto-publish-policy/v1",
                "policy_id": "synthetic-auto-policy",
                "scope_id": "synthetic-routing-probe",
                "scope_sha256": scope_digest,
                "provider": "github_cli",
                "max_issues_per_run": 1,
                "allowed_generation_states": ["ready_for_human_review"],
                "allowed_adapters": ["github-code-search"],
            }
            policy_path.write_text(json.dumps(policy_payload), encoding="utf-8")
            policy_digest = hashlib.sha256(policy_path.read_bytes()).hexdigest()

            loaded = load_auto_publish_policy(
                policy_path, policy_digest, scope(), scope_path
            )

            self.assertEqual(policy_digest, loaded.policy_sha256)
            changed = json.loads(policy_path.read_text())
            changed["max_issues_per_run"] = 2
            policy_path.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "SHA-256 confirmation"):
                load_auto_publish_policy(policy_path, policy_digest, scope(), scope_path)


if __name__ == "__main__":
    unittest.main()
