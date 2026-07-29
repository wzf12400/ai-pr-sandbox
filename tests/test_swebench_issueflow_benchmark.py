import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.swebench_issueflow_benchmark import (
    handoff_eligible,
    load_tasks,
    run_generation,
    terminal_evidence,
)


def task():
    return {
        "answer_fields_present": False,
        "case_ref": "swebench_codefix_ref:" + "a" * 32,
        "repository": "example/project",
        "problem_statement": "Please add widget support.",
    }


class SwebenchIssueflowBenchmarkTest(unittest.TestCase):
    def test_bin_entrypoint_loads_the_project_module(self):
        root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [str(root / "bin" / "generate-swebench-issueflow"), "--help"],
            cwd="/",
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("terminal agent", completed.stdout)

    def test_terminal_evidence_matches_single_repository_request_semantics(self):
        evidence = terminal_evidence(task())

        self.assertEqual(
            evidence["facts"]["reported_description"],
            evidence["facts"]["requested_change"],
        )
        self.assertEqual("example/project", evidence["facts"]["repository"])
        self.assertEqual("natural_language", evidence["source"]["type"])

    def test_task_with_answer_fields_is_rejected(self):
        payload = task()
        payload["answer_fields_present"] = True
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.jsonl"
            path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "answer-bearing"):
                load_tasks(path)

    def test_handoff_requires_valid_nonrejected_generation(self):
        result = {
            "state": "needs_human_context",
            "validation": {"valid": True},
            "review": {"verdict": "needs_clarification"},
        }
        self.assertTrue(handoff_eligible(result))

        result["review"]["verdict"] = "reject"
        self.assertFalse(handoff_eligible(result))

    def test_input_safety_block_is_isolated_to_one_case(self):
        blocked = task()
        blocked["case_ref"] = "swebench_codefix_ref:" + "b" * 32
        blocked["problem_statement"] = (
            "Investigate QWxhZGRpbjpvcGVuIHNlc2FtZV9yYW5kb21WYWx1ZQ=="
        )
        accepted = task()
        result = {
            "state": "needs_human_context",
            "validation": {"valid": True},
            "review": {"verdict": "needs_clarification"},
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            with mock.patch(
                "src.swebench_issueflow_benchmark.ai_issue_generator.generate_issue",
                return_value=result,
            ), mock.patch(
                "src.swebench_issueflow_benchmark.ai_issue_generator.write_result",
            ):
                summary = run_generation([blocked, accepted], output, object())

        self.assertEqual(1, summary["counts"]["structured_generation_completed"])
        self.assertEqual(
            "input_safety_blocked",
            summary["cases"][0]["reason_category"],
        )
        self.assertTrue(summary["cases"][1]["handoff_eligible"])


if __name__ == "__main__":
    unittest.main()
