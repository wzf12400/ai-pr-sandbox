import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from src.issue_publication_policy import load_policy, policy_summary


def policy_payload():
    return {
        "schema_version": "issue-auto-publish-policy/v1",
        "policy_id": "demo-errors-v1",
        "max_issues_per_run": 2,
        "allowed_states": ["ready_for_human_review", "needs_human_context"],
        "routes": [
            {
                "route_id": "checkout-service",
                "match": {"service": "demo-checkout"},
                "provider": "github_cli",
                "repository": "acme/checkout",
            }
        ],
    }


class IssuePublicationPolicyTest(unittest.TestCase):
    def _write(self, root: Path, payload):
        path = root / "policy.json"
        encoded = json.dumps(payload, sort_keys=True).encode()
        path.write_bytes(encoded)
        return path, hashlib.sha256(encoded).hexdigest()

    def test_loads_digest_bound_policy_and_routes_exact_service(self):
        with tempfile.TemporaryDirectory() as directory:
            path, digest = self._write(Path(directory), policy_payload())
            policy = load_policy(path, digest)

        incident = {
            "members": [{"target": {"service": "demo-checkout"}}]
        }
        route = policy.resolve(incident)

        self.assertIsNotNone(route)
        self.assertEqual(route.repository, "acme/checkout")
        self.assertEqual(route.provider, "github_cli")
        self.assertEqual(policy_summary(policy)["route_count"], 1)

    def test_rejects_changed_policy_when_confirmed_digest_is_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, digest = self._write(root, policy_payload())
            changed = policy_payload()
            changed["routes"][0]["repository"] = "acme/other"
            path.write_text(json.dumps(changed), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "SHA-256"):
                load_policy(path, digest)

    def test_rejects_ambiguous_or_unsupported_routes(self):
        duplicate = policy_payload()
        duplicate["routes"].append(
            {
                "route_id": "duplicate-service",
                "match": {"service": "demo-checkout"},
                "provider": "github_cli",
                "repository": "acme/other",
            }
        )
        unsupported = policy_payload()
        unsupported["routes"][0]["provider"] = "gitlab"

        for payload, error in ((duplicate, "duplicate service"), (unsupported, "only the github_cli")):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                path, digest = self._write(Path(directory), payload)
                with self.assertRaisesRegex(ValueError, error):
                    load_policy(path, digest)


if __name__ == "__main__":
    unittest.main()
