import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.terminal_control_center import (
    Terminal,
    _fetch_log_candidate,
    _load_or_create_log_key,
    _run_record,
    _run_resume,
)


RUN_ID = "20260724T120000Z-1234abcd"


def prepared_record():
    return {
        "run_id": RUN_ID,
        "status": "awaiting_approval",
        "preview": {
            "title": "Add calculator multiplication",
            "repository": "example/ai-pr-sandbox",
            "body": "# Add multiplication\n\nSafe reviewed body.",
            "copilot_model": "gpt-5.6-sol",
            "required_labels": ["ai-code-approved"],
            "allowed_write_paths": ["src/**", "tests/**"],
            "approval_digest": "a" * 64,
        },
    }


class FakeWorkflow:
    def __init__(self, record):
        self.record = record
        self.approvals = []

    def read(self, run_id):
        return self.record

    def approve(self, run_id, digest):
        self.approvals.append((run_id, digest))
        self.record = {
            "run_id": run_id,
            "status": "completed",
            "result": {
                "issue_url": "https://github.com/example/ai-pr-sandbox/issues/1",
                "draft_pr_url": "https://github.com/example/ai-pr-sandbox/pull/2",
            },
        }
        return {"run_id": run_id, "status": "executing"}

    def _run_dir(self, run_id):
        return Path("/safe/local/audit") / run_id


class FakeResumeWorkflow(FakeWorkflow):
    def __init__(self):
        super().__init__({"run_id": RUN_ID, "status": "blocked"})
        self.resume_approvals = []
        self.cancelled = []

    def prepare_resume(self, run_id):
        self.record = {
            "run_id": run_id,
            "status": "awaiting_resume_approval",
            "resume_preview": {
                "issue_url": (
                    "https://github.com/example/ai-pr-sandbox/issues/17"
                ),
                "repository": "example/ai-pr-sandbox",
                "copilot_model": "gpt-5.6-sol",
                "claim_branch": "codex/copilot/claims/issue-17-abcd1234",
                "resume_attempt": 2,
                "work_branch": "codex/copilot/issue-17-abcd1234",
                "remove_empty_work_branch": True,
                "approval_digest": "b" * 64,
            },
        }
        return self.record

    def cancel_resume(self, run_id):
        self.cancelled.append(run_id)
        self.record = {"run_id": run_id, "status": "blocked"}
        return self.record

    def approve_resume(self, run_id, digest):
        self.resume_approvals.append((run_id, digest))
        self.record = {
            "run_id": run_id,
            "status": "completed",
            "result": {
                "issue_url": (
                    "https://github.com/example/ai-pr-sandbox/issues/17"
                ),
                "draft_pr_url": (
                    "https://github.com/example/ai-pr-sandbox/pull/18"
                ),
            },
        }
        return {"run_id": run_id, "status": "executing"}


class TerminalControlCenterTest(unittest.TestCase):
    def test_terminal_preview_can_be_cancelled_without_approval(self):
        output = io.StringIO()
        workflow = FakeWorkflow(prepared_record())

        code = _run_record(
            workflow,
            {"run_id": RUN_ID},
            Terminal(output, color=False),
            lambda _prompt: "n",
            preview_only=False,
        )

        self.assertEqual(0, code)
        self.assertEqual([], workflow.approvals)
        self.assertIn("Add calculator multiplication", output.getvalue())
        self.assertIn("没有创建 Issue", output.getvalue())

    def test_one_terminal_approval_runs_to_draft_pr(self):
        output = io.StringIO()
        workflow = FakeWorkflow(prepared_record())

        code = _run_record(
            workflow,
            {"run_id": RUN_ID},
            Terminal(output, color=False),
            lambda _prompt: "y",
            preview_only=False,
        )

        self.assertEqual(0, code)
        self.assertEqual([(RUN_ID, "a" * 64)], workflow.approvals)
        self.assertIn("/pull/2", output.getvalue())
        self.assertIn("不会执行", output.getvalue())

    def test_existing_issue_preview_says_reuse_instead_of_create(self):
        output = io.StringIO()
        record = prepared_record()
        record["preview"].update(
            {
                "issue_mode": "reuse_existing",
                "existing_issue_url": (
                    "https://github.com/example/ai-pr-sandbox/issues/17"
                ),
            }
        )
        workflow = FakeWorkflow(record)

        code = _run_record(
            workflow,
            {"run_id": RUN_ID},
            Terminal(output, color=False),
            lambda _prompt: "n",
            preview_only=False,
        )

        self.assertEqual(0, code)
        self.assertIn("/issues/17", output.getvalue())
        self.assertIn("复用该 Issue", output.getvalue())

    def test_retained_claim_resume_requires_a_fresh_terminal_approval(self):
        output = io.StringIO()
        workflow = FakeResumeWorkflow()

        code = _run_resume(
            workflow,
            RUN_ID,
            Terminal(output, color=False),
            lambda _prompt: "y",
            preview_only=False,
        )

        self.assertEqual(0, code)
        self.assertEqual([(RUN_ID, "b" * 64)], workflow.resume_approvals)
        self.assertIn("/pull/18", output.getvalue())
        self.assertIn("删除 claim", output.getvalue())
        self.assertIn("codex/copilot/issue-17-abcd1234", output.getvalue())
        self.assertIn("Attempt", output.getvalue())

    def test_retained_claim_resume_can_be_cancelled_without_execution(self):
        output = io.StringIO()
        workflow = FakeResumeWorkflow()

        code = _run_resume(
            workflow,
            RUN_ID,
            Terminal(output, color=False),
            lambda _prompt: "n",
            preview_only=False,
        )

        self.assertEqual(0, code)
        self.assertEqual([RUN_ID], workflow.cancelled)
        self.assertEqual([], workflow.resume_approvals)

    def test_local_log_key_is_owner_only_and_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "log-key.json"
            first = _load_or_create_log_key(path)
            second = _load_or_create_log_key(path)
            mode = path.stat().st_mode & 0o777

        self.assertEqual(first, second)
        self.assertGreaterEqual(len(first.encode()), 32)
        self.assertEqual(0o600, mode)

    def test_log_fetch_returns_only_selected_sanitized_artifact(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log_output = root / "logs"

            def fake_connector(arguments):
                name = arguments[arguments.index("--name") + 1]
                output_path = Path(arguments[arguments.index("--output-dir") + 1])
                candidate = output_path / name / "candidate-01"
                candidate.mkdir(parents=True)
                artifact = candidate / "sanitized-incident.json"
                artifact.write_text(
                    json.dumps(
                        {
                            "schema_version": "ai-issue-evidence/v1",
                            "source": {
                                "type": "kibana",
                                "reference": "event_ref:safe",
                                "url": "",
                            },
                            "safety": {
                                "status": "sanitized",
                                "ai_allowed": True,
                            },
                            "facts": {"summary": "safe failure"},
                        }
                    ),
                    encoding="utf-8",
                )
                (output_path / name / "summary.json").write_text(
                    json.dumps(
                        {
                            "selection": {
                                "scanned_hits": 1,
                                "eligible_events": 1,
                            },
                            "candidates": [
                                {
                                    "artifact": str(artifact),
                                    "services": ["calculator"],
                                    "event_count": 1,
                                    "first_seen_at": "2099-01-01T00:00:00Z",
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                return 0

            answers = iter(["1"])
            with mock.patch(
                "src.terminal_control_center.kibana_issue_connector.main",
                side_effect=fake_connector,
            ), mock.patch.dict(os.environ, {}, clear=True):
                evidence = _fetch_log_candidate(
                    root=root,
                    terminal=Terminal(output, color=False),
                    input_fn=lambda _prompt: next(answers),
                    discover_url="https://logs.example.test/_dashboards/app/discover#x",
                    username="reader",
                    output_path=log_output,
                    key_path=root / "log-key.json",
                    password_fn=lambda _prompt: "temporary-password",
                )
                self.assertNotIn("OPENSEARCH_PASSWORD", os.environ)

            persisted = "".join(
                path.read_text(encoding="utf-8")
                for path in root.rglob("*.json")
            )

        self.assertEqual("sanitized", evidence["safety"]["status"])
        self.assertNotIn("temporary-password", persisted)
        self.assertNotIn("temporary-password", output.getvalue())


if __name__ == "__main__":
    unittest.main()
