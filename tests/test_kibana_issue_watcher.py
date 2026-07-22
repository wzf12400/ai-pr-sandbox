import contextlib
import io
import os
import unittest
from unittest import mock

from src.kibana_issue_watcher import main


HMAC_KEY = "0123456789abcdef0123456789abcdef"
CONNECTOR_ARGS = [
    "--",
    "--discover-url",
    "https://logs.example.test/_dashboards/app/discover#/?_g=(time:(from:now-2h,to:now))&_a=(index:view)",
    "--username",
    "reader",
    "--generate",
    "--auto-publish-policy",
    "policy.json",
    "--confirm-policy-sha256",
    "0" * 64,
]


class KibanaIssueWatcherTest(unittest.TestCase):
    def test_requires_noninteractive_auto_publish_contract(self):
        with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stderr(
            io.StringIO()
        ):
            code = main(["--max-runs", "1", "--", "--discover-url", "demo"])

        self.assertEqual(code, 2)

    @mock.patch("src.kibana_issue_watcher.kibana_issue_connector.main", return_value=0)
    def test_runs_connector_once_with_stable_in_memory_secrets(self, connector_main):
        with mock.patch.dict(
            os.environ,
            {
                "OPENSEARCH_PASSWORD": "password",
                "AI_API_KEY": "api-key",
                "LOG_SANITIZER_HMAC_KEY": HMAC_KEY,
            },
            clear=True,
        ):
            code = main(["--interval-seconds", "60", "--max-runs", "1", *CONNECTOR_ARGS])

        self.assertEqual(code, 0)
        connector_main.assert_called_once()
        arguments = connector_main.call_args.args[0]
        self.assertIn("--auto-publish-policy", arguments)
        self.assertIn("--name", arguments)
        self.assertNotIn("--prompt-password", arguments)
        self.assertNotIn("--prompt-api-key", arguments)

    @mock.patch("src.kibana_issue_watcher.kibana_issue_connector.main", return_value=0)
    def test_rejects_interactive_secret_prompt_in_watch_mode(self, connector_main):
        with mock.patch.dict(
            os.environ,
            {
                "OPENSEARCH_PASSWORD": "password",
                "AI_API_KEY": "api-key",
                "LOG_SANITIZER_HMAC_KEY": HMAC_KEY,
            },
            clear=True,
        ), contextlib.redirect_stderr(io.StringIO()):
            code = main(["--max-runs", "1", *CONNECTOR_ARGS, "--prompt-api-key"])

        self.assertEqual(code, 2)
        connector_main.assert_not_called()


if __name__ == "__main__":
    unittest.main()
