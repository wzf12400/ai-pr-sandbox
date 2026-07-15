import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from src.ai_issue_generator import (
    DRAFT_SCHEMA_VERSION,
    Completion,
    GatewayConfig,
    compact_evidence,
    generate_issue,
    render_markdown,
    validate_draft,
)


ROOT = Path(__file__).resolve().parents[1]


class FakeProvider:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return Completion(self.outputs.pop(0), "request-test", "model-test", {"total_tokens": 1})


def valid_draft():
    return {
        "schema_version": DRAFT_SCHEMA_VERSION,
        "title": "Symbol instances unexpectedly have __dict__ in 1.7",
        "request_type": "Bug",
        "severity": "Unknown",
        "object": {
            "product": "SymPy",
            "repository": "sympy/sympy",
            "service": "unknown",
            "module": "core",
            "code_object": "Symbol",
            "owner": "unknown",
        },
        "interface": {
            "protocol": "Python API",
            "method": "unknown",
            "path_or_topic": "unknown",
            "upstream": "unknown",
            "downstream": "unknown",
        },
        "error": {
            "error_code": "unknown",
            "exception_type": "unknown",
            "message": "Symbol instances have an empty __dict__ in version 1.7",
        },
        "problem": {
            "background": "Version 1.6.2 did not expose __dict__ on Symbol instances.",
            "reported_hypothesis": "unknown",
            "current_behavior": "Version 1.7 exposes an empty __dict__.",
            "expected_behavior": "unknown",
        },
        "reproduction": {
            "preconditions": "SymPy 1.7",
            "steps": ["Evaluate sympy.Symbol('s').__dict__."],
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
        "missing_information": ["Affected user count", "Owning maintainer"],
        "clarifying_questions": ["Is the behavior reproducible on the latest release?"],
        "confidence": 0.91,
        "evidence": [
            {"claim_path": "$.object.repository", "source_paths": ["$.facts.repository"]},
            {"claim_path": "$.object.module", "source_paths": ["$.facts.labels[0]"]},
            {"claim_path": "$.object.code_object", "source_paths": ["$.facts.title"]},
            {"claim_path": "$.interface.protocol", "source_paths": ["$.facts.body"]},
            {"claim_path": "$.error.exception_type", "source_paths": ["$.facts.body"]},
            {"claim_path": "$.error.message", "source_paths": ["$.facts.body"]},
            {"claim_path": "$.problem.current_behavior", "source_paths": ["$.facts.body"]},
        ],
    }


def passing_review():
    return {
        "verdict": "needs_clarification",
        "unsupported_claim_paths": [],
        "missing_critical_fields": ["interface.path_or_topic"],
        "sensitive_data_detected": False,
        "notes": ["The draft preserves the public issue facts."],
    }


class AIIssueGeneratorTest(unittest.TestCase):
    def setUp(self):
        self.public_issue = json.loads(
            (ROOT / "tests" / "fixtures" / "sympy-20567.json").read_text(encoding="utf-8")
        )

    def test_public_github_input_is_minimized(self):
        compact = compact_evidence(self.public_issue)

        self.assertEqual("github", compact["source"]["type"])
        self.assertEqual("sympy/sympy#20567", compact["source"]["reference"])
        self.assertEqual({"repository", "title", "body", "labels"}, set(compact["facts"]))
        self.assertNotIn("user", json.dumps(compact))
        self.assertNotIn("node_id", json.dumps(compact))

    def test_draft_rejects_extra_fields(self):
        draft = valid_draft()
        draft["automatic_publish"] = True

        errors, _ = validate_draft(draft, compact_evidence(self.public_issue))

        self.assertIn("$.automatic_publish is not allowed", errors)

    def test_generic_evidence_cannot_bypass_ai_safety_gate(self):
        payload = {
            "schema_version": "ai-issue-evidence/v1",
            "source": {"type": "jira", "reference": "TEST-1", "url": ""},
            "safety": {"status": "sanitized", "ai_allowed": False},
            "facts": {"summary": "Example"},
        }

        with self.assertRaisesRegex(ValueError, "not allowed"):
            compact_evidence(payload)

    def test_known_claim_requires_evidence_mapping(self):
        draft = valid_draft()
        draft["evidence"] = [
            item for item in draft["evidence"] if item["claim_path"] != "$.error.message"
        ]

        errors, _ = validate_draft(draft, compact_evidence(self.public_issue))

        self.assertIn("known claim has no source evidence: $.error.message", errors)

    def test_reproduction_steps_reject_traceback_output_and_empty_items(self):
        draft = valid_draft()
        draft["reproduction"]["steps"] = ["Run the example", "Traceback (most recent call last)", ""]

        errors, _ = validate_draft(draft, compact_evidence(self.public_issue))

        self.assertTrue(any("traceback output" in error for error in errors))
        self.assertTrue(any("must not be empty" in error for error in errors))

    def test_speculation_is_rejected_from_factual_fields(self):
        draft = valid_draft()
        draft["problem"]["background"] = "I assume a parent class caused this behavior."

        errors, _ = validate_draft(draft, compact_evidence(self.public_issue))

        self.assertIn("$.problem.background contains speculative language", errors)

    def test_attributed_hypothesis_has_a_dedicated_field(self):
        draft = valid_draft()
        draft["problem"]["reported_hypothesis"] = (
            "The reporter suspects a parent class stopped defining __slots__."
        )
        draft["evidence"].append(
            {"claim_path": "$.problem.reported_hypothesis", "source_paths": ["$.facts.body"]}
        )

        errors, _ = validate_draft(draft, compact_evidence(self.public_issue))

        self.assertFalse(any("reported_hypothesis" in error for error in errors), errors)

    def test_expected_behavior_cannot_be_inferred_from_public_issue_body(self):
        draft = valid_draft()
        draft["problem"]["expected_behavior"] = "Symbol instances should not have __dict__."
        draft["evidence"].append(
            {"claim_path": "$.problem.expected_behavior", "source_paths": ["$.facts.body"]}
        )

        errors, _ = validate_draft(draft, compact_evidence(self.public_issue))

        self.assertIn(
            "expected behavior requires a dedicated expected-behavior evidence field",
            errors,
        )

    def test_sensitive_ai_output_is_rejected_without_echoing_secret(self):
        draft = valid_draft()
        draft["error"]["message"] = "token=do-not-echo-this-value"

        errors, _ = validate_draft(draft, compact_evidence(self.public_issue))

        combined = " ".join(errors)
        self.assertIn("sensitive data detected", combined)
        self.assertNotIn("do-not-echo-this-value", combined)

    def test_generator_and_reviewer_cannot_authorize_actions(self):
        generator = FakeProvider(valid_draft())
        reviewer = FakeProvider(passing_review())

        result = generate_issue(self.public_issue, generator, reviewer)

        self.assertEqual("needs_human_context", result["state"])
        self.assertTrue(result["validation"]["valid"])
        self.assertFalse(result["policy"]["publication_allowed"])
        self.assertFalse(result["policy"]["implementation_allowed"])
        self.assertTrue(result["policy"]["human_confirmation_required"])
        self.assertEqual(1, len(generator.calls))
        self.assertEqual(1, len(reviewer.calls))
        self.assertIn("available_evidence_paths", generator.calls[0]["user_payload"])
        self.assertIn(
            "$.error.message",
            generator.calls[0]["user_payload"][
                "critical_claim_paths_requiring_evidence_when_known"
            ],
        )
        self.assertIn("## Object", render_markdown(result))
        self.assertIn("## Interface", render_markdown(result))
        self.assertIn("## Error", render_markdown(result))

    def test_reviewer_unsupported_claim_blocks_result(self):
        review = passing_review()
        review["verdict"] = "reject"
        review["unsupported_claim_paths"] = ["$.problem.current_behavior"]

        result = generate_issue(self.public_issue, FakeProvider(valid_draft()), FakeProvider(review))

        self.assertEqual("blocked", result["state"])
        self.assertFalse(result["validation"]["valid"])

    def test_reviewer_unknown_placeholder_is_not_treated_as_fabrication(self):
        review = passing_review()
        review["unsupported_claim_paths"] = [
            "$.severity",
            "$.problem.expected_behavior",
            "$.interface.path_or_topic",
            "$.acceptance_criteria",
        ]

        result = generate_issue(self.public_issue, FakeProvider(valid_draft()), FakeProvider(review))

        self.assertEqual("needs_human_context", result["state"])
        self.assertTrue(result["validation"]["valid"])
        self.assertTrue(any("unknown placeholders" in item for item in result["validation"]["warnings"]))

    def test_gateway_configuration_requires_https_and_key(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "AI_BASE_URL"):
                GatewayConfig.from_env()
        with patch.dict(
            os.environ,
            {"AI_BASE_URL": "http://example.test/api/v1", "AI_API_KEY": "test"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "HTTPS"):
                GatewayConfig.from_env()

    def test_gateway_configuration_repr_hides_credentials(self):
        with patch.dict(
            os.environ,
            {
                "AI_BASE_URL": "https://example.test/api/v1",
                "AI_API_KEY": "secret-test-key",
                "AI_SAFETY_IDENTIFIER": "private-user-id",
            },
            clear=True,
        ):
            rendered = repr(GatewayConfig.from_env())

        self.assertNotIn("secret-test-key", rendered)
        self.assertNotIn("private-user-id", rendered)

    def test_result_writes_no_raw_public_issue(self):
        result = generate_issue(
            self.public_issue,
            FakeProvider(valid_draft()),
            FakeProvider(passing_review()),
        )

        encoded = json.dumps(result)
        self.assertNotIn("avatar_url", encoded)
        self.assertNotIn("closed_by", encoded)
        self.assertIn("input_sha256", result)


if __name__ == "__main__":
    unittest.main()
