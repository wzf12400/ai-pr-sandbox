import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src.terminal_control_center import (
    Terminal,
    _fetch_log_candidate,
    _interactive_input,
    _load_or_create_log_key,
    _review_incident,
    _resolved_log_connection,
    _run_interactive_session,
    _run_record,
    _run_resume,
    _run_with_spinner,
    _watch_logs,
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
            "approval_digests": {
                "draft_pr": "a" * 64,
                "issue_only": "c" * 64,
            },
        },
    }


class FakeWorkflow:
    def __init__(self, record):
        self.record = record
        self.approvals = []
        self.approval_modes = []

    def read(self, run_id):
        return self.record

    def approve(self, run_id, digest, *, mode="draft_pr"):
        self.approvals.append((run_id, digest))
        self.approval_modes.append(mode)
        self.record = {
            "run_id": run_id,
            "status": "completed",
            "result": {
                "issue_url": "https://github.com/example/ai-pr-sandbox/issues/1",
                "draft_pr_url": (
                    "https://github.com/example/ai-pr-sandbox/pull/2"
                    if mode == "draft_pr"
                    else None
                ),
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


class FakeInbox:
    def __init__(self):
        self.record = {
            "incident_id": "INC-123456789ABC",
            "status": "pending",
            "services": ["calculator"],
            "event_count": 1,
            "first_seen_at": "2099-01-01T00:00:00Z",
            "last_seen_at": "2099-01-01T00:00:00Z",
            "workflow_run_id": RUN_ID,
            "issue_url": None,
            "draft_pr_url": None,
            "evidence": {"safe": True},
        }

    def get(self, incident_id):
        self.assert_id = incident_id
        return dict(self.record)

    def update(self, incident_id, **changes):
        self.assert_id = incident_id
        self.record.update(changes)
        return dict(self.record)

    def add_context(self, incident_id, context):
        self.record.update(
            {"status": "pending", "workflow_run_id": None, "context": context}
        )
        return dict(self.record)

    def snooze(self, incident_id):
        self.record["status"] = "snoozed"
        return dict(self.record)

    def ignore(self, incident_id):
        self.record["status"] = "ignored"
        return dict(self.record)


class TerminalControlCenterTest(unittest.TestCase):
    def test_spinner_keeps_slow_log_scan_visibly_active(self):
        output = io.StringIO()

        def slow_action():
            time.sleep(0.12)
            return 0

        code = _run_with_spinner(
            Terminal(output, color=True),
            "扫描中",
            slow_action,
        )

        self.assertEqual(code, 0)
        self.assertIn("扫描中", output.getvalue())

    def test_log_mode_reuses_configured_url_and_username(self):
        configured = SimpleNamespace(
            log_source=SimpleNamespace(
                discover_url="https://logs.example.test/discover",
                username="configured-reader",
            )
        )

        self.assertEqual(
            (
                "https://logs.example.test/discover",
                "configured-reader",
            ),
            _resolved_log_connection(
                configured,
                discover_url="",
                username="",
            ),
        )
        self.assertEqual(
            ("https://override.example.test/discover", "override-reader"),
            _resolved_log_connection(
                configured,
                discover_url="https://override.example.test/discover",
                username="override-reader",
            ),
        )

    def test_interactive_commands_are_not_sent_as_change_requests(self):
        for value in ("/logs", "logs", "LOG", "日志", "日志平台"):
            self.assertEqual(("logs", ""), _interactive_input(value))
        for value in ("inbox", "/inbox", "收件箱", "异常收件箱"):
            self.assertEqual(("inbox", ""), _interactive_input(value))
        self.assertEqual(
            ("review", "INC-123456789ABC"),
            _interactive_input("review inc-123456789abc"),
        )
        self.assertEqual(("help", ""), _interactive_input("帮助"))
        self.assertEqual(("exit", ""), _interactive_input("退出"))
        self.assertEqual(("empty", ""), _interactive_input("  "))
        self.assertEqual(
            ("request", "在计算器模块新增乘法功能"),
            _interactive_input("在计算器模块新增乘法功能"),
        )
        with self.assertRaisesRegex(ValueError, "未知终端命令"):
            _interactive_input("/unknown")

    def test_interactive_session_recovers_from_empty_log_password(self):
        output = io.StringIO()
        config = SimpleNamespace(
            log_source=SimpleNamespace(
                discover_url="https://logs.example.test/discover",
                username="reader",
                max_scan_hits=1000,
            )
        )
        args = SimpleNamespace(
            preview_only=False,
            max_scan_hits=None,
            discover_url="",
            username="",
            log_output=Path("logs"),
            log_key=Path("key.json"),
            log_scan_state=Path("cursor.json"),
        )
        answers = iter(["log", "help", "exit"])
        with tempfile.TemporaryDirectory() as directory:
            code = _run_interactive_session(
                root=Path(directory),
                config=config,
                workflow=mock.Mock(),
                inbox=mock.Mock(),
                terminal=Terminal(output, color=False),
                input_fn=lambda _prompt: next(answers),
                password_fn=lambda _prompt: "",
                args=args,
            )

        rendered = output.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("日志平台密码不能为空", rendered)
        self.assertGreaterEqual(rendered.count("功能入口"), 2)
        self.assertIn("会话已结束", rendered)

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
            inbox = mock.Mock()
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
                    inbox=inbox,
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
        self.assertNotIn("原始响应不落盘", output.getvalue())
        self.assertIn("扫描 1 · 有效 1 · 异常 1", output.getvalue())
        inbox.ingest_summary.assert_called_once()

    def test_log_review_issue_only_does_not_select_code_scope(self):
        output = io.StringIO()
        workflow = FakeWorkflow(prepared_record())
        inbox = FakeInbox()

        code = _review_incident(
            incident_id="INC-123456789ABC",
            inbox=inbox,
            workflow=workflow,
            terminal=Terminal(output, color=False),
            input_fn=lambda _prompt: "i",
            preview_only=False,
        )

        self.assertEqual(0, code)
        self.assertEqual(["issue_only"], workflow.approval_modes)
        self.assertEqual([(RUN_ID, "c" * 64)], workflow.approvals)
        self.assertEqual("completed", inbox.record["status"])
        self.assertIsNone(inbox.record["draft_pr_url"])
        self.assertIn("未授权 AI 修改代码", output.getvalue())

    def test_watch_once_persists_candidate_without_remote_workflow(self):
        output = io.StringIO()
        discover_url = (
            "https://logs.example.test/_dashboards/app/discover#/?"
            "_g=(time:(from:now-2h,to:now))&_a=(index:view-1)"
        )
        log_source = SimpleNamespace(
            discover_url=discover_url,
            username="reader",
            interval_seconds=300,
            max_scan_hits=1000,
        )
        config = SimpleNamespace(log_source=log_source)
        store = mock.Mock()
        inbox = mock.Mock()
        inbox.ingest_summary.return_value = {
            "candidates": 1,
            "added": 1,
            "deduplicated": 0,
        }
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "src.terminal_control_center._poll_log_candidates",
            return_value=(
                Path(directory) / "summary.json",
                {"selection": {"scanned_hits": 2}},
            ),
        ) as poll, mock.patch.dict(
            os.environ,
            {"OPENSEARCH_PASSWORD": "process-only-password"},
            clear=True,
        ):
            code = _watch_logs(
                root=Path(directory),
                store=store,
                config=config,
                inbox=inbox,
                terminal=Terminal(output, color=False),
                input_fn=lambda _prompt: "",
                password_fn=lambda _prompt: self.fail("password prompt was unexpected"),
                discover_url="",
                username="",
                output_path=Path(directory) / "logs",
                key_path=Path(directory) / "key.json",
                scan_state_path=Path(directory) / "cursor.json",
                max_scan_hits=1000,
                interval_seconds=None,
                max_runs=1,
            )

        self.assertEqual(0, code)
        store.save_log_source.assert_not_called()
        poll.assert_called_once()
        inbox.ingest_summary.assert_called_once()
        self.assertNotIn("process-only-password", output.getvalue())


if __name__ == "__main__":
    unittest.main()
