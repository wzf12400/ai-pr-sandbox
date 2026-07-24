import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.copilot_code_modifier import ProcessOutput
from src.copilot_issue_provider import (
    CopilotCLIIssueProvider,
    _parse_json_object,
)


class CopilotIssueProviderTest(unittest.TestCase):
    def test_parses_exact_json_and_one_plain_fence(self):
        self.assertEqual({"verdict": "pass"}, _parse_json_object('{"verdict":"pass"}'))
        self.assertEqual(
            {"verdict": "pass"},
            _parse_json_object('```json\n{"verdict":"pass"}\n```'),
        )

    def test_rejects_commentary_around_json(self):
        with self.assertRaisesRegex(ValueError, "valid JSON"):
            _parse_json_object('Here is the result: {"verdict":"pass"}')

    @mock.patch("src.copilot_issue_provider.shutil.which", return_value="/usr/bin/copilot")
    @mock.patch("src.copilot_issue_provider._run_process")
    def test_uses_no_tools_network_memory_or_custom_instructions(
        self, run_process, _which
    ):
        run_process.return_value = ProcessOutput(
            returncode=0,
            stdout=json.dumps({"verdict": "pass"}),
            stderr="",
        )
        provider = CopilotCLIIssueProvider("gpt-5.6-sol")

        with mock.patch.dict(
            os.environ,
            {
                "HOME": "/safe-home",
                "PATH": "/usr/bin",
                "AI_API_KEY": "must-not-reach-copilot",
                "UNRELATED_SECRET": "must-not-reach-copilot",
            },
            clear=True,
        ):
            completion = provider.complete(
                system_prompt="Review evidence.",
                user_payload={"draft": {"title": "Synthetic"}},
                schema_name="review",
                schema={"type": "object"},
            )

        args, cwd, _ = run_process.call_args.args
        environment = run_process.call_args.kwargs["env"]
        joined = " ".join(args)
        self.assertIn("--available-tools=", args)
        self.assertIn("--disable-builtin-mcps", args)
        self.assertIn("--no-custom-instructions", args)
        self.assertIn("--no-remote-export", args)
        self.assertIn("--deny-url", args)
        self.assertNotIn("--allow-all", joined)
        self.assertNotIn("--yolo", joined)
        self.assertNotIn("AI_API_KEY", environment)
        self.assertNotIn("UNRELATED_SECRET", environment)
        self.assertNotEqual(Path("/safe-home"), cwd)
        self.assertEqual({"verdict": "pass"}, completion.content)
        self.assertEqual("gpt-5.6-sol", completion.model)

    @mock.patch("src.copilot_issue_provider.shutil.which", return_value="/usr/bin/copilot")
    @mock.patch("src.copilot_issue_provider._run_process")
    def test_failed_copilot_call_stops_without_parsing_output(
        self, run_process, _which
    ):
        run_process.return_value = ProcessOutput(
            returncode=1,
            stdout='{"verdict":"pass"}',
            stderr="authentication failed",
        )

        with self.assertRaisesRegex(ValueError, "generation failed"):
            CopilotCLIIssueProvider("gpt-5.6-sol").complete(
                system_prompt="Review.",
                user_payload={},
                schema_name="review",
                schema={"type": "object"},
            )


if __name__ == "__main__":
    unittest.main()
