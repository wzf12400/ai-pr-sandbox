import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.issue_entry import _gateway_config, compose_evidence, publish_issue


HMAC_KEY = b"0123456789abcdef0123456789abcdef"


def kibana_hit():
    return {
        "_id": "event-1",
        "_source": {
            "@timestamp": "2099-01-01T00:00:00Z",
            "message": (
                "[2099-01-01 08:00:00.000] [TID: trace-demo-1] ERROR [worker-1] "
                "com.example.OrderController:87 - com.example.OrderService: createOrder: failed"
            ),
            "kubernetes": {
                "namespace_name": "demo",
                "container_name": "demo",
                "labels": {"app_kubernetes_io/name": "demo"},
            },
        },
    }


class IssueEntryTest(unittest.TestCase):
    def _log_file(self, directory):
        path = Path(directory) / "log.json"
        path.write_text(json.dumps(kibana_hit()), encoding="utf-8")
        return path

    def test_composes_minimized_evidence_from_description_and_log(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = compose_evidence(
                "调用 `OrderController.createOrder` 返回 500",
                self._log_file(directory),
                HMAC_KEY,
            )

        self.assertEqual(evidence["schema_version"], "ai-issue-evidence/v1")
        self.assertEqual(evidence["target"]["service"], "demo")
        self.assertEqual(evidence["target"]["business_method"], "createOrder")
        self.assertTrue(evidence["event"]["is_error"])
        self.assertNotIn("event-1", json.dumps(evidence))

    def test_redacted_credentials_require_security_review(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = compose_evidence(
                "请求失败，Authorization: Bearer secret-demo-token",
                self._log_file(directory),
                HMAC_KEY,
            )

        serialized = json.dumps(evidence)
        self.assertNotIn("secret-demo-token", serialized)
        self.assertTrue(evidence["safety"]["security_review_required"])

    @mock.patch("src.issue_entry.ai_issue_generator.GatewayConfig.from_env")
    @mock.patch("src.issue_entry.getpass.getpass", return_value="temporary-secret")
    def test_api_key_can_be_prompted_without_persisting_in_environment(self, getpass, from_env):
        sentinel = object()
        from_env.return_value = sentinel

        with mock.patch.dict("src.issue_entry.os.environ", {}, clear=True):
            self.assertIs(_gateway_config(True), sentinel)
            self.assertNotIn("AI_API_KEY", os.environ)

        getpass.assert_called_once_with("AI API key: ")
        self.assertEqual(from_env.call_count, 1)

    @mock.patch("src.issue_entry.subprocess.run")
    def test_publish_uses_validated_markdown_and_title(self, run):
        run.side_effect = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "https://github.com/acme/project/issues/12\n", ""),
        ]
        result = {
            "state": "needs_human_context",
            "validation": {"valid": True},
            "draft": {"title": "Demo issue"},
        }
        evidence = {"safety": {"security_review_required": False}}

        url = publish_issue(result, Path("issue.md"), "acme/project", evidence)

        self.assertEqual(url, "https://github.com/acme/project/issues/12")
        self.assertEqual(run.call_args_list[1].args[0][-2:], ["--body-file", "issue.md"])

    @mock.patch("src.issue_entry.subprocess.run")
    def test_security_review_blocks_publication(self, run):
        result = {
            "state": "ready_for_human_review",
            "validation": {"valid": True},
            "draft": {"title": "Demo issue"},
        }
        evidence = {"safety": {"security_review_required": True}}

        with self.assertRaisesRegex(ValueError, "security review"):
            publish_issue(result, Path("issue.md"), "acme/project", evidence)
        run.assert_not_called()

    @mock.patch("src.issue_entry.subprocess.run")
    def test_sanitized_event_security_review_blocks_publication(self, run):
        result = {
            "state": "ready_for_human_review",
            "validation": {"valid": True},
            "draft": {"title": "Demo issue"},
        }
        evidence = {"sanitization": {"security_review_required": True}}

        with self.assertRaisesRegex(ValueError, "security review"):
            publish_issue(result, Path("issue.md"), "acme/project", evidence)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
