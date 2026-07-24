"""Codex-style terminal entry for the guarded Issue-to-code workflow."""

from __future__ import annotations

import argparse
import contextlib
import getpass
import io
import json
import os
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, TextIO

from src import kibana_issue_connector, kibana_sanitizer
from src.local_control_center import (
    CONFIG_SCHEMA_VERSION,
    DEFAULT_CONFIG_PATH,
    DEFAULT_RUNS_PATH,
    ControlCenterConfig,
    ControlCenterWorkflow,
    LocalConfigStore,
    _atomic_replace_json,
    inspect_identity,
)
from src.copilot_code_modifier import load_issue_code_policy


DEFAULT_LOG_OUTPUT_PATH = Path(".issue-entry-output/log-intake")
DEFAULT_LOG_KEY_PATH = Path(".issue-entry-state/log-sanitizer-key.json")
TERMINAL_STATES = {"awaiting_approval", "completed", "blocked"}
SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


class Terminal:
    def __init__(self, stream: TextIO = sys.stdout, *, color: Optional[bool] = None):
        self.stream = stream
        detected = bool(getattr(stream, "isatty", lambda: False)())
        self.color = detected if color is None else color

    def _paint(self, code: str, value: str) -> str:
        return f"\033[{code}m{value}\033[0m" if self.color else value

    def line(self, value: str = "") -> None:
        print(value, file=self.stream, flush=True)

    def banner(self) -> None:
        self.line(self._paint("1;36", "╭─ AI Change Control ─────────────────────────╮"))
        self.line("│  自然语言 / 日志异常  →  Issue  →  Draft PR  │")
        self.line(self._paint("1;36", "╰─────────────────────────────────────────────╯"))

    def section(self, title: str) -> None:
        self.line()
        self.line(self._paint("1", f"• {title}"))

    def field(self, name: str, value: str) -> None:
        self.line(f"  {self._paint('2', name.ljust(10))} {value}")

    def ok(self, value: str) -> None:
        self.line(self._paint("32", f"✓ {value}"))

    def warn(self, value: str) -> None:
        self.line(self._paint("33", f"! {value}"))

    def fail(self, value: str) -> None:
        self.line(self._paint("31", f"× {value}"))


def _prompt(input_fn: Callable[[str], str], text: str) -> str:
    return input_fn(f"› {text}").strip()


def _configure_one_repository(
    store: LocalConfigStore,
    root: Path,
    terminal: Terminal,
    input_fn: Callable[[str], str],
) -> ControlCenterConfig:
    identity = inspect_identity(root)
    login = str(identity.get("github", {}).get("login") or "")
    if not login:
        raise ValueError("请先在终端完成 GitHub CLI 登录。")
    if not identity.get("copilot", {}).get("available", False):
        raise ValueError("未检测到可用的 GitHub Copilot CLI。")
    terminal.section("首次配置")
    repository_path = Path(
        _prompt(input_fn, "输入受控仓库的本地绝对路径: ")
    ).expanduser()
    policy = load_issue_code_policy(
        repository_path / ".github" / "issue-code-policy.json"
    )
    payload = {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "github": {"login": login},
        "copilot": {"model": policy.default_model},
        "repositories": [
            {
                "repository": policy.repository,
                "local_path": str(repository_path.resolve()),
                "enabled": True,
            }
        ],
    }
    config = store.save(payload)
    terminal.ok("配置已保存；未保存 GitHub、Copilot 或日志平台密码。")
    return config


def _show_config(
    terminal: Terminal,
    config: ControlCenterConfig,
    identity: Mapping[str, Any],
) -> None:
    terminal.section("运行环境")
    terminal.field("GitHub", config.github_login)
    terminal.field("Copilot", config.copilot_model)
    terminal.field(
        "CLI",
        str(identity.get("copilot", {}).get("version") or "未检测到"),
    )
    for repository in config.enabled_repositories:
        terminal.field("Repository", repository.repository)
        terminal.field("Checkout", repository.local_path)
        terminal.field("Write scope", ", ".join(repository.allowed_write_paths))


def _wait_for_terminal_state(
    workflow: ControlCenterWorkflow,
    run_id: str,
    terminal: Terminal,
    *,
    timeout_seconds: float,
    poll_seconds: float = 0.25,
) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    index = 0
    interactive = terminal.color
    while True:
        record = workflow.read(run_id)
        if record.get("status") in TERMINAL_STATES:
            if interactive:
                terminal.stream.write("\r\033[2K")
                terminal.stream.flush()
            return record
        if time.monotonic() >= deadline:
            raise ValueError("等待本地流程完成超时；请检查本地审计记录。")
        if interactive:
            terminal.stream.write(
                f"\r{SPINNER[index % len(SPINNER)]} "
                f"{'正在生成计划' if record.get('status') == 'preparing' else '正在执行'}"
            )
            terminal.stream.flush()
            index += 1
        time.sleep(poll_seconds)


def _render_preview(terminal: Terminal, record: Mapping[str, Any]) -> None:
    preview = record["preview"]
    terminal.section("待批准计划")
    terminal.field("Issue", str(preview["title"]))
    terminal.field("Repository", str(preview["repository"]))
    terminal.field("Model", str(preview["copilot_model"]))
    terminal.field("Labels", ", ".join(preview["required_labels"]))
    terminal.field("Write scope", ", ".join(preview["allowed_write_paths"]))
    if preview.get("issue_mode") == "reuse_existing":
        terminal.field("Existing", str(preview.get("existing_issue_url") or ""))
    terminal.line()
    terminal.line(str(preview["body"]))
    terminal.line()
    if preview.get("issue_mode") == "reuse_existing":
        terminal.warn(
            "批准后将复用该 Issue、运行 Copilot 和测试，并创建 Draft PR。"
        )
    else:
        terminal.warn("批准后将创建 Issue、运行 Copilot 和测试，并创建 Draft PR。")
    terminal.field("不会执行", "merge / deploy")


def _run_record(
    workflow: ControlCenterWorkflow,
    initial_record: Mapping[str, Any],
    terminal: Terminal,
    input_fn: Callable[[str], str],
    *,
    preview_only: bool,
) -> int:
    prepared = _wait_for_terminal_state(
        workflow,
        str(initial_record["run_id"]),
        terminal,
        timeout_seconds=420,
    )
    if prepared.get("status") == "blocked":
        terminal.fail(str(prepared.get("failure", {}).get("message") or "流程已停止。"))
        terminal.field("Audit", str(workflow._run_dir(prepared["run_id"])))
        return 2
    _render_preview(terminal, prepared)
    if preview_only:
        terminal.ok("预览完成；未执行任何远程写入。")
        return 0
    answer = _prompt(input_fn, "批准并运行到 Draft PR？输入 y 继续 [y/N]: ")
    if answer.casefold() not in {"y", "yes"}:
        terminal.warn("已取消；没有创建 Issue，也没有修改代码。")
        return 0
    executing = workflow.approve(
        str(prepared["run_id"]),
        str(prepared["preview"]["approval_digest"]),
    )
    terminal.section("执行")
    completed = _wait_for_terminal_state(
        workflow,
        str(executing["run_id"]),
        terminal,
        timeout_seconds=1800,
    )
    if completed.get("status") != "completed":
        terminal.fail(str(completed.get("failure", {}).get("message") or "流程已停止。"))
        if completed.get("result", {}).get("issue_url"):
            terminal.field("Issue", str(completed["result"]["issue_url"]))
        terminal.field("Audit", str(workflow._run_dir(completed["run_id"])))
        return 2
    terminal.ok("Draft PR 已创建，自动化在这里停止。")
    terminal.field("Issue", str(completed["result"]["issue_url"]))
    terminal.field("Draft PR", str(completed["result"]["draft_pr_url"]))
    return 0


def _run_resume(
    workflow: ControlCenterWorkflow,
    run_id: str,
    terminal: Terminal,
    input_fn: Callable[[str], str],
    *,
    preview_only: bool,
) -> int:
    record = workflow.prepare_resume(run_id)
    preview = record["resume_preview"]
    terminal.section("恢复已保留的任务")
    terminal.field("Issue", str(preview["issue_url"]))
    terminal.field("Repository", str(preview["repository"]))
    terminal.field("Model", str(preview["copilot_model"]))
    terminal.field("Claim", str(preview["claim_branch"]))
    terminal.field("Attempt", str(preview.get("resume_attempt") or ""))
    if preview.get("remove_empty_work_branch"):
        terminal.field("Cleanup", str(preview.get("work_branch") or ""))
    terminal.warn(
        "原 claim 将保留并被重新核验；批准后继续 Copilot、测试和 Draft PR。"
    )
    terminal.field(
        "不会执行",
        "新建 Issue / 删除 claim / merge / deploy",
    )
    if preview_only:
        workflow.cancel_resume(run_id)
        terminal.ok("恢复预览完成；未运行 Copilot，也没有执行远程写入。")
        return 0
    answer = _prompt(input_fn, "批准从该 claim 继续？输入 y 继续 [y/N]: ")
    if answer.casefold() not in {"y", "yes"}:
        workflow.cancel_resume(run_id)
        terminal.warn("已取消；claim 保持不变，没有运行 Copilot。")
        return 0
    executing = workflow.approve_resume(
        run_id,
        str(preview["approval_digest"]),
    )
    terminal.section("恢复执行")
    completed = _wait_for_terminal_state(
        workflow,
        str(executing["run_id"]),
        terminal,
        timeout_seconds=1800,
    )
    if completed.get("status") != "completed":
        terminal.fail(str(completed.get("failure", {}).get("message") or "流程已停止。"))
        terminal.field("Issue", str(completed.get("result", {}).get("issue_url") or ""))
        terminal.field("Audit", str(workflow._run_dir(run_id)))
        return 2
    terminal.ok("Draft PR 已创建，自动化在这里停止。")
    terminal.field("Issue", str(completed["result"]["issue_url"]))
    terminal.field("Draft PR", str(completed["result"]["draft_pr_url"]))
    return 0


def _load_or_create_log_key(path: Path) -> str:
    if path.exists():
        if path.is_symlink() or path.stat().st_size > 4096:
            raise ValueError("本地日志脱敏密钥文件无效。")
        payload = json.loads(path.read_text(encoding="utf-8"))
        key = str(payload.get("key", "")) if isinstance(payload, dict) else ""
        if len(key.encode("utf-8")) < kibana_sanitizer.MIN_HMAC_KEY_BYTES:
            raise ValueError("本地日志脱敏密钥文件无效。")
        return key
    key = secrets.token_hex(32)
    _atomic_replace_json(
        path,
        {
            "schema_version": "local-log-sanitizer-key/v1",
            "key": key,
        },
    )
    return key


@contextlib.contextmanager
def _temporary_environment(values: Mapping[str, str]):
    previous = {key: os.environ.get(key) for key in values}
    try:
        os.environ.update(values)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _fetch_log_candidate(
    *,
    root: Path,
    terminal: Terminal,
    input_fn: Callable[[str], str],
    discover_url: str,
    username: str,
    output_path: Path,
    key_path: Path,
    password_fn: Callable[[str], str] = getpass.getpass,
) -> Dict[str, Any]:
    discover_url = discover_url or _prompt(
        input_fn,
        "粘贴 OpenSearch Dashboards Discover 完整 URL: ",
    )
    username = username or _prompt(input_fn, "只读日志账号: ")
    password = password_fn("› 日志平台密码（不会保存）: ")
    if not password:
        raise ValueError("日志平台密码不能为空。")
    run_name = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + secrets.token_hex(3)
    )
    output_path = output_path if output_path.is_absolute() else root / output_path
    key_path = key_path if key_path.is_absolute() else root / key_path
    terminal.section("读取日志")
    terminal.line("  只读取最多 50 条错误候选；原始响应不落盘。")
    stdout = io.StringIO()
    stderr = io.StringIO()
    environment = {
        kibana_sanitizer.HMAC_KEY_ENV: _load_or_create_log_key(key_path),
        kibana_issue_connector.PASSWORD_ENV: password,
    }
    with _temporary_environment(environment), contextlib.redirect_stdout(
        stdout
    ), contextlib.redirect_stderr(stderr):
        code = kibana_issue_connector.main(
            [
                "--discover-url",
                discover_url,
                "--username",
                username,
                "--max-candidates",
                "5",
                "--fetch-size",
                "50",
                "--output-dir",
                str(output_path),
                "--name",
                run_name,
            ]
        )
    if code != 0:
        detail = " ".join(stderr.getvalue().split())
        raise ValueError(detail.removeprefix("error: ").strip() or "日志平台读取失败。")
    summary_path = output_path / run_name / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    candidates = summary.get("candidates", [])
    selection = summary.get("selection", {})
    terminal.field("Scanned", str(selection.get("scanned_hits", 0)))
    terminal.field("Sanitized", str(selection.get("eligible_events", 0)))
    terminal.field("Incidents", str(len(candidates)))
    if not candidates:
        raise ValueError("没有可进入 AI 流程的安全错误候选。")
    terminal.section("选择异常")
    for index, item in enumerate(candidates, start=1):
        services = ", ".join(item.get("services", [])) or "unknown"
        terminal.line(
            f"  {index}. {services} · {item.get('event_count', 0)} events · "
            f"{item.get('first_seen_at', '')}"
        )
    selected = _prompt(input_fn, f"选择 1-{len(candidates)} [1]: ") or "1"
    if not selected.isdigit() or not 1 <= int(selected) <= len(candidates):
        raise ValueError("日志候选编号无效。")
    artifact = Path(str(candidates[int(selected) - 1]["artifact"]))
    if not artifact.is_absolute():
        artifact = root / artifact
    return json.loads(artifact.read_text(encoding="utf-8"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the guarded Issue-to-code workflow entirely in the terminal."
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--request", help="Natural-language change request.")
    source.add_argument("--logs", action="store_true", help="Read a sanitized log candidate.")
    source.add_argument("--resume", help="Resume one exact run with a retained claim.")
    parser.add_argument("--discover-url", default="")
    parser.add_argument("--username", default="")
    parser.add_argument("--configure", action="store_true")
    parser.add_argument("--preview-only", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--runs", type=Path, default=DEFAULT_RUNS_PATH)
    parser.add_argument("--log-output", type=Path, default=DEFAULT_LOG_OUTPUT_PATH)
    parser.add_argument("--log-key", type=Path, default=DEFAULT_LOG_KEY_PATH)
    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    input_fn: Callable[[str], str] = input,
    password_fn: Callable[[str], str] = getpass.getpass,
    stream: TextIO = sys.stdout,
) -> int:
    args = build_parser().parse_args(argv)
    root = Path.cwd().resolve()
    config_path = args.config if args.config.is_absolute() else root / args.config
    runs_path = args.runs if args.runs.is_absolute() else root / args.runs
    terminal = Terminal(stream, color=False if args.no_color else None)
    terminal.banner()
    store = LocalConfigStore(config_path)
    try:
        config = store.load()
        if args.configure or config is None:
            config = _configure_one_repository(store, root, terminal, input_fn)
        identity = inspect_identity(root)
        if identity.get("github", {}).get("login") != config.github_login:
            raise ValueError("当前 GitHub 账号与本地配置不一致。")
        if not identity.get("copilot", {}).get("available", False):
            raise ValueError("未检测到可用的 GitHub Copilot CLI。")
        _show_config(terminal, config, identity)
        workflow = ControlCenterWorkflow(store, runs_path)
        if args.resume:
            return _run_resume(
                workflow,
                args.resume,
                terminal,
                input_fn,
                preview_only=args.preview_only,
            )
        use_logs = args.logs
        request = (args.request or "").strip()
        if not args.logs and not request:
            terminal.section("输入")
            request = _prompt(
                input_fn,
                "描述你要改变什么（输入 /logs 读取日志平台）: ",
            )
            use_logs = request.casefold() == "/logs"
        if use_logs:
            evidence = _fetch_log_candidate(
                root=root,
                terminal=terminal,
                input_fn=input_fn,
                discover_url=args.discover_url,
                username=args.username,
                output_path=args.log_output,
                key_path=args.log_key,
                password_fn=password_fn,
            )
            initial = workflow.create_from_evidence(evidence)
        else:
            initial = workflow.create(request)
        terminal.section("生成计划")
        return _run_record(
            workflow,
            initial,
            terminal,
            input_fn,
            preview_only=args.preview_only,
        )
    except KeyboardInterrupt:
        terminal.line()
        terminal.warn("已停止。")
        return 130
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        terminal.fail(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
