"""Codex-style terminal entry for the guarded Issue-to-code workflow."""

from __future__ import annotations

import argparse
import contextlib
import getpass
import hashlib
import io
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, TextIO

from src import kibana_issue_connector, kibana_sanitizer
from src.local_control_center import (
    CONFIG_SCHEMA_VERSION,
    DEFAULT_CONFIG_PATH,
    DEFAULT_RUNS_PATH,
    MAX_LOG_INTERVAL_SECONDS,
    MIN_LOG_INTERVAL_SECONDS,
    ControlCenterConfig,
    ControlCenterWorkflow,
    LocalConfigStore,
    _atomic_replace_json,
    inspect_identity,
)
from src.copilot_code_modifier import load_issue_code_policy
from src.log_incident_inbox import LogIncidentInbox


# Importing readline installs Unicode-aware interactive editing for input().
# Without it, the macOS terminal line discipline can delete individual UTF-8
# bytes from Chinese text and leave a malformed character rendered as a space.
try:
    import readline as _readline  # noqa: F401
except ImportError:  # pragma: no cover - platform fallback
    _readline = None


DEFAULT_LOG_OUTPUT_PATH = Path(".issue-entry-output/log-intake")
DEFAULT_LOG_KEY_PATH = Path(".issue-entry-state/log-sanitizer-key.json")
DEFAULT_LOG_INBOX_PATH = Path(".issue-entry-state/log-inbox.json")
DEFAULT_LOG_SCAN_STATE_PATH = Path(".issue-entry-state/log-scan-cursor.json")
DEFAULT_LOG_HISTORY_STATE_PATH = Path(
    ".issue-entry-state/log-history-cursor.json"
)
LOG_INGESTION_SETTLE_DELAY_SECONDS = 900
MAX_DISPLAYED_LOG_CANDIDATES = 20
MAX_LOG_AUTH_ATTEMPTS = 3
MAX_LOG_TRANSIENT_ATTEMPTS = 3
LOG_READ_TIMEOUT_SECONDS = 60
LOG_RETRY_DELAYS_SECONDS = (1.0, 2.0)
TERMINAL_STATES = {"awaiting_approval", "completed", "blocked"}
SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
INTERACTIVE_LOG_ALIASES = frozenset({"/logs", "logs", "log", "日志", "日志平台"})
INTERACTIVE_LOG_MORE_ALIASES = frozenset(
    {
        "/logs-more",
        "logs more",
        "log more",
        "继续扫描",
        "继续日志",
        "更多日志",
    }
)
INTERACTIVE_LOG_SETUP_ALIASES = frozenset(
    {
        "/logs-setup",
        "logs setup",
        "log setup",
        "日志配置",
        "配置日志",
    }
)
INTERACTIVE_INBOX_ALIASES = frozenset({"/inbox", "inbox", "收件箱", "异常收件箱"})
INTERACTIVE_HELP_ALIASES = frozenset({"/help", "help", "帮助", "?"})
INTERACTIVE_EXIT_ALIASES = frozenset({"/exit", "exit", "quit", "q", "退出", "结束"})
LOG_KEYCHAIN_SERVICE = "github-ai-agent-opensearch"
MAX_KEYCHAIN_PASSWORD_BYTES = 16_384

PIXEL_MASCOT_PALETTE = {
    "H": 223,  # horns and teeth
    "M": 233,  # mane and pupils
    "D": 94,   # dark brown hair
    "B": 95,   # face
    "L": 137,  # muzzle
    "E": 230,  # eyes
    "P": 210,  # lips
    "R": 167,  # lip outline
    "A": 173,  # ears
}
PIXEL_MASCOT_ROWS = (
    "..........HHHH.............HHHH.........",
    "..........HLHH.............HLHH.........",
    "..........HLHH.............HLHH.........",
    "..........HLHH.............HLHH.........",
    "..........HLHH.............HLHH.........",
    "..........HLHH.............HLHH.........",
    "...........HHHH.MM......MMHHHH..........",
    "...........HMMMMMMMMMMMMMMHHHH..........",
    "............MMMMDMMMMMMMDMMMH...........",
    "...........MMMMDDDMDDDMDDDMMMM..........",
    "..........MMMDDDBBBBBBBBBBBBMMM.........",
    ".....AAAAMMMMDDBBBBBBBBBBBBBBMMAAAA.....",
    "....AAAAAMMMMDDLLLLLLLLLLLLLLLLAAAAA....",
    "..AAAAAAAADMMDBLLLLLLLLLLLLLLLLBBAAAAA..",
    "........ADDDMMBEEEEMMEEEEEEMMEEEB.......",
    "........DDDDDMBBEEEMMEELEEEMMEEBB.......",
    "........MMDDDDBBBEEEEERLPEEEEELL........",
    ".......DMMDDDDBBLRPPPPPPPPPPPPPPPPPPPPP.",
    "......DDDDDDDDBBRPPPPPPPPPPPPPPPPPPPPPP.",
    "......DDDDDDDDBLRPPPPEEELEEELEEELEEEEPPP",
    "......DMMDDDDDDRPPPPPEEELEEELEEELEEEERR.",
    "......DMMDDDDDDMRRPRLEERLEEELEEELEEEER..",
    ".....DDDDDDDDDDMBRPPPPPPRRRRRRRRRRRRRRR.",
    ".....DDDDDDDDDMMRPPPPPPPPPPPPPPPPPPPPPR.",
    ".....DDDDDDDDDMRPPPPPPPPPPPPPPPPPPPPPPPR",
    "....DDDDDDMMDDMMMPMMMMMMMMMMM.......R...",
    "...DDDDDDDMMDMMMMMMMMMMMMMMMM...........",
    "..DDDDDDDDDDDMMMMMMMMMMMMMMM............",
)


class Terminal:
    def __init__(self, stream: TextIO = sys.stdout, *, color: Optional[bool] = None):
        self.stream = stream
        detected = bool(getattr(stream, "isatty", lambda: False)())
        self.color = detected if color is None else color

    def _paint(self, code: str, value: str) -> str:
        return f"\033[{code}m{value}\033[0m" if self.color else value

    def line(self, value: str = "") -> None:
        print(value, file=self.stream, flush=True)

    def _pixel_row(self, top: str, bottom: str) -> str:
        if not self.color:
            return "".join(
                "█"
                if upper != "." and lower != "."
                else "▀"
                if upper != "."
                else "▄"
                if lower != "."
                else " "
                for upper, lower in zip(top, bottom)
            )
        rendered = []
        start = 0
        while start < len(top):
            pair = (top[start], bottom[start])
            end = start + 1
            while end < len(top) and (top[end], bottom[end]) == pair:
                end += 1
            upper, lower = pair
            count = end - start
            if upper == "." and lower == ".":
                rendered.append(f"\033[0m{' ' * count}")
            elif lower == ".":
                rendered.append(
                    f"\033[0m\033[38;5;{PIXEL_MASCOT_PALETTE[upper]}m"
                    + "▀" * count
                )
            elif upper == ".":
                rendered.append(
                    f"\033[0m\033[38;5;{PIXEL_MASCOT_PALETTE[lower]}m"
                    + "▄" * count
                )
            else:
                rendered.append(
                    f"\033[0m\033[38;5;{PIXEL_MASCOT_PALETTE[upper]}m"
                    f"\033[48;5;{PIXEL_MASCOT_PALETTE[lower]}m"
                    + "▀" * count
                )
            start = end
        return "".join(rendered) + "\033[0m"

    def banner(self) -> None:
        for index in range(0, len(PIXEL_MASCOT_ROWS), 2):
            self.line(
                "  "
                + self._pixel_row(
                    PIXEL_MASCOT_ROWS[index],
                    PIXEL_MASCOT_ROWS[index + 1],
                )
            )
        self.line(self._paint("2", "  输入需求 · help 查看功能"))

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


def _interactive_input(value: str) -> tuple[str, str]:
    text = value.strip()
    normalized = text.casefold()
    if not text:
        return "empty", ""
    if normalized in INTERACTIVE_LOG_MORE_ALIASES:
        return "logs_more", ""
    if normalized in INTERACTIVE_LOG_SETUP_ALIASES:
        return "logs_setup", ""
    if normalized in INTERACTIVE_LOG_ALIASES:
        return "logs", ""
    if normalized in INTERACTIVE_INBOX_ALIASES:
        return "inbox", ""
    if normalized in INTERACTIVE_HELP_ALIASES:
        return "help", ""
    if normalized in INTERACTIVE_EXIT_ALIASES:
        return "exit", ""
    review = re.fullmatch(
        r"(?:/?review|审阅|查看)\s+(INC-[0-9A-Fa-f]{12})",
        text,
        flags=re.IGNORECASE,
    )
    if review:
        return "review", review.group(1).upper()
    if text.startswith("/"):
        raise ValueError("未知终端命令；输入 help 查看可用功能。")
    return "request", text


def _show_interactive_menu(terminal: Terminal) -> None:
    terminal.section("功能入口")
    terminal.line("  直接输入需求          自然语言 → Issue → AI 修改 → Draft PR")
    terminal.line("  logs / 日志           从日志平台读取并选择异常")
    terminal.line("  log more / 继续扫描   向更早的非空日志窗口续扫")
    terminal.line("  log setup / 日志配置  保存日志源并配置系统钥匙串")
    terminal.line("  inbox / 收件箱        查看已脱敏的异常收件箱")
    terminal.line("  review INCIDENT_ID    审阅一个异常并选择处理范围")
    terminal.line("  help / 帮助           再次显示本菜单")
    terminal.line("  exit / 退出           结束当前会话")


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
    discover_url = _prompt(
        input_fn,
        "日志 Discover 完整 URL（暂不配置可直接回车）: ",
    )
    if discover_url:
        payload["logs"] = {
            "discover_url": discover_url,
            "username": _prompt(input_fn, "只读日志账号: "),
            "interval_seconds": 300,
            "max_scan_hits": kibana_issue_connector.DEFAULT_MAX_SCAN_HITS,
            "initial_scan_hits": kibana_issue_connector.DEFAULT_INITIAL_SCAN_HITS,
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
    if config.log_source:
        terminal.field("Log source", config.log_source.base_url)
        terminal.field("Log interval", f"{config.log_source.interval_seconds}s")
        terminal.field("Scan limit", str(config.log_source.max_scan_hits))
        terminal.field("Initial scan", str(config.log_source.initial_scan_hits))


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


def _render_preview(
    terminal: Terminal,
    record: Mapping[str, Any],
    *,
    inbox_choices: bool = False,
) -> None:
    preview = record["preview"]
    terminal.section("待批准计划")
    terminal.field("Issue", str(preview["title"]))
    terminal.field("Repository", str(preview["repository"]))
    terminal.field("Model", str(preview["copilot_model"]))
    terminal.field("Labels", ", ".join(preview["required_labels"]))
    terminal.field("Write scope", ", ".join(preview["allowed_write_paths"]))
    if inbox_choices:
        terminal.warn(
            "输入 a：创建或复用 Issue、授权 AI 修改、运行测试并创建 Draft PR。"
        )
        terminal.warn("输入 i：只创建或复用 Issue，不授权 AI 修改。")
    elif preview.get("issue_mode") == "reuse_existing":
        terminal.field("Existing", str(preview.get("existing_issue_url") or ""))
    if preview.get("revision_of"):
        terminal.field("Revision of", str(preview["revision_of"]))
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
        failure = prepared.get("failure", {})
        result = prepared.get("result", {})
        terminal.fail(str(failure.get("message") or "流程已停止。"))
        if result.get("issue_url"):
            terminal.field("Issue", str(result["issue_url"]))
        terminal.field("Audit", str(workflow._run_dir(prepared["run_id"])))
        if (
            failure.get("code") == "request_already_completed"
            and result.get("issue_url")
            and not preview_only
        ):
            terminal.warn(
                "如有未完成项或新增验收标准，可创建独立修订任务；"
                "不会重开或覆盖原 Issue。"
            )
            action = _prompt(
                input_fn,
                "输入 r 创建修订任务，其他输入返回: ",
            ).casefold()
            if action in {"r", "revision", "修订"}:
                revision_description = _prompt(
                    input_fn,
                    "描述本轮未完成项、新行为或新的验收标准: ",
                )
                revised = workflow.create_revision(
                    str(result["issue_url"]),
                    revision_description,
                )
                terminal.section("生成修订计划")
                return _run_record(
                    workflow,
                    revised,
                    terminal,
                    input_fn,
                    preview_only=preview_only,
                )
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


def _run_with_spinner(
    terminal: Terminal,
    label: str,
    action: Callable[[], int],
) -> int:
    if not terminal.color:
        return action()
    completed = threading.Event()
    result: Dict[str, Any] = {}

    def run() -> None:
        try:
            result["value"] = action()
        except BaseException as exc:
            result["error"] = exc
        finally:
            completed.set()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    index = 0
    try:
        while not completed.wait(0.1):
            terminal.stream.write(
                f"\r{SPINNER[index % len(SPINNER)]} {label}"
            )
            terminal.stream.flush()
            index += 1
    finally:
        terminal.stream.write("\r\033[2K")
        terminal.stream.flush()
    error = result.get("error")
    if error is not None:
        raise error
    return int(result.get("value", 2))


def _resolved_log_connection(
    config: ControlCenterConfig,
    *,
    discover_url: str,
    username: str,
) -> tuple[str, str]:
    configured = config.log_source
    return (
        discover_url or (configured.discover_url if configured else ""),
        username or (configured.username if configured else ""),
    )


def _log_keychain_account(discover_url: str, username: str) -> str:
    target = kibana_issue_connector.parse_discover_url(discover_url)
    identity = (
        f"{target.base_url}\0{target.data_view_id}\0{username.strip()}".encode(
            "utf-8"
        )
    )
    return hashlib.sha256(identity).hexdigest()


def _load_keychain_log_password(discover_url: str, username: str) -> str:
    if sys.platform != "darwin" or not discover_url or not username:
        return ""
    try:
        account = _log_keychain_account(discover_url, username)
    except ValueError:
        return ""
    try:
        result = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-w",
                "-s",
                LOG_KEYCHAIN_SERVICE,
                "-a",
                account,
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    secret = result.stdout.rstrip(b"\r\n")
    if result.returncode != 0 or not secret or len(secret) > MAX_KEYCHAIN_PASSWORD_BYTES:
        return ""
    try:
        return secret.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _store_keychain_log_password(discover_url: str, username: str) -> None:
    if sys.platform != "darwin":
        raise ValueError("系统钥匙串自动登录目前只支持 macOS。")
    account = _log_keychain_account(discover_url, username)
    result = subprocess.run(
        [
            "/usr/bin/security",
            "add-generic-password",
            "-U",
            "-a",
            account,
            "-s",
            LOG_KEYCHAIN_SERVICE,
            "-l",
            "GitHub AI Agent OpenSearch read-only login",
            "-w",
        ],
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("日志密码未保存到系统钥匙串。")


def _initial_log_password(discover_url: str, username: str) -> str:
    return (
        os.environ.get(kibana_issue_connector.PASSWORD_ENV, "")
        or _load_keychain_log_password(discover_url, username)
    )


def _configure_log_access(
    *,
    store: LocalConfigStore,
    config: ControlCenterConfig,
    terminal: Terminal,
    input_fn: Callable[[str], str],
    discover_url: str,
    username: str,
    interval_seconds: Optional[int],
    max_scan_hits: int,
    initial_scan_hits: int,
) -> ControlCenterConfig:
    configured = config.log_source
    resolved_url = discover_url or (
        configured.discover_url if configured is not None else ""
    )
    resolved_username = username or (
        configured.username if configured is not None else ""
    )
    if not resolved_url:
        resolved_url = _prompt(
            input_fn,
            "粘贴 OpenSearch Dashboards Discover 完整 URL: ",
        )
    if not resolved_username:
        resolved_username = _prompt(input_fn, "只读日志账号: ")
    interval = (
        interval_seconds
        if interval_seconds is not None
        else configured.interval_seconds
        if configured is not None
        else 300
    )
    updated = store.save_log_source(
        config,
        discover_url=resolved_url,
        username=resolved_username,
        interval_seconds=interval,
        max_scan_hits=max_scan_hits,
        initial_scan_hits=initial_scan_hits,
    )
    terminal.ok("日志地址和只读账号已保存到本地 ignored 配置。")
    terminal.line("  系统钥匙串将提示一次密码；输入内容不会进入程序参数或配置文件。")
    _store_keychain_log_password(resolved_url, resolved_username)
    terminal.ok("日志密码已保存到系统钥匙串；后续扫描将自动读取。")
    return updated


def _fetch_log_candidate(
    *,
    root: Path,
    terminal: Terminal,
    input_fn: Callable[[str], str],
    discover_url: str,
    username: str,
    output_path: Path,
    key_path: Path,
    scan_state_path: Path = DEFAULT_LOG_SCAN_STATE_PATH,
    history_state_path: Path = DEFAULT_LOG_HISTORY_STATE_PATH,
    history_scan: bool = False,
    max_scan_hits: int = kibana_issue_connector.DEFAULT_MAX_SCAN_HITS,
    initial_scan_hits: int = kibana_issue_connector.DEFAULT_INITIAL_SCAN_HITS,
    inbox: Optional[LogIncidentInbox] = None,
    password_fn: Callable[[str], str] = getpass.getpass,
) -> Dict[str, Any]:
    discover_url = discover_url or _prompt(
        input_fn,
        "粘贴 OpenSearch Dashboards Discover 完整 URL: ",
    )
    username = username or _prompt(input_fn, "只读日志账号: ")
    password = _initial_log_password(discover_url, username)
    displayed_entries: list[tuple[Dict[str, Any], Path]] = []
    displayed_by_key: Dict[str, int] = {}
    total_scanned = 0
    total_eligible = 0
    total_candidates = 0
    batch_number = 0
    while True:
        batch_number += 1
        summary_path, summary, password = _poll_with_auth_retry(
            root=root,
            terminal=terminal,
            password_fn=password_fn,
            initial_password=password,
            discover_url=discover_url,
            username=username,
            output_path=output_path,
            key_path=key_path,
            scan_state_path=scan_state_path,
            history_state_path=history_state_path,
            history_scan=history_scan,
            max_scan_hits=max_scan_hits,
            initial_scan_hits=initial_scan_hits,
        )
        if inbox is not None:
            inbox.ingest_summary(summary_path)
        if history_scan:
            _commit_log_history_cursor(
                discover_url=discover_url,
                history_state_path=history_state_path,
                summary_path=summary_path,
                summary=summary,
            )
        else:
            _commit_log_scan_cursor(
                discover_url=discover_url,
                scan_state_path=scan_state_path,
                summary_path=summary_path,
                summary=summary,
            )
        candidates = summary.get("candidates", [])
        selection = summary.get("selection", {})
        total_scanned += int(selection.get("scanned_hits", 0))
        total_eligible += int(selection.get("eligible_events", 0))
        total_candidates += len(candidates)
        for item in candidates:
            signature = item.get("issue_signature")
            signature = signature if isinstance(signature, dict) else {}
            fingerprint = str(signature.get("fingerprint", "")).strip()
            display_key = (
                fingerprint
                or str(item.get("incident_ref", "")).strip()
                or str(item.get("artifact", "")).strip()
            )
            existing_index = displayed_by_key.get(display_key)
            if existing_index is not None:
                existing, _ = displayed_entries[existing_index]
                existing["event_count"] = int(
                    existing.get("event_count", 0)
                ) + int(item.get("event_count", 0))
                first_seen_values = [
                    value
                    for value in (
                        str(existing.get("first_seen_at", "")),
                        str(item.get("first_seen_at", "")),
                    )
                    if value
                ]
                last_seen_values = [
                    value
                    for value in (
                        str(existing.get("last_seen_at", "")),
                        str(item.get("last_seen_at", "")),
                    )
                    if value
                ]
                existing["first_seen_at"] = (
                    min(first_seen_values) if first_seen_values else ""
                )
                existing["last_seen_at"] = (
                    max(last_seen_values) if last_seen_values else ""
                )
                continue
            if len(displayed_entries) >= MAX_DISPLAYED_LOG_CANDIDATES:
                continue
            displayed_by_key[display_key] = len(displayed_entries)
            displayed_entries.append((dict(item), summary_path.parent))
        if not summary.get("query", {}).get("backlog_remaining", False):
            break
        terminal.warn("本轮达到批次上限；下次将从当前游标继续。")
        break
    collapsed = total_candidates - len(displayed_entries)
    summary_line = (
        f"扫描 {total_scanned} · "
        f"有效 {total_eligible} · "
        f"异常 {len(displayed_entries)}"
    )
    if collapsed > 0:
        summary_line += f" · 同类归并 {collapsed}"
    terminal.ok(summary_line)
    if not displayed_entries:
        query = summary.get("query", {})
        if history_scan and query.get("history_exhausted", False):
            raise ValueError("当前 Discover 时间范围内已没有更早的错误日志。")
        if history_scan:
            raise ValueError(
                "这一批没有可进入 AI 流程的安全错误候选；"
                "可再次输入 log more 继续向更早窗口扫描。"
            )
        raise ValueError(
            "没有可进入 AI 流程的安全错误候选；"
            "输入 log more 可继续向更早窗口扫描。"
        )
    terminal.section("选择异常")
    for index, (item, _summary_parent) in enumerate(displayed_entries, start=1):
        services = ", ".join(item.get("services", [])) or "unknown"
        signature = item.get("issue_signature")
        signature = signature if isinstance(signature, dict) else {}
        components = signature.get("components")
        components = components if isinstance(components, dict) else {}
        endpoints = components.get("paths")
        endpoint = (
            ", ".join(str(value) for value in endpoints[:2])
            if isinstance(endpoints, list) and endpoints
            else "unknown"
        )
        terminal.line(
            f"  {index}. {services} · {endpoint} · "
            f"{item.get('event_count', 0)} 条 · "
            f"{item.get('first_seen_at', '')} → "
            f"{item.get('last_seen_at', '') or item.get('first_seen_at', '')}"
        )
    if total_candidates > len(displayed_entries):
        terminal.line(
            f"  … 其余 {total_candidates - len(displayed_entries)} 条已入收件箱"
        )
    selected = _prompt(input_fn, f"选择 1-{len(displayed_entries)} [1]: ") or "1"
    if not selected.isdigit() or not 1 <= int(selected) <= len(displayed_entries):
        raise ValueError("日志候选编号无效。")
    selected_item, selected_summary_parent = displayed_entries[int(selected) - 1]
    artifact = Path(str(selected_item["artifact"]))
    if not artifact.is_absolute():
        artifact = root / artifact
    resolved = artifact.resolve()
    if not resolved.is_relative_to(selected_summary_parent.resolve()):
        raise ValueError("日志候选证据路径无效。")
    selected_evidence = json.loads(resolved.read_text(encoding="utf-8"))
    if inbox is not None:
        signature = selected_item.get("issue_signature")
        signature = signature if isinstance(signature, dict) else {}
        record = inbox.find(
            source_reference=str(selected_item.get("incident_ref", "")),
            issue_fingerprint=str(signature.get("fingerprint", "")),
        )
        if isinstance(record, dict) and isinstance(record.get("evidence"), dict):
            return dict(record["evidence"])
    return selected_evidence


def _poll_log_candidates(
    *,
    root: Path,
    terminal: Terminal,
    discover_url: str,
    username: str,
    password: str,
    output_path: Path,
    key_path: Path,
    scan_state_path: Path,
    history_state_path: Path,
    history_scan: bool,
    max_scan_hits: int,
    initial_scan_hits: int,
) -> tuple[Path, Dict[str, Any]]:
    run_name = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + secrets.token_hex(3)
    )
    output_path = output_path if output_path.is_absolute() else root / output_path
    key_path = key_path if key_path.is_absolute() else root / key_path
    scan_state_path = (
        scan_state_path if scan_state_path.is_absolute() else root / scan_state_path
    )
    history_state_path = (
        history_state_path
        if history_state_path.is_absolute()
        else root / history_state_path
    )
    stdout = io.StringIO()
    stderr = io.StringIO()
    environment = {
        kibana_sanitizer.HMAC_KEY_ENV: _load_or_create_log_key(key_path),
        kibana_issue_connector.PASSWORD_ENV: password,
    }

    def run_connector() -> int:
        with _temporary_environment(environment), contextlib.redirect_stdout(
            stdout
        ), contextlib.redirect_stderr(stderr):
            arguments = [
                "--discover-url",
                discover_url,
                "--username",
                username,
                "--max-candidates",
                str(max_scan_hits),
                "--fetch-size",
                str(kibana_issue_connector.DEFAULT_FETCH_SIZE),
                "--max-scan-hits",
                str(max_scan_hits),
                "--initial-scan-hits",
                str(initial_scan_hits),
                "--timeout-seconds",
                str(LOG_READ_TIMEOUT_SECONDS),
            ]
            if history_scan:
                arguments.extend(
                    [
                        "--history-state-file",
                        str(history_state_path),
                    ]
                )
            else:
                arguments.extend(
                    [
                        "--scan-state-file",
                        str(scan_state_path),
                        "--find-next-error-window",
                    ]
                )
            arguments.extend(
                [
                    "--scan-delay-seconds",
                    str(LOG_INGESTION_SETTLE_DELAY_SECONDS),
                    "--defer-cursor-commit",
                    "--output-dir",
                    str(output_path),
                    "--name",
                    run_name,
                ]
            )
            return kibana_issue_connector.main(arguments)

    code = _run_with_spinner(terminal, "扫描中", run_connector)
    if code != 0:
        detail = " ".join(stderr.getvalue().split())
        raise ValueError(detail.removeprefix("error: ").strip() or "日志平台读取失败。")
    summary_path = output_path / run_name / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    return summary_path, summary


def _commit_log_scan_cursor(
    *,
    discover_url: str,
    scan_state_path: Path,
    summary_path: Path,
    summary: Dict[str, Any],
) -> None:
    query = summary.get("query", {})
    deferred = query.get("cursor_commit_deferred", False)
    if not isinstance(deferred, bool):
        raise ValueError("日志扫描摘要的 cursor 状态无效。")
    if not deferred:
        return
    target = kibana_issue_connector.parse_discover_url(discover_url)
    source = summary.get("source", {})
    if (
        not isinstance(source, dict)
        or source.get("base_url") != target.base_url
        or source.get("data_view_id") != target.data_view_id
        or source.get("time_from") != target.time_from
        or source.get("time_to") != target.time_to
    ):
        raise ValueError("日志扫描摘要与配置的数据源不一致。")
    completed_through = kibana_issue_connector._parse_utc_timestamp(
        str(query.get("batch_completed_through", ""))
    )
    backlog_remaining = bool(query.get("backlog_remaining", False))
    effective_time_to = kibana_issue_connector._parse_utc_timestamp(
        str(query.get("effective_time_to", ""))
    )
    if not backlog_remaining and completed_through != effective_time_to:
        raise ValueError("日志扫描摘要的完成边界无效。")
    kibana_issue_connector._save_scan_cursor(
        scan_state_path,
        target,
        completed_through,
        summary_path,
        backlog_pending=backlog_remaining,
        backlog_target_through=(
            effective_time_to if backlog_remaining else None
        ),
    )


def _commit_log_history_cursor(
    *,
    discover_url: str,
    history_state_path: Path,
    summary_path: Path,
    summary: Dict[str, Any],
) -> None:
    query = summary.get("query", {})
    deferred = query.get("history_cursor_commit_deferred", False)
    if not isinstance(deferred, bool):
        raise ValueError("日志历史续扫摘要的 cursor 状态无效。")
    if not deferred:
        return
    target = kibana_issue_connector.parse_discover_url(discover_url)
    source = summary.get("source", {})
    if (
        not isinstance(source, dict)
        or source.get("base_url") != target.base_url
        or source.get("data_view_id") != target.data_view_id
        or source.get("time_from") != target.time_from
        or source.get("time_to") != target.time_to
    ):
        raise ValueError("日志历史续扫摘要与配置的数据源不一致。")
    range_from = kibana_issue_connector._parse_utc_timestamp(
        str(query.get("history_range_from", ""))
    )
    next_before = kibana_issue_connector._parse_utc_timestamp(
        str(query.get("history_next_before", ""))
    )
    pending_from_value = str(query.get("history_pending_from", "")).strip()
    pending_to_value = str(query.get("history_pending_to", "")).strip()
    pending_from = (
        kibana_issue_connector._parse_utc_timestamp(pending_from_value)
        if pending_from_value
        else None
    )
    pending_to = (
        kibana_issue_connector._parse_utc_timestamp(pending_to_value)
        if pending_to_value
        else None
    )
    kibana_issue_connector._save_history_cursor(
        history_state_path,
        target,
        range_from=range_from,
        next_before=next_before,
        summary_path=summary_path,
        pending_from=pending_from,
        pending_to=pending_to,
    )


def _poll_with_auth_retry(
    *,
    root: Path,
    terminal: Terminal,
    password_fn: Callable[[str], str],
    initial_password: str,
    discover_url: str,
    username: str,
    output_path: Path,
    key_path: Path,
    scan_state_path: Path,
    max_scan_hits: int,
    initial_scan_hits: int,
    history_state_path: Path = DEFAULT_LOG_HISTORY_STATE_PATH,
    history_scan: bool = False,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> tuple[Path, Dict[str, Any], str]:
    password = initial_password or password_fn("› 日志密码: ")
    if not password:
        raise ValueError("已取消日志登录。")
    terminal.section("读取日志")
    authentication_failures = 0
    transient_failures = 0
    while True:
        try:
            summary_path, summary = _poll_log_candidates(
                root=root,
                terminal=terminal,
                discover_url=discover_url,
                username=username,
                password=password,
                output_path=output_path,
                key_path=key_path,
                scan_state_path=scan_state_path,
                history_state_path=history_state_path,
                history_scan=history_scan,
                max_scan_hits=max_scan_hits,
                initial_scan_hits=initial_scan_hits,
            )
            return summary_path, summary, password
        except ValueError as exc:
            detail = str(exc)
            authentication_failed = (
                "HTTP 401" in detail
                or "credentials were not accepted" in detail
            )
            if authentication_failed:
                authentication_failures += 1
                if authentication_failures >= MAX_LOG_AUTH_ATTEMPTS:
                    raise ValueError(
                        "日志认证连续失败，请检查配置账号或重置密码。"
                    ) from None
                terminal.warn("认证失败，请重新输入。")
                password = password_fn("› 日志密码: ")
                if not password:
                    raise ValueError("已取消日志登录。") from None
                continue
            transient_failure = (
                detail.startswith("OpenSearch Dashboards read timed out after ")
                or detail == "OpenSearch Dashboards request failed"
                or any(
                    f"HTTP {status}" in detail
                    for status in (429, 502, 503, 504)
                )
            )
            if not transient_failure:
                raise
            transient_failures += 1
            if transient_failures >= MAX_LOG_TRANSIENT_ATTEMPTS:
                raise ValueError(
                    "日志平台连续读取超时或暂时不可用；游标未推进，"
                    "请稍后再次输入 log。"
                ) from None
            delay = LOG_RETRY_DELAYS_SECONDS[transient_failures - 1]
            terminal.warn(
                "日志读取超时或连接中断，"
                f"{delay:g} 秒后自动重试"
                f"（{transient_failures}/{MAX_LOG_TRANSIENT_ATTEMPTS - 1}）。"
            )
            sleep_fn(delay)


def _show_inbox(terminal: Terminal, inbox: LogIncidentInbox) -> int:
    records = inbox.list()
    terminal.section("异常收件箱")
    if not records:
        terminal.ok("当前没有待处理异常。")
        return 0
    for item in records:
        services = ", ".join(item.get("services", [])) or "unknown"
        endpoints = ", ".join(item.get("affected_endpoints", [])) or "unknown"
        terminal.line(
            f"  {item['incident_id']}  {str(item['status']).ljust(17)} "
            f"{services} · {endpoints} · "
            f"本批 {item.get('latest_batch_event_count', 0)} / "
            f"累计 {item.get('total_event_count', item.get('event_count', 0))} 条 · "
            f"{item.get('last_seen_at', '')}"
        )
    terminal.line()
    terminal.line("  查看并审批：./bin/ai-agent review INCIDENT_ID")
    return 0


def _review_nonapproval_action(
    *,
    action: str,
    incident_id: str,
    inbox: LogIncidentInbox,
    terminal: Terminal,
    input_fn: Callable[[str], str],
) -> int:
    if action == "e":
        context = _prompt(
            input_fn,
            "补充目标功能、预期行为、验收标准或代码线索: ",
        )
        inbox.add_context(incident_id, context)
        terminal.ok("上下文已脱敏保存；下次 review 会重新生成计划。")
        return 0
    if action == "s":
        inbox.snooze(incident_id)
        terminal.ok("已稍后处理 24 小时。")
        return 0
    if action == "x":
        inbox.ignore(incident_id)
        terminal.ok("已忽略；后续重复日志不会重新激活该异常。")
        return 0
    raise ValueError("操作无效。")


def _review_incident(
    *,
    incident_id: str,
    inbox: LogIncidentInbox,
    workflow: ControlCenterWorkflow,
    terminal: Terminal,
    input_fn: Callable[[str], str],
    preview_only: bool,
) -> int:
    incident = inbox.get(incident_id)
    terminal.section("异常证据")
    terminal.field("Incident", incident_id)
    terminal.field("Status", str(incident.get("status", "")))
    terminal.field("Service", ", ".join(incident.get("services", [])) or "unknown")
    terminal.field(
        "Events",
        (
            f"本批 {incident.get('latest_batch_event_count', 0)} / "
            f"累计 {incident.get('total_event_count', incident.get('event_count', 0))}"
        ),
    )
    terminal.field("First seen", str(incident.get("first_seen_at", "")))
    terminal.field("Last seen", str(incident.get("last_seen_at", "")))
    terminal.field(
        "Endpoints",
        ", ".join(incident.get("affected_endpoints", [])) or "unknown",
    )
    user_min = incident.get("affected_user_count_min")
    user_max = incident.get("affected_user_count_max")
    if isinstance(user_min, int) and isinstance(user_max, int):
        affected_users = (
            str(user_min) if user_min == user_max else f"{user_min}-{user_max}"
        )
    else:
        affected_users = "unknown"
    terminal.field("Affected users", affected_users)

    if incident.get("status") == "ignored":
        terminal.warn("该异常已忽略；如需恢复，请重新补充上下文。")
        return 0
    if incident.get("status") == "completed":
        terminal.ok("该异常已经处理完成。")
        if incident.get("issue_url"):
            terminal.field("Issue", str(incident["issue_url"]))
        if incident.get("draft_pr_url"):
            terminal.field("Draft PR", str(incident["draft_pr_url"]))
        return 0

    run_id = str(incident.get("workflow_run_id") or "")
    prepared: Dict[str, Any]
    if run_id:
        prepared = workflow.read(run_id)
    else:
        initial = workflow.create_from_evidence(incident["evidence"])
        run_id = str(initial["run_id"])
        inbox.update(
            incident_id,
            status="preparing",
            workflow_run_id=run_id,
            failure=None,
        )
        terminal.section("生成计划")
        prepared = _wait_for_terminal_state(
            workflow,
            run_id,
            terminal,
            timeout_seconds=420,
        )

    if prepared.get("status") == "preparing":
        prepared = _wait_for_terminal_state(
            workflow,
            run_id,
            terminal,
            timeout_seconds=420,
        )
    if prepared.get("status") == "executing":
        prepared = _wait_for_terminal_state(
            workflow,
            run_id,
            terminal,
            timeout_seconds=1800,
        )
    if prepared.get("status") == "completed":
        result = prepared.get("result", {})
        inbox.update(
            incident_id,
            status="completed",
            issue_url=result.get("issue_url"),
            draft_pr_url=result.get("draft_pr_url"),
            failure=None,
        )
        terminal.ok("该异常已经处理完成。")
        terminal.field("Issue", str(result.get("issue_url") or ""))
        if result.get("draft_pr_url"):
            terminal.field("Draft PR", str(result["draft_pr_url"]))
        return 0
    if prepared.get("status") == "blocked":
        failure = prepared.get("failure", {})
        inbox.update(
            incident_id,
            status="blocked",
            issue_url=prepared.get("result", {}).get("issue_url"),
            failure={
                "code": str(failure.get("code", "unexpected_failure")),
                "message": str(failure.get("message", "流程已停止。")),
            },
        )
        terminal.fail(str(failure.get("message") or "流程已停止。"))
        terminal.field("Audit", str(workflow._run_dir(run_id)))
        if preview_only:
            return 2
        action = _prompt(input_fn, "输入 e 补充上下文 / s 稍后处理 / x 忽略: ").casefold()
        return _review_nonapproval_action(
            action=action,
            incident_id=incident_id,
            inbox=inbox,
            terminal=terminal,
            input_fn=input_fn,
        )
    if prepared.get("status") != "awaiting_approval":
        raise ValueError("异常关联流程状态无效。")

    inbox.update(incident_id, status="awaiting_approval", failure=None)
    _render_preview(terminal, prepared, inbox_choices=True)
    if preview_only:
        terminal.ok("预览完成；未执行任何远程写入。")
        return 0
    action = _prompt(
        input_fn,
        "输入 a 全流程 / i 仅 Issue / e 补充 / s 稍后 / x 忽略: ",
    ).casefold()
    if action in {"e", "s", "x"}:
        return _review_nonapproval_action(
            action=action,
            incident_id=incident_id,
            inbox=inbox,
            terminal=terminal,
            input_fn=input_fn,
        )
    modes = {"a": "draft_pr", "i": "issue_only"}
    mode = modes.get(action)
    if mode is None:
        terminal.warn("已取消；没有执行远程写入。")
        return 0
    digests = prepared["preview"].get("approval_digests", {})
    digest = str(digests.get(mode, ""))
    if not digest:
        raise ValueError("该计划不支持所选审批范围。")
    executing = workflow.approve(run_id, digest, mode=mode)
    inbox.update(
        incident_id,
        status="executing",
        approval={
            "mode": mode,
            "approval_digest": digest,
            "approved_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        },
    )
    terminal.section("执行")
    completed = _wait_for_terminal_state(
        workflow,
        str(executing["run_id"]),
        terminal,
        timeout_seconds=1800,
    )
    result = completed.get("result", {})
    if completed.get("status") != "completed":
        failure = completed.get("failure", {})
        inbox.update(
            incident_id,
            status="blocked",
            issue_url=result.get("issue_url"),
            draft_pr_url=None,
            failure={
                "code": str(failure.get("code", "unexpected_failure")),
                "message": str(failure.get("message", "流程已停止。")),
            },
        )
        terminal.fail(str(failure.get("message") or "流程已停止。"))
        if result.get("issue_url"):
            terminal.field("Issue", str(result["issue_url"]))
        terminal.field("Audit", str(workflow._run_dir(run_id)))
        return 2
    inbox.update(
        incident_id,
        status="completed",
        issue_url=result.get("issue_url"),
        draft_pr_url=result.get("draft_pr_url"),
        failure=None,
    )
    if mode == "issue_only":
        terminal.ok("Issue 已创建；未授权 AI 修改代码。")
    else:
        terminal.ok("Draft PR 已创建，自动化在这里停止。")
    terminal.field("Issue", str(result.get("issue_url") or ""))
    if result.get("draft_pr_url"):
        terminal.field("Draft PR", str(result["draft_pr_url"]))
    return 0


def _watch_logs(
    *,
    root: Path,
    store: LocalConfigStore,
    config: ControlCenterConfig,
    inbox: LogIncidentInbox,
    terminal: Terminal,
    input_fn: Callable[[str], str],
    password_fn: Callable[[str], str],
    discover_url: str,
    username: str,
    output_path: Path,
    key_path: Path,
    scan_state_path: Path,
    max_scan_hits: int,
    initial_scan_hits: int,
    interval_seconds: Optional[int],
    max_runs: int,
) -> int:
    configured = config.log_source
    resolved_url = discover_url or (configured.discover_url if configured else "")
    resolved_username = username or (configured.username if configured else "")
    if not resolved_url:
        resolved_url = _prompt(
            input_fn,
            "粘贴 OpenSearch Dashboards Discover 完整 URL: ",
        )
    if not resolved_username:
        resolved_username = _prompt(input_fn, "只读日志账号: ")
    interval = (
        interval_seconds
        if interval_seconds is not None
        else configured.interval_seconds
        if configured
        else 300
    )
    if not MIN_LOG_INTERVAL_SECONDS <= interval <= MAX_LOG_INTERVAL_SECONDS:
        raise ValueError("日志轮询间隔必须在 60 到 3600 秒之间。")
    if (
        configured is None
        or configured.discover_url != resolved_url
        or configured.username != resolved_username
        or configured.interval_seconds != interval
        or configured.max_scan_hits != max_scan_hits
        or configured.initial_scan_hits != initial_scan_hits
    ):
        config = store.save_log_source(
            config,
            discover_url=resolved_url,
            username=resolved_username,
            interval_seconds=interval,
            max_scan_hits=max_scan_hits,
            initial_scan_hits=initial_scan_hits,
        )
        terminal.ok("日志地址、只读账号和轮询间隔已保存；密码未保存。")
    password = _initial_log_password(resolved_url, resolved_username)
    run_count = 0
    terminal.section("日志监听")
    terminal.field("Mode", "前台轮询")
    terminal.field("Interval", f"{interval}s")
    while max_runs == 0 or run_count < max_runs:
        run_count += 1
        batch_number = 0
        while True:
            batch_number += 1
            summary_path, summary, password = _poll_with_auth_retry(
                root=root,
                terminal=terminal,
                password_fn=password_fn,
                initial_password=password,
                discover_url=resolved_url,
                username=resolved_username,
                output_path=output_path,
                key_path=key_path,
                scan_state_path=scan_state_path,
                max_scan_hits=max_scan_hits,
                initial_scan_hits=initial_scan_hits,
            )
            result = inbox.ingest_summary(summary_path)
            _commit_log_scan_cursor(
                discover_url=resolved_url,
                scan_state_path=scan_state_path,
                summary_path=summary_path,
                summary=summary,
            )
            selection = summary.get("selection", {})
            terminal.line(
                f"  第 {run_count} 次 / 批次 {batch_number}："
                f"扫描 {selection.get('scanned_hits', 0)}，"
                f"新增 {result['added']}，去重 {result['deduplicated']}"
            )
            if not summary.get("query", {}).get("backlog_remaining", False):
                break
            terminal.warn("本轮达到批次上限；下次轮询将从当前游标继续。")
            break
        if max_runs and run_count >= max_runs:
            break
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            break
    terminal.ok("监听已停止；收件箱状态已保留。")
    return 0


def _run_interactive_session(
    *,
    root: Path,
    config: ControlCenterConfig,
    workflow: ControlCenterWorkflow,
    inbox: LogIncidentInbox,
    terminal: Terminal,
    input_fn: Callable[[str], str],
    password_fn: Callable[[str], str],
    args: argparse.Namespace,
    store: Optional[LocalConfigStore] = None,
) -> int:
    while True:
        try:
            entered = _prompt(input_fn, "输入需求或功能命令: ")
        except EOFError:
            terminal.line()
            terminal.ok("会话已结束。")
            return 0
        try:
            action, value = _interactive_input(entered)
            if action == "empty":
                terminal.warn("请输入需求或命令。")
                continue
            if action == "exit":
                terminal.ok("会话已结束。")
                return 0
            if action == "help":
                _show_interactive_menu(terminal)
                continue
            if action == "logs_setup":
                if store is None:
                    raise ValueError("当前会话不能保存日志配置。")
                max_scan_hits = (
                    args.max_scan_hits
                    if args.max_scan_hits is not None
                    else config.log_source.max_scan_hits
                    if config.log_source is not None
                    else kibana_issue_connector.DEFAULT_MAX_SCAN_HITS
                )
                initial_scan_hits = (
                    args.initial_scan_hits
                    if args.initial_scan_hits is not None
                    else config.log_source.initial_scan_hits
                    if config.log_source is not None
                    else kibana_issue_connector.DEFAULT_INITIAL_SCAN_HITS
                )
                config = _configure_log_access(
                    store=store,
                    config=config,
                    terminal=terminal,
                    input_fn=input_fn,
                    discover_url=args.discover_url,
                    username=args.username,
                    interval_seconds=args.interval_seconds,
                    max_scan_hits=max_scan_hits,
                    initial_scan_hits=initial_scan_hits,
                )
                continue
            if action == "inbox":
                _show_inbox(terminal, inbox)
                continue
            if action == "review":
                _review_incident(
                    incident_id=value,
                    inbox=inbox,
                    workflow=workflow,
                    terminal=terminal,
                    input_fn=input_fn,
                    preview_only=args.preview_only,
                )
                continue
            if action in {"logs", "logs_more"}:
                max_scan_hits = (
                    args.max_scan_hits
                    if args.max_scan_hits is not None
                    else config.log_source.max_scan_hits
                    if config.log_source is not None
                    else kibana_issue_connector.DEFAULT_MAX_SCAN_HITS
                )
                initial_scan_hits = (
                    args.initial_scan_hits
                    if args.initial_scan_hits is not None
                    else config.log_source.initial_scan_hits
                    if config.log_source is not None
                    else kibana_issue_connector.DEFAULT_INITIAL_SCAN_HITS
                )
                discover_url, username = _resolved_log_connection(
                    config,
                    discover_url=args.discover_url,
                    username=args.username,
                )
                evidence = _fetch_log_candidate(
                    root=root,
                    terminal=terminal,
                    input_fn=input_fn,
                    discover_url=discover_url,
                    username=username,
                    output_path=args.log_output,
                    key_path=args.log_key,
                    scan_state_path=args.log_scan_state,
                    history_state_path=getattr(
                        args,
                        "log_history_state",
                        DEFAULT_LOG_HISTORY_STATE_PATH,
                    ),
                    history_scan=action == "logs_more",
                    max_scan_hits=max_scan_hits,
                    initial_scan_hits=initial_scan_hits,
                    inbox=inbox,
                    password_fn=password_fn,
                )
                initial = workflow.create_from_evidence(evidence)
            else:
                initial = workflow.create(value)
            terminal.section("生成计划")
            _run_record(
                workflow,
                initial,
                terminal,
                input_fn,
                preview_only=args.preview_only,
            )
        except EOFError:
            terminal.line()
            terminal.ok("会话已结束。")
            return 0
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            terminal.fail(str(exc))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the guarded Issue-to-code workflow entirely in the terminal."
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("watch", "inbox", "review"),
        help="Log automation command.",
    )
    parser.add_argument(
        "incident_id",
        nargs="?",
        help="Incident ID required by the review command.",
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
    parser.add_argument("--inbox-path", type=Path, default=DEFAULT_LOG_INBOX_PATH)
    parser.add_argument(
        "--log-scan-state",
        type=Path,
        default=DEFAULT_LOG_SCAN_STATE_PATH,
    )
    parser.add_argument(
        "--log-history-state",
        type=Path,
        default=DEFAULT_LOG_HISTORY_STATE_PATH,
    )
    parser.add_argument(
        "--max-scan-hits",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--initial-scan-hits",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=None,
        help="Foreground watch polling interval.",
    )
    parser.add_argument(
        "--max-runs",
        type=int,
        default=0,
        help="Stop watch after this many polls; zero keeps running.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one log poll and stop.",
    )
    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    input_fn: Callable[[str], str] = input,
    password_fn: Callable[[str], str] = getpass.getpass,
    stream: TextIO = sys.stdout,
) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "review" and args.incident_id:
        raise SystemExit("incident_id is accepted only by the review command")
    if args.command == "review" and not args.incident_id:
        raise SystemExit("review requires INCIDENT_ID")
    if args.command and any((args.request, args.logs, args.resume)):
        raise SystemExit("log commands cannot be combined with --request, --logs, or --resume")
    if args.max_runs < 0:
        raise SystemExit("--max-runs cannot be negative")
    if args.max_scan_hits is not None and not (
        1 <= args.max_scan_hits <= kibana_issue_connector.MAX_SCAN_HITS
    ):
        raise SystemExit(
            f"--max-scan-hits must be between 1 and "
            f"{kibana_issue_connector.MAX_SCAN_HITS}"
        )
    if args.initial_scan_hits is not None and not (
        1 <= args.initial_scan_hits <= kibana_issue_connector.MAX_INITIAL_SCAN_HITS
    ):
        raise SystemExit(
            f"--initial-scan-hits must be between 1 and "
            f"{kibana_issue_connector.MAX_INITIAL_SCAN_HITS}"
        )
    root = Path.cwd().resolve()
    config_path = args.config if args.config.is_absolute() else root / args.config
    runs_path = args.runs if args.runs.is_absolute() else root / args.runs
    terminal = Terminal(stream, color=False if args.no_color else None)
    terminal.banner()
    store = LocalConfigStore(config_path)
    inbox_path = (
        args.inbox_path
        if args.inbox_path.is_absolute()
        else root / args.inbox_path
    )
    inbox = LogIncidentInbox(inbox_path)
    try:
        if args.command == "inbox":
            return _show_inbox(terminal, inbox)
        config = store.load()
        if args.configure or config is None:
            config = _configure_one_repository(store, root, terminal, input_fn)
        if args.command == "watch":
            max_scan_hits = (
                args.max_scan_hits
                if args.max_scan_hits is not None
                else config.log_source.max_scan_hits
                if config.log_source is not None
                else kibana_issue_connector.DEFAULT_MAX_SCAN_HITS
            )
            initial_scan_hits = (
                args.initial_scan_hits
                if args.initial_scan_hits is not None
                else config.log_source.initial_scan_hits
                if config.log_source is not None
                else kibana_issue_connector.DEFAULT_INITIAL_SCAN_HITS
            )
            output_path = (
                args.log_output
                if args.log_output.is_absolute()
                else root / args.log_output
            )
            key_path = (
                args.log_key if args.log_key.is_absolute() else root / args.log_key
            )
            return _watch_logs(
                root=root,
                store=store,
                config=config,
                inbox=inbox,
                terminal=terminal,
                input_fn=input_fn,
                password_fn=password_fn,
                discover_url=args.discover_url,
                username=args.username,
                output_path=output_path,
                key_path=key_path,
                scan_state_path=args.log_scan_state,
                max_scan_hits=max_scan_hits,
                initial_scan_hits=initial_scan_hits,
                interval_seconds=args.interval_seconds,
                max_runs=1 if args.once else args.max_runs,
            )
        identity = inspect_identity(root)
        if identity.get("github", {}).get("login") != config.github_login:
            raise ValueError("当前 GitHub 账号与本地配置不一致。")
        if not identity.get("copilot", {}).get("available", False):
            raise ValueError("未检测到可用的 GitHub Copilot CLI。")
        _show_config(terminal, config, identity)
        workflow = ControlCenterWorkflow(store, runs_path)
        if args.command == "review":
            return _review_incident(
                incident_id=args.incident_id,
                inbox=inbox,
                workflow=workflow,
                terminal=terminal,
                input_fn=input_fn,
                preview_only=args.preview_only,
            )
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
            return _run_interactive_session(
                root=root,
                store=store,
                config=config,
                workflow=workflow,
                inbox=inbox,
                terminal=terminal,
                input_fn=input_fn,
                password_fn=password_fn,
                args=args,
            )
        if use_logs:
            max_scan_hits = (
                args.max_scan_hits
                if args.max_scan_hits is not None
                else config.log_source.max_scan_hits
                if config.log_source is not None
                else kibana_issue_connector.DEFAULT_MAX_SCAN_HITS
            )
            initial_scan_hits = (
                args.initial_scan_hits
                if args.initial_scan_hits is not None
                else config.log_source.initial_scan_hits
                if config.log_source is not None
                else kibana_issue_connector.DEFAULT_INITIAL_SCAN_HITS
            )
            discover_url, username = _resolved_log_connection(
                config,
                discover_url=args.discover_url,
                username=args.username,
            )
            evidence = _fetch_log_candidate(
                root=root,
                terminal=terminal,
                input_fn=input_fn,
                discover_url=discover_url,
                username=username,
                output_path=args.log_output,
                key_path=args.log_key,
                scan_state_path=args.log_scan_state,
                history_state_path=args.log_history_state,
                max_scan_hits=max_scan_hits,
                initial_scan_hits=initial_scan_hits,
                inbox=inbox,
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
