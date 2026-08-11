import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from src.repository_issue_automation import GitHubRESTIssueClient


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception, traceback):
        return False

    def read(self):
        return self._body


class GitHubRESTIssueClientTest(unittest.TestCase):
    def test_creates_issue_with_versioned_rest_api(self):
        client = GitHubRESTIssueClient("secret-test-token")
        response = FakeResponse(
            201,
            {
                "number": 42,
                "html_url": "https://github.com/wzf12400/ai-pr-sandbox/issues/42",
            },
        )

        with patch(
            "src.repository_issue_automation.urllib.request.urlopen",
            return_value=response,
        ) as opener:
            issue_url = client.create_issue(
                "wzf12400/ai-pr-sandbox",
                "Calculator divide validation",
                "Sanitized task evidence.",
            )

        self.assertEqual(
            "https://github.com/wzf12400/ai-pr-sandbox/issues/42",
            issue_url,
        )
        request = opener.call_args.args[0]
        self.assertEqual(
            "https://api.github.com/repos/wzf12400/ai-pr-sandbox/issues",
            request.full_url,
        )
        self.assertEqual("POST", request.method)
        self.assertEqual("2026-03-10", request.headers["X-github-api-version"])
        self.assertNotIn("secret-test-token", repr(client))
        self.assertEqual(
            {
                "title": "Calculator divide validation",
                "body": "Sanitized task evidence.",
            },
            json.loads(request.data),
        )

    def test_lists_issues_and_excludes_pull_requests(self):
        client = GitHubRESTIssueClient("secret-test-token")
        response = FakeResponse(
            200,
            [
                {
                    "number": 4,
                    "title": "Existing issue",
                    "body": "body",
                    "html_url": "https://github.com/wzf12400/ai-pr-sandbox/issues/4",
                    "state": "open",
                },
                {
                    "number": 5,
                    "title": "Pull request",
                    "body": "body",
                    "html_url": "https://github.com/wzf12400/ai-pr-sandbox/pull/5",
                    "state": "open",
                    "pull_request": {},
                },
            ],
        )

        with patch(
            "src.repository_issue_automation.urllib.request.urlopen",
            return_value=response,
        ):
            issues = client.list_issues("wzf12400/ai-pr-sandbox", 10)

        self.assertEqual([4], [issue["number"] for issue in issues])

    def test_refetches_one_exact_issue_snapshot(self):
        client = GitHubRESTIssueClient("secret-test-token")
        response = FakeResponse(
            200,
            {
                "number": 42,
                "title": "Calculator divide validation",
                "body": "Sanitized approved Issue body.",
                "html_url": "https://github.com/wzf12400/ai-pr-sandbox/issues/42",
                "state": "open",
            },
        )

        with patch(
            "src.repository_issue_automation.urllib.request.urlopen",
            return_value=response,
        ) as opener:
            issue = client.get_issue("wzf12400/ai-pr-sandbox", 42)

        self.assertEqual(42, issue["number"])
        self.assertEqual("Calculator divide validation", issue["title"])
        self.assertEqual(
            "https://api.github.com/repos/wzf12400/ai-pr-sandbox/issues/42",
            opener.call_args.args[0].full_url,
        )

    def test_applies_only_explicit_repository_owned_labels(self):
        client = GitHubRESTIssueClient("secret-test-token")
        response = FakeResponse(
            200,
            [{"name": "ai-code-approved"}, {"name": "bug"}],
        )

        with patch(
            "src.repository_issue_automation.urllib.request.urlopen",
            return_value=response,
        ) as opener:
            labels = client.add_labels(
                "wzf12400/ai-pr-sandbox",
                42,
                ("ai-code-approved",),
            )

        self.assertEqual(("ai-code-approved",), labels)
        request = opener.call_args.args[0]
        self.assertEqual(
            "https://api.github.com/repos/wzf12400/ai-pr-sandbox/issues/42/labels",
            request.full_url,
        )
        self.assertEqual({"labels": ["ai-code-approved"]}, json.loads(request.data))

    def test_rejects_incomplete_label_result(self):
        client = GitHubRESTIssueClient("secret-test-token")

        with patch(
            "src.repository_issue_automation.urllib.request.urlopen",
            return_value=FakeResponse(200, [{"name": "bug"}]),
        ), self.assertRaisesRegex(ValueError, "every required label"):
            client.add_labels(
                "wzf12400/ai-pr-sandbox",
                42,
                ("ai-code-approved",),
            )

    def test_rejects_sensitive_issue_content_before_network_call(self):
        client = GitHubRESTIssueClient("secret-test-token")

        with patch(
            "src.repository_issue_automation.urllib.request.urlopen"
        ) as opener, self.assertRaisesRegex(ValueError, "sensitive-data"):
            client.create_issue(
                "wzf12400/ai-pr-sandbox",
                "Credential leak",
                "password=do-not-publish",
            )

        opener.assert_not_called()

    def test_http_failure_does_not_expose_response_or_token(self):
        client = GitHubRESTIssueClient("secret-test-token")
        error = urllib.error.HTTPError(
            "https://api.github.com/repos/wzf12400/ai-pr-sandbox/issues",
            403,
            "Forbidden",
            {},
            io.BytesIO(b'{"message":"secret upstream body"}'),
        )

        with patch(
            "src.repository_issue_automation.urllib.request.urlopen",
            side_effect=error,
        ), self.assertRaises(ValueError) as raised:
            client.create_issue(
                "wzf12400/ai-pr-sandbox",
                "Safe title",
                "Safe body",
            )

        message = str(raised.exception)
        self.assertIn("HTTP 403", message)
        self.assertNotIn("secret upstream body", message)
        self.assertNotIn("secret-test-token", message)


if __name__ == "__main__":
    unittest.main()
