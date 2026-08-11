import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from src.repository_issue_automation import (
    FINGERPRINT_VERSION,
    LEGACY_FINGERPRINT_VERSION,
    ROUTING_MODE_DEMO_SINGLE_REPOSITORY,
    ROUTING_MODE_PRODUCTION_EVIDENCE,
    RepositoryAutoPublishPolicy,
    _legacy_issue_fingerprint,
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


def requirement_generation(expected, criteria):
    result = generation()
    result["draft"]["title"] = "Limit calculator range"
    result["draft"]["problem"]["expected_behavior"] = expected
    result["draft"]["acceptance_criteria"] = list(criteria)
    return result


def legacy_issue_body(generated, repository):
    body, fingerprint = render_automated_issue_body(
        generated,
        repository,
        policy(),
    )
    legacy = _legacy_issue_fingerprint(generated, repository)
    return body.replace(
        f"<!-- {FINGERPRINT_VERSION}:{fingerprint} -->",
        f"<!-- {LEGACY_FINGERPRINT_VERSION}:{legacy} -->",
    )


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
    def test_operator_approved_single_repository_scope_skips_code_search(self):
        single_scope = RepositorySearchScope(
            "synthetic-routing-probe",
            (RepositoryEntry(REPOSITORIES[0], True, "main", ("probe",)),),
            SearchLimits(12, 1, 5),
        )
        client = FakeIssueClient()
        search = FakeSearchAdapter()

        result = automate_repository_issue(
            generation(),
            {"safety": {"ai_allowed": True, "security_review_required": False}},
            single_scope,
            search,
            "github-tree-probe",
            policy(),
            client,
            False,
            preselected_repository=REPOSITORIES[0],
            routing_mode=ROUTING_MODE_DEMO_SINGLE_REPOSITORY,
            input_type="sanitized_evidence",
        )

        self.assertEqual("resolved", result["resolution"]["status"])
        self.assertEqual(
            "operator_scope",
            result["resolution"]["search_audit"]["provider"],
        )
        self.assertEqual([], search.calls)
        self.assertEqual(
            "approved_not_published",
            result["publication"]["status"],
        )
        self.assertEqual(
            ROUTING_MODE_DEMO_SINGLE_REPOSITORY,
            result["policy"]["routing_mode"],
        )

    def test_production_log_rejects_single_repository_convenience_binding(self):
        single_scope = RepositorySearchScope(
            "synthetic-routing-probe",
            (RepositoryEntry(REPOSITORIES[0], True, "main", ("probe",)),),
            SearchLimits(12, 1, 5),
        )

        with self.assertRaisesRegex(ValueError, "forbids single-repository"):
            automate_repository_issue(
                generation(),
                {"safety": {"ai_allowed": True, "security_review_required": False}},
                single_scope,
                FakeSearchAdapter(),
                "github-tree-probe",
                policy(),
                FakeIssueClient(),
                False,
                preselected_repository=REPOSITORIES[0],
                routing_mode=ROUTING_MODE_PRODUCTION_EVIDENCE,
                input_type="sanitized_evidence",
            )

    def test_production_log_uses_code_evidence_in_single_repository_scope(self):
        single_scope = RepositorySearchScope(
            "synthetic-routing-probe",
            (RepositoryEntry(REPOSITORIES[0], True, "main", ("probe",)),),
            SearchLimits(12, 1, 5),
        )
        search = FakeSearchAdapter()

        result = automate_repository_issue(
            generation(),
            {"safety": {"ai_allowed": True, "security_review_required": False}},
            single_scope,
            search,
            "github-tree-probe",
            policy(),
            FakeIssueClient(),
            False,
            routing_mode=ROUTING_MODE_PRODUCTION_EVIDENCE,
            input_type="sanitized_evidence",
        )

        self.assertEqual("resolved", result["resolution"]["status"])
        self.assertEqual("github", result["resolution"]["search_audit"]["provider"])
        self.assertGreater(len(search.calls), 0)

    def test_single_repository_without_operator_binding_requires_code_evidence(self):
        single_scope = RepositorySearchScope(
            "synthetic-routing-probe",
            (RepositoryEntry(REPOSITORIES[0], True, "main", ("probe",)),),
            SearchLimits(12, 1, 5),
        )
        search = FakeSearchAdapter()

        result = automate_repository_issue(
            generation(),
            {"safety": {"ai_allowed": True, "security_review_required": False}},
            single_scope,
            search,
            "github-tree-probe",
            policy(),
            FakeIssueClient(),
            False,
        )

        self.assertEqual("resolved", result["resolution"]["status"])
        self.assertEqual("github", result["resolution"]["search_audit"]["provider"])
        self.assertGreater(len(search.calls), 0)
        self.assertTrue(
            all(call[0] == REPOSITORIES[0] for call in search.calls)
        )

    def test_single_repository_without_matching_code_is_not_selected(self):
        single_scope = RepositorySearchScope(
            "synthetic-routing-probe",
            (RepositoryEntry(REPOSITORIES[0], True, "main", ("probe",)),),
            SearchLimits(12, 1, 5),
        )

        class EmptySearchAdapter:
            def __init__(self):
                self.calls = []

            def search(self, repository, term, max_hits):
                self.calls.append((repository, term, max_hits))
                return SearchHits(frozenset())

        search = EmptySearchAdapter()
        client = FakeIssueClient()
        result = automate_repository_issue(
            generation(),
            {"safety": {"ai_allowed": True, "security_review_required": False}},
            single_scope,
            search,
            "github-tree-probe",
            policy(),
            client,
            True,
        )

        self.assertEqual("unknown", result["resolution"]["status"])
        self.assertIsNone(result["resolution"]["selected_repository"])
        self.assertGreater(len(search.calls), 0)
        self.assertEqual("blocked", result["publication"]["status"])
        self.assertEqual([], client.create_calls)

    def test_preselected_repository_is_rejected_for_multi_repository_scope(self):
        with self.assertRaisesRegex(ValueError, "single enabled scope"):
            automate_repository_issue(
                generation(),
                {"safety": {"ai_allowed": True, "security_review_required": False}},
                scope(),
                FakeSearchAdapter(),
                "github-tree-probe",
                policy(),
                FakeIssueClient(),
                False,
                preselected_repository=REPOSITORIES[0],
            )

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
        self.assertIn("repository-issue-fingerprint/v2", client.create_calls[0][2])
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

    def test_closed_exact_fingerprint_is_reported_as_completed_without_write(self):
        generated = generation()
        body, _ = render_automated_issue_body(generated, REPOSITORIES[0], policy())
        existing = {
            "number": 7,
            "title": generated["draft"]["title"],
            "body": body,
            "url": f"https://github.com/{REPOSITORIES[0]}/issues/7",
            "state": "CLOSED",
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

        self.assertTrue(result["approval"]["approved"])
        self.assertEqual("closed_issue_candidate", result["issue_match"]["status"])
        self.assertEqual("already_completed", result["publication"]["status"])
        self.assertEqual(existing["url"], result["publication"]["issue_url"])
        self.assertEqual([], client.create_calls)

    def test_open_exact_fingerprint_wins_over_closed_duplicate(self):
        generated = generation()
        body, _ = render_automated_issue_body(generated, REPOSITORIES[0], policy())
        closed = {
            "number": 7,
            "title": generated["draft"]["title"],
            "body": body,
            "url": f"https://github.com/{REPOSITORIES[0]}/issues/7",
            "state": "CLOSED",
        }
        opened = {
            **closed,
            "number": 8,
            "url": f"https://github.com/{REPOSITORIES[0]}/issues/8",
            "state": "OPEN",
        }
        client = FakeIssueClient([closed, opened])

        result = automate_repository_issue(
            generated,
            {"safety": {"ai_allowed": True, "security_review_required": False}},
            scope(),
            FakeSearchAdapter(),
            "github-tree-probe",
            policy(),
            client,
            False,
        )

        self.assertEqual("existing_issue_candidate", result["issue_match"]["status"])
        self.assertEqual("deduplicated", result["publication"]["status"])
        self.assertEqual(opened["url"], result["publication"]["issue_url"])

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

    def test_synonymous_numeric_requirements_share_the_same_fingerprint(self):
        first = requirement_generation(
            "将计算器的计算范围限制在50以内",
            ["计算范围限制在50以内。"],
        )
        second = requirement_generation(
            "计算器输入值不得超过 50",
            ["输入数字最大值为50。"],
        )

        self.assertEqual(
            issue_fingerprint(first, REPOSITORIES[0]),
            issue_fingerprint(second, REPOSITORIES[0]),
        )

    def test_different_numeric_limits_have_different_fingerprints(self):
        fifty = requirement_generation(
            "将计算器的计算范围限制在50以内",
            ["计算范围限制在50以内。"],
        )
        forty = requirement_generation(
            "将计算器的计算范围限制在40以内",
            ["计算范围限制在40以内。"],
        )

        self.assertNotEqual(
            issue_fingerprint(fifty, REPOSITORIES[0]),
            issue_fingerprint(forty, REPOSITORIES[0]),
        )

    def test_new_non_numeric_acceptance_criterion_changes_fingerprint(self):
        base = requirement_generation(
            "将计算器的计算范围限制在50以内",
            ["计算范围限制在50以内。"],
        )
        expanded = requirement_generation(
            "将计算器的计算范围限制在50以内",
            [
                "计算范围限制在50以内。",
                "超过范围时返回明确错误。",
            ],
        )

        self.assertNotEqual(
            issue_fingerprint(base, REPOSITORIES[0]),
            issue_fingerprint(expanded, REPOSITORIES[0]),
        )

    def test_compatible_legacy_fingerprint_reuses_same_numeric_requirement(self):
        original = requirement_generation(
            "将计算器的计算范围限制在50以内",
            ["计算范围限制在50以内。"],
        )
        synonymous = requirement_generation(
            "计算器输入值不得超过 50",
            ["输入数字最大值为50。"],
        )
        existing = {
            "number": 29,
            "title": original["draft"]["title"],
            "body": legacy_issue_body(original, REPOSITORIES[0]),
            "url": f"https://github.com/{REPOSITORIES[0]}/issues/29",
            "state": "CLOSED",
        }

        result = automate_repository_issue(
            synonymous,
            {"safety": {"ai_allowed": True, "security_review_required": False}},
            scope(),
            FakeSearchAdapter(),
            "github-tree-probe",
            policy(),
            FakeIssueClient([existing]),
            False,
        )

        self.assertEqual("already_completed", result["publication"]["status"])
        self.assertEqual(
            LEGACY_FINGERPRINT_VERSION,
            result["issue_match"]["selected"]["fingerprint_version"],
        )

    def test_legacy_fingerprint_does_not_swallow_conflicting_numeric_limit(self):
        fifty = requirement_generation(
            "将计算器的计算范围限制在50以内",
            ["计算范围限制在50以内。"],
        )
        forty = requirement_generation(
            "将计算器的计算范围限制在40以内",
            ["计算范围限制在40以内。"],
        )
        existing = {
            "number": 29,
            "title": fifty["draft"]["title"],
            "body": legacy_issue_body(fifty, REPOSITORIES[0]),
            "url": f"https://github.com/{REPOSITORIES[0]}/issues/29",
            "state": "CLOSED",
        }

        result = automate_repository_issue(
            forty,
            {"safety": {"ai_allowed": True, "security_review_required": False}},
            scope(),
            FakeSearchAdapter(),
            "github-tree-probe",
            policy(),
            FakeIssueClient([existing]),
            False,
        )

        self.assertEqual("new_issue", result["issue_match"]["status"])
        self.assertEqual("approved_not_published", result["publication"]["status"])

    def test_legacy_fingerprint_does_not_swallow_new_acceptance_criterion(self):
        original = requirement_generation(
            "将计算器的计算范围限制在50以内",
            ["计算范围限制在50以内。"],
        )
        expanded = requirement_generation(
            "将计算器的计算范围限制在50以内",
            [
                "计算范围限制在50以内。",
                "超过范围时返回明确错误。",
            ],
        )
        existing = {
            "number": 29,
            "title": original["draft"]["title"],
            "body": legacy_issue_body(original, REPOSITORIES[0]),
            "url": f"https://github.com/{REPOSITORIES[0]}/issues/29",
            "state": "CLOSED",
        }

        result = automate_repository_issue(
            expanded,
            {"safety": {"ai_allowed": True, "security_review_required": False}},
            scope(),
            FakeSearchAdapter(),
            "github-tree-probe",
            policy(),
            FakeIssueClient([existing]),
            False,
        )

        self.assertEqual("new_issue", result["issue_match"]["status"])

    def test_revision_fingerprint_is_versioned_from_parent_and_new_request(self):
        generated = generation()
        base = issue_fingerprint(generated, REPOSITORIES[0])
        generated["revision"] = {
            "schema_version": "issue-revision/v1",
            "parent_issue_url": (
                f"https://github.com/{REPOSITORIES[0]}/issues/7"
            ),
            "request_sha256": "d" * 64,
        }

        revised = issue_fingerprint(generated, REPOSITORIES[0])
        body, _ = render_automated_issue_body(
            generated,
            REPOSITORIES[0],
            policy(),
        )

        self.assertNotEqual(base, revised)
        self.assertEqual(revised, issue_fingerprint(generated, REPOSITORIES[0]))
        self.assertIn("## Revision lineage", body)
        self.assertIn("/issues/7", body)

    def test_revision_does_not_heuristically_reuse_the_parent_issue(self):
        generated = generation()
        parent_body, _ = render_automated_issue_body(
            generated,
            REPOSITORIES[0],
            policy(),
        )
        generated["revision"] = {
            "schema_version": "issue-revision/v1",
            "parent_issue_url": (
                f"https://github.com/{REPOSITORIES[0]}/issues/7"
            ),
            "request_sha256": "e" * 64,
        }
        parent = {
            "number": 7,
            "title": generated["draft"]["title"],
            "body": parent_body,
            "url": f"https://github.com/{REPOSITORIES[0]}/issues/7",
            "state": "CLOSED",
        }
        client = FakeIssueClient([parent])

        result = automate_repository_issue(
            generated,
            {"safety": {"ai_allowed": True, "security_review_required": False}},
            scope(),
            FakeSearchAdapter(),
            "github-tree-probe",
            policy(),
            client,
            False,
        )

        self.assertEqual("new_issue", result["issue_match"]["status"])
        self.assertEqual(
            "approved_not_published",
            result["publication"]["status"],
        )

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
