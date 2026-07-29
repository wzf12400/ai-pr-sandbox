import errno
import io
import json
import os
import pty
import select
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src.terminal_control_center import (
    Terminal,
    _commit_log_history_cursor,
    _commit_log_scan_cursor,
    _fetch_log_candidate,
    _initial_log_password,
    _interactive_input,
    _load_or_create_log_key,
    _poll_with_auth_retry,
    _review_incident,
    _resolved_log_connection,
    _run_interactive_session,
    _run_record,
    _run_resume,
    _run_with_spinner,
    _store_keychain_log_password,
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
    def test_banner_uses_minimal_pixel_mascot_without_old_flow_box(self):
        output = io.StringIO()

        Terminal(output, color=False).banner()

        rendered = output.getvalue()
        self.assertIn("▄████▄", rendered)
        self.assertIn("输入需求 · help 查看功能", rendered)
        self.assertNotIn("AI Change Control", rendered)
        self.assertNotIn("自然语言 / 日志异常", rendered)

    @unittest.skipUnless(os.name == "posix", "requires a POSIX pseudo-terminal")
    def test_interactive_input_deletes_complete_chinese_characters(self):
        master, slave = pty.openpty()

        def read_master():
            try:
                return os.read(master, 4096)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    return b""
                raise

        repository = Path(__file__).resolve().parents[1]
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "from src import terminal_control_center; "
                    "value = input('PROMPT> '); "
                    "print('VALUE_HEX=' + value.encode('utf-8').hex())"
                ),
            ],
            cwd=repository,
            stdin=slave,
            stdout=slave,
            stderr=slave,
        )
        os.close(slave)
        output = bytearray()
        deadline = time.monotonic() + 10
        try:
            while b"PROMPT> " not in output and time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.1)
                if ready:
                    chunk = read_master()
                    if not chunk:
                        break
                    output.extend(chunk)
            self.assertIn(b"PROMPT> ", output)
            os.write(master, "第一句错误".encode("utf-8"))
            os.write(master, b"\x7f\x7f")
            os.write(master, " 第二句\n".encode("utf-8"))
            while process.poll() is None and time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.1)
                if ready:
                    chunk = read_master()
                    if not chunk:
                        break
                    output.extend(chunk)
            process.wait(timeout=1)
            while True:
                ready, _, _ = select.select([master], [], [], 0)
                if not ready:
                    break
                chunk = read_master()
                if not chunk:
                    break
                output.extend(chunk)
        finally:
            os.close(master)
            if process.poll() is None:
                process.kill()
                process.wait()

        expected = "第一句 第二句".encode("utf-8").hex().encode("ascii")
        self.assertEqual(0, process.returncode, output.decode("utf-8", "replace"))
        self.assertIn(b"VALUE_HEX=" + expected, output)

    def test_log_authentication_retries_without_persisting_passwords(self):
        output = io.StringIO()
        passwords = iter(["wrong-password", "right-password"])
        expected = (Path("/safe/summary.json"), {"candidates": []})
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "src.terminal_control_center._poll_log_candidates",
            side_effect=[
                ValueError(
                    "OpenSearch Dashboards returned HTTP 401: "
                    "Authentication Exception"
                ),
                expected,
            ],
        ) as poll:
            summary_path, summary, password = _poll_with_auth_retry(
                root=Path("/safe"),
                terminal=Terminal(output, color=False),
                password_fn=lambda _prompt: next(passwords),
                initial_password="",
                discover_url="https://logs.example.test/discover",
                username="reader",
                output_path=Path("logs"),
                key_path=Path("key.json"),
                scan_state_path=Path("cursor.json"),
                max_scan_hits=1000,
                initial_scan_hits=30,
            )

        self.assertEqual(summary_path, expected[0])
        self.assertEqual(summary, expected[1])
        self.assertEqual(password, "right-password")
        self.assertEqual(poll.call_count, 2)
        self.assertIn("认证失败，请重新输入", output.getvalue())
        self.assertNotIn("wrong-password", output.getvalue())
        self.assertNotIn("right-password", output.getvalue())

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
        for value in ("log more", "logs more", "继续扫描", "更多日志"):
            self.assertEqual(("logs_more", ""), _interactive_input(value))
        for value in ("log setup", "logs setup", "日志配置"):
            self.assertEqual(("logs_setup", ""), _interactive_input(value))
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

    def test_keychain_password_is_loaded_without_command_line_secret(self):
        discover_url = (
            "https://logs.example.test/_dashboards/app/discover#/?"
            "_g=(time:(from:now-2h,to:now))&_a=(index:view-1)"
        )
        completed = SimpleNamespace(
            returncode=0,
            stdout=b"stored-password\n",
        )
        with mock.patch(
            "src.terminal_control_center.sys.platform",
            "darwin",
        ), mock.patch(
            "src.terminal_control_center.subprocess.run",
            return_value=completed,
        ) as run:
            password = _initial_log_password(discover_url, "reader")

        self.assertEqual("stored-password", password)
        arguments = run.call_args.args[0]
        self.assertNotIn("stored-password", arguments)
        self.assertEqual("find-generic-password", arguments[1])

    def test_keychain_setup_delegates_hidden_prompt_to_security(self):
        discover_url = (
            "https://logs.example.test/_dashboards/app/discover#/?"
            "_g=(time:(from:now-2h,to:now))&_a=(index:view-1)"
        )
        with mock.patch(
            "src.terminal_control_center.sys.platform",
            "darwin",
        ), mock.patch(
            "src.terminal_control_center.subprocess.run",
            return_value=SimpleNamespace(returncode=0),
        ) as run:
            _store_keychain_log_password(discover_url, "reader")

        arguments = run.call_args.args[0]
        self.assertEqual("-w", arguments[-1])
        self.assertEqual("add-generic-password", arguments[1])

    def test_interactive_session_recovers_from_empty_log_password(self):
        output = io.StringIO()
        config = SimpleNamespace(
            log_source=SimpleNamespace(
                discover_url="https://logs.example.test/discover",
                username="reader",
                max_scan_hits=1000,
                initial_scan_hits=30,
            )
        )
        args = SimpleNamespace(
            preview_only=False,
            max_scan_hits=None,
            initial_scan_hits=None,
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
        self.assertIn("已取消日志登录", rendered)
        self.assertEqual(rendered.count("功能入口"), 1)
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

    def test_completed_closed_issue_stops_before_approval_with_issue_url(self):
        output = io.StringIO()
        issue_url = "https://github.com/example/ai-pr-sandbox/issues/24"
        workflow = FakeWorkflow(
            {
                "run_id": RUN_ID,
                "status": "blocked",
                "result": {"issue_url": issue_url, "draft_pr_url": None},
                "failure": {
                    "code": "request_already_completed",
                    "message": "相同需求已由关闭的 GitHub Issue 处理完成。",
                },
            }
        )

        code = _run_record(
            workflow,
            {"run_id": RUN_ID},
            Terminal(output, color=False),
            lambda _prompt: self.fail("approval prompt must not be displayed"),
            preview_only=False,
        )

        self.assertEqual(2, code)
        self.assertEqual([], workflow.approvals)
        self.assertIn("处理完成", output.getvalue())
        self.assertIn(issue_url, output.getvalue())

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

    def test_deferred_cursor_is_committed_from_sanitized_summary_metadata(self):
        discover_url = (
            "https://logs.example.test/_dashboards/app/discover#/?"
            "_g=(time:(from:now-2h,to:now))&_a=(index:view-1)"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cursor_path = root / "cursor.json"
            summary_path = root / "summary.json"
            _commit_log_scan_cursor(
                discover_url=discover_url,
                scan_state_path=cursor_path,
                summary_path=summary_path,
                summary={
                    "source": {
                        "base_url": "https://logs.example.test/_dashboards",
                        "data_view_id": "view-1",
                        "time_from": "now-2h",
                        "time_to": "now",
                    },
                    "query": {
                        "cursor_commit_deferred": True,
                        "batch_completed_through": "2026-07-27T08:02:00.000Z",
                        "effective_time_to": "2026-07-27T08:10:00.000Z",
                        "backlog_remaining": True,
                    }
                },
            )
            cursor = json.loads(cursor_path.read_text(encoding="utf-8"))

        self.assertEqual(
            "2026-07-27T08:02:00.000Z",
            cursor["completed_through"],
        )
        self.assertEqual(
            "2026-07-27T08:10:00.000Z",
            cursor["backlog_target_through"],
        )
        self.assertTrue(cursor["backlog_pending"])

    def test_deferred_history_cursor_is_committed_after_inbox_acknowledgement(self):
        discover_url = (
            "https://logs.example.test/_dashboards/app/discover#/?"
            "_g=(time:(from:now-2h,to:now))&_a=(index:view-1)"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cursor_path = root / "history.json"
            summary_path = root / "summary.json"
            _commit_log_history_cursor(
                discover_url=discover_url,
                history_state_path=cursor_path,
                summary_path=summary_path,
                summary={
                    "source": {
                        "base_url": "https://logs.example.test/_dashboards",
                        "data_view_id": "view-1",
                        "time_from": "now-2h",
                        "time_to": "now",
                    },
                    "query": {
                        "history_cursor_commit_deferred": True,
                        "history_range_from": "2026-07-27T08:00:00.000Z",
                        "history_next_before": "2026-07-27T09:00:00.000Z",
                        "history_pending_from": "",
                        "history_pending_to": "",
                    },
                },
            )
            cursor = json.loads(cursor_path.read_text(encoding="utf-8"))

        self.assertEqual(
            "2026-07-27T09:00:00.000Z",
            cursor["next_before"],
        )
        self.assertEqual("", cursor["pending_from"])

    def test_log_fetch_returns_only_selected_sanitized_artifact(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log_output = root / "logs"

            def fake_connector(arguments):
                self.assertEqual(
                    arguments.count("--find-next-error-window"),
                    1,
                )
                self.assertEqual(
                    arguments[arguments.index("--scan-delay-seconds") + 1],
                    "900",
                )
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

    def test_interactive_log_fetch_stops_after_one_backlog_batch(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_run = root / "logs" / "batch-1"
            first_run.mkdir(parents=True)
            artifact = first_run / "sanitized-incident.json"
            artifact.write_text(
                json.dumps(
                    {
                        "schema_version": "ai-issue-evidence/v1",
                        "safety": {"status": "sanitized", "ai_allowed": True},
                    }
                ),
                encoding="utf-8",
            )
            summaries = [
                (
                    first_run / "summary.json",
                    {
                        "query": {"backlog_remaining": True},
                        "selection": {
                            "scanned_hits": 5000,
                            "eligible_events": 10,
                        },
                        "candidates": [
                            {
                                "artifact": str(artifact),
                                "services": ["calculator"],
                                "event_count": 10,
                                "first_seen_at": "2026-07-27T08:00:00Z",
                            }
                        ],
                    },
                    "temporary-password",
                ),
            ]
            inbox = mock.Mock()
            with mock.patch(
                "src.terminal_control_center._poll_with_auth_retry",
                side_effect=summaries,
            ) as poll:
                evidence = _fetch_log_candidate(
                    root=root,
                    terminal=Terminal(output, color=False),
                    input_fn=lambda _prompt: "1",
                    discover_url="https://logs.example.test/_dashboards/app/discover#x",
                    username="reader",
                    output_path=root / "logs",
                    key_path=root / "log-key.json",
                    inbox=inbox,
                    password_fn=lambda _prompt: "temporary-password",
                )

        self.assertEqual("sanitized", evidence["safety"]["status"])
        self.assertEqual(1, poll.call_count)
        self.assertEqual(1, inbox.ingest_summary.call_count)
        self.assertIn("扫描 5000 · 有效 10 · 异常 1", output.getvalue())
        self.assertIn("下次将从当前游标继续", output.getvalue())

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
            initial_scan_hits=30,
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
                initial_scan_hits=30,
                interval_seconds=None,
                max_runs=1,
            )

        self.assertEqual(0, code)
        store.save_log_source.assert_not_called()
        poll.assert_called_once()
        inbox.ingest_summary.assert_called_once()
        self.assertNotIn("process-only-password", output.getvalue())

    def test_watch_once_stops_after_one_backlog_batch(self):
        output = io.StringIO()
        discover_url = (
            "https://logs.example.test/_dashboards/app/discover#/?"
            "_g=(time:(from:now-2h,to:now))&_a=(index:view-1)"
        )
        log_source = SimpleNamespace(
            discover_url=discover_url,
            username="reader",
            interval_seconds=300,
            max_scan_hits=5000,
            initial_scan_hits=30,
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
                Path(directory) / "batch-1.json",
                {
                    "query": {"backlog_remaining": True},
                    "selection": {"scanned_hits": 5000},
                },
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
                password_fn=lambda _prompt: self.fail(
                    "password prompt was unexpected"
                ),
                discover_url="",
                username="",
                output_path=Path(directory) / "logs",
                key_path=Path(directory) / "key.json",
                scan_state_path=Path(directory) / "cursor.json",
                max_scan_hits=5000,
                initial_scan_hits=30,
                interval_seconds=None,
                max_runs=1,
            )

        self.assertEqual(0, code)
        self.assertEqual(1, poll.call_count)
        self.assertEqual(1, inbox.ingest_summary.call_count)
        self.assertIn("下次轮询将从当前游标继续", output.getvalue())

    def test_watch_does_not_commit_cursor_when_inbox_ingest_fails(self):
        discover_url = (
            "https://logs.example.test/_dashboards/app/discover#/?"
            "_g=(time:(from:now-2h,to:now))&_a=(index:view-1)"
        )
        log_source = SimpleNamespace(
            discover_url=discover_url,
            username="reader",
            interval_seconds=300,
            max_scan_hits=5000,
            initial_scan_hits=30,
        )
        inbox = mock.Mock()
        inbox.ingest_summary.side_effect = ValueError("inbox write failed")
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "src.terminal_control_center._poll_log_candidates",
            return_value=(
                Path(directory) / "batch.json",
                {
                    "query": {
                        "cursor_commit_deferred": True,
                        "backlog_remaining": True,
                    },
                    "selection": {"scanned_hits": 5000},
                },
            ),
        ), mock.patch(
            "src.terminal_control_center._commit_log_scan_cursor"
        ) as commit, mock.patch.dict(
            os.environ,
            {"OPENSEARCH_PASSWORD": "process-only-password"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "inbox write failed"):
                _watch_logs(
                    root=Path(directory),
                    store=mock.Mock(),
                    config=SimpleNamespace(log_source=log_source),
                    inbox=inbox,
                    terminal=Terminal(io.StringIO(), color=False),
                    input_fn=lambda _prompt: "",
                    password_fn=lambda _prompt: "unexpected",
                    discover_url="",
                    username="",
                    output_path=Path(directory) / "logs",
                    key_path=Path(directory) / "key.json",
                    scan_state_path=Path(directory) / "cursor.json",
                    max_scan_hits=5000,
                    initial_scan_hits=30,
                    interval_seconds=None,
                    max_runs=1,
                )

        commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
