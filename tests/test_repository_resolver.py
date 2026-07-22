import json
import base64
import tempfile
import unittest
from pathlib import Path

from src.repository_resolver import (
    GitHubCLIRepositoryTreeProbeAdapter,
    RepositoryEntry,
    RepositorySearchError,
    SearchHits,
    load_issue_generation,
    load_search_scope,
    plan_search_terms,
    resolve_repository,
)


REPOSITORIES = (
    "example-org/routing-alpha",
    "example-org/routing-beta",
    "example-org/routing-gamma",
)
QUALIFIED_CLASS = "com.example.routing.SyntheticRoutingController"
CLASS_NAME = "SyntheticRoutingController"
METHOD_NAME = "routeIssue"


def issue_result():
    return {
        "schema_version": "ai-issue-generation/v1",
        "state": "ready_for_human_review",
        "source": {"type": "synthetic", "reference": "probe-1", "url": ""},
        "input_sha256": "a" * 64,
        "draft": {
            "schema_version": "ai-issue-draft/v1",
            "title": "Synthetic routing failure",
            "object": {
                "product": "unknown",
                "repository": "unknown",
                "service": "synthetic-routing-service",
                "module": "unknown",
                "code_object": QUALIFIED_CLASS,
                "owner": "unknown",
            },
            "interface": {
                "protocol": "unknown",
                "method": METHOD_NAME,
                "path_or_topic": "/v1/synthetic-routing",
                "upstream": "unknown",
                "downstream": "unknown",
            },
            "error": {
                "error_code": "unknown",
                "exception_type": "SyntheticRoutingException",
                "message": "Synthetic routing failed",
            },
            "problem": {
                "background": "unknown",
                "reported_hypothesis": "unknown",
                "current_behavior": "Synthetic routing failed",
                "expected_behavior": "unknown",
            },
            "reproduction": {
                "preconditions": "unknown",
                "steps": [],
                "frequency": "unknown",
                "reproducible": "unknown",
                "workaround": "unknown",
            },
            "impact": {
                "affected_subjects": "unknown",
                "affected_flow": "unknown",
                "quantity_or_ratio": "unknown",
                "business_risk": "unknown",
            },
            "acceptance_criteria": [],
            "missing_information": [],
            "clarifying_questions": [],
            "confidence": 0.99,
            "evidence": [
                {
                    "claim_path": "$.object.code_object",
                    "source_paths": ["$.facts.qualified_class"],
                },
                {
                    "claim_path": "$.interface.method",
                    "source_paths": ["$.facts.class_method"],
                },
                {
                    "claim_path": "$.error.message",
                    "source_paths": ["$.facts.error"],
                },
                {
                    "claim_path": "$.problem.current_behavior",
                    "source_paths": ["$.facts.error"],
                },
            ],
        },
        "review": {
            "verdict": "pass",
            "unsupported_claim_paths": [],
            "missing_critical_fields": [],
            "sensitive_data_detected": False,
            "notes": [],
        },
        "validation": {"valid": True, "errors": [], "warnings": []},
        "policy": {
            "human_confirmation_required": True,
            "publication_allowed": False,
            "implementation_allowed": False,
        },
        "model_metadata": {},
    }


def scope_payload(max_queries=12, max_candidates=3):
    return {
        "schema_version": "repository-search-scope/v1",
        "scope_id": "synthetic-routing-probe",
        "provider": "github",
        "repositories": [
            {
                "repository": repository,
                "enabled": True,
                "default_branch": "main",
                "labels": ["probe"],
            }
            for repository in REPOSITORIES
        ],
        "limits": {
            "max_queries": max_queries,
            "max_candidate_repositories": max_candidates,
            "max_hits_per_query": 5,
        },
    }


class FakeAdapter:
    def __init__(self, matches=None, fail=False):
        self.matches = matches or {}
        self.fail = fail
        self.calls = []

    def search(self, repository, term, max_hits):
        self.calls.append((repository, term, max_hits))
        if self.fail:
            raise RepositorySearchError("synthetic adapter failure")
        return SearchHits(frozenset(self.matches.get((repository, term), set())))


class FakeTreeProbeAdapter(GitHubCLIRepositoryTreeProbeAdapter):
    def __init__(self, responses):
        entries = [
            RepositoryEntry(REPOSITORIES[0], True, "main", ("probe",)),
            RepositoryEntry(REPOSITORIES[1], True, "main", ("probe",)),
        ]
        super().__init__(entries)
        self.responses = responses
        self.endpoints = []

    def _api_json(self, endpoint, *, allow_empty_repository=False):
        self.endpoints.append(endpoint)
        response = self.responses.get(endpoint)
        if response is None:
            raise AssertionError(f"unexpected endpoint: {endpoint}")
        return response


class RepositoryResolverTest(unittest.TestCase):
    def write_json(self, root, name, payload):
        path = Path(root) / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def load_scope(self, root, payload=None):
        path = self.write_json(root, "scope.json", payload or scope_payload())
        return load_search_scope(path)

    def test_loads_secret_free_search_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            scope = self.load_scope(directory)

        self.assertEqual("synthetic-routing-probe", scope.scope_id)
        self.assertEqual(
            REPOSITORIES,
            tuple(item.repository for item in scope.enabled_repositories),
        )
        self.assertEqual(12, scope.limits.max_queries)

    def test_scope_rejects_case_insensitive_duplicate_repository(self):
        payload = scope_payload()
        payload["repositories"].append(
            {"repository": REPOSITORIES[0].upper(), "enabled": False}
        )
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_json(directory, "scope.json", payload)
            with self.assertRaisesRegex(ValueError, "duplicate repository"):
                load_search_scope(path)

    def test_issue_result_rejects_model_authorization(self):
        payload = issue_result()
        payload["policy"]["publication_allowed"] = True
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_json(directory, "issue.json", payload)
            with self.assertRaisesRegex(ValueError, "unsafe authorization"):
                load_issue_generation(path)

    def test_plans_only_grounded_strong_code_identifiers(self):
        plans = plan_search_terms(issue_result())

        self.assertEqual(["qualified_class", "class_method"], [plan.family for plan in plans])
        self.assertEqual((QUALIFIED_CLASS,), plans[0].search_terms)
        self.assertEqual((CLASS_NAME, METHOD_NAME), plans[1].search_terms)
        self.assertEqual(
            ("$.facts.qualified_class", "$.facts.class_method"),
            plans[1].source_paths,
        )

    def test_http_verb_is_not_treated_as_a_code_method(self):
        payload = issue_result()
        payload["draft"]["interface"]["method"] = "POST"

        plans = plan_search_terms(payload)

        self.assertEqual(["qualified_class"], [plan.family for plan in plans])

    def test_repeated_qualified_classes_do_not_exceed_family_cap(self):
        second_class = "com.example.routing.SecondSyntheticController"
        payload = issue_result()
        payload["draft"]["object"]["code_object"] += f" {second_class}"
        shared_path = {"src/SyntheticRoutingController.java"}
        matches = {
            (REPOSITORIES[0], QUALIFIED_CLASS): shared_path,
            (REPOSITORIES[0], second_class): shared_path,
        }
        payload["draft"]["interface"]["method"] = "POST"
        with tempfile.TemporaryDirectory() as directory:
            result = resolve_repository(
                payload, self.load_scope(directory), FakeAdapter(matches)
            )

        self.assertEqual("ambiguous", result["status"])
        self.assertEqual(40, result["decision"]["top_score"])
        self.assertEqual(1, result["candidates"][0]["strong_families"])

    def test_resolves_one_repository_with_two_strong_families(self):
        shared_path = {"src/SyntheticRoutingController.java"}
        matches = {
            (REPOSITORIES[0], QUALIFIED_CLASS): shared_path,
            (REPOSITORIES[0], CLASS_NAME): shared_path,
            (REPOSITORIES[0], METHOD_NAME): shared_path,
        }
        with tempfile.TemporaryDirectory() as directory:
            scope = self.load_scope(directory)
            adapter = FakeAdapter(matches)
            result = resolve_repository(issue_result(), scope, adapter)

        self.assertEqual("resolved", result["status"])
        self.assertEqual(REPOSITORIES[0], result["selected_repository"])
        self.assertEqual(75, result["decision"]["top_score"])
        self.assertEqual(2, result["candidates"][0]["strong_families"])
        self.assertEqual(9, result["search_audit"]["queries_executed"])
        self.assertNotIn("src/SyntheticRoutingController.java", json.dumps(result))

    def test_equal_strong_matches_remain_ambiguous(self):
        shared_path = {"src/SyntheticRoutingController.java"}
        matches = {}
        for repository in REPOSITORIES[:2]:
            for term in (QUALIFIED_CLASS, CLASS_NAME, METHOD_NAME):
                matches[(repository, term)] = shared_path
        with tempfile.TemporaryDirectory() as directory:
            result = resolve_repository(
                issue_result(), self.load_scope(directory), FakeAdapter(matches)
            )

        self.assertEqual("ambiguous", result["status"])
        self.assertIsNone(result["selected_repository"])
        self.assertEqual(0, result["decision"]["margin"])

    def test_hidden_runner_up_still_prevents_resolution(self):
        shared_path = {"src/SyntheticRoutingController.java"}
        matches = {}
        for repository in REPOSITORIES[:2]:
            for term in (QUALIFIED_CLASS, CLASS_NAME, METHOD_NAME):
                matches[(repository, term)] = shared_path
        with tempfile.TemporaryDirectory() as directory:
            scope = self.load_scope(
                directory, scope_payload(max_candidates=1)
            )
            result = resolve_repository(issue_result(), scope, FakeAdapter(matches))

        self.assertEqual("ambiguous", result["status"])
        self.assertEqual(1, len(result["candidates"]))
        self.assertEqual(2, result["search_audit"]["candidate_repositories_verified"])
        self.assertEqual(0, result["decision"]["margin"])

    def test_no_matches_stay_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            result = resolve_repository(
                issue_result(), self.load_scope(directory), FakeAdapter()
            )

        self.assertEqual("unknown", result["status"])
        self.assertIsNone(result["selected_repository"])
        self.assertEqual([], result["candidates"])

    def test_query_budget_fails_closed_before_search(self):
        with tempfile.TemporaryDirectory() as directory:
            scope = self.load_scope(directory, scope_payload(max_queries=8))
            adapter = FakeAdapter()
            result = resolve_repository(issue_result(), scope, adapter)

        self.assertEqual("blocked", result["status"])
        self.assertEqual([], adapter.calls)
        self.assertEqual(0, result["search_audit"]["queries_executed"])

    def test_adapter_failure_discards_partial_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = FakeAdapter(fail=True)
            result = resolve_repository(
                issue_result(), self.load_scope(directory), adapter
            )

        self.assertEqual("blocked", result["status"])
        self.assertEqual([], result["candidates"])
        self.assertEqual(1, result["search_audit"]["queries_executed"])
        self.assertNotIn("synthetic adapter failure", json.dumps(result))

    def test_tree_probe_verifies_qualified_class_and_method_in_memory(self):
        repository = REPOSITORIES[0]
        path = "src/main/java/com/example/routing/SyntheticRoutingController.java"
        source = (
            "package com.example.routing;\n"
            "public final class SyntheticRoutingController {\n"
            "  void routeIssue() {}\n"
            "}\n"
        )
        responses = {
            f"repos/{repository}": {
                "size": 1,
                "archived": False,
                "pushed_at": "2026-07-22T00:00:00Z",
            },
            f"repos/{repository}/git/trees/main?recursive=1": {
                "truncated": False,
                "tree": [
                    {"type": "blob", "path": path, "sha": "b" * 40, "size": len(source)}
                ],
            },
            f"repos/{repository}/git/blobs/{'b' * 40}": {
                "encoding": "base64",
                "content": base64.b64encode(source.encode()).decode(),
            },
        }
        adapter = FakeTreeProbeAdapter(responses)

        qualified = adapter.search(repository, QUALIFIED_CLASS, 5)
        class_name = adapter.search(repository, CLASS_NAME, 5)
        method = adapter.search(repository, METHOD_NAME, 5)

        self.assertEqual(frozenset({path}), qualified.keys)
        self.assertEqual(qualified, class_name)
        self.assertEqual(qualified, method)
        self.assertEqual(3, len(adapter.endpoints))
        self.assertNotIn(source, repr(qualified))

    def test_tree_probe_treats_never_pushed_repository_as_empty(self):
        repository = REPOSITORIES[1]
        adapter = FakeTreeProbeAdapter(
            {
                f"repos/{repository}": {"size": 0, "archived": False},
                f"repos/{repository}/git/trees/main?recursive=1": {
                    "_empty_repository": True
                },
            }
        )

        hits = adapter.search(repository, QUALIFIED_CLASS, 5)

        self.assertEqual(frozenset(), hits.keys)
        self.assertEqual(
            [
                f"repos/{repository}",
                f"repos/{repository}/git/trees/main?recursive=1",
            ],
            adapter.endpoints,
        )


if __name__ == "__main__":
    unittest.main()
