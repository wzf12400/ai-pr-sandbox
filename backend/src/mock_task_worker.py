"""Local worker for the Java control-plane mock execution path.

The queue contains task identifiers only. Task state and the sanitized work
contract are fetched from the control plane after an atomic claim. External
Issue, model, and Cloud Agent calls remain behind explicit disabled-by-default
policy gates.

Default mode runs continuously: each iteration is still a bounded single-task
execution with a bounded Redis wait, so individual tasks keep their safety
bounds while the process stays alive to consume new work. Use --once for a
single bounded pass (used by tests and scripts).
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


LOGGER = logging.getLogger("mock-task-worker")
DEFAULT_CONTROL_PLANE_URL = "http://127.0.0.1:8080"
DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"
DEFAULT_QUEUE_KEY = "github-ai-agent:jobs:v2"
DEFAULT_CONSUMER_GROUP = "github-ai-agent-workers"
DEFAULT_AUTHORIZED_REPOSITORY = "KikaTech/backend-aicompanion"
DEFAULT_REPOSITORY_PATH = ".worker-repos/KikaTech__backend-aicompanion"
DEFAULT_WORKER_REPOS_ROOT = ".worker-repos"
DEFAULT_WORKER_WORKTREES_ROOT = ".worker-worktrees"
DEFAULT_WORKER_AUDIT_ROOT = ".worker-audit"
# 这些组织的仓库用 GITHUB_ROUTING_TOKEN（公司账号 PAT）克隆和读写，
# 其余仓库用 GITHUB_ISSUE_TOKEN（本机 gh 登录账号）。可用
# WORKER_ROUTING_TOKEN_ORGS="OrgA,OrgB" 覆盖。
DEFAULT_ROUTING_TOKEN_ORGS = "KikaTech"
MIN_JIRA_CODE_ROUTING_CONFIDENCE = 85
JIRA_TARGET_PATTERN = re.compile(
    r"(?i)(?:页面|接口|播放器?|音色|排行榜|文案|角标|角色|字体|主题|按钮|弹窗|"
    r"聊天|服务|模块|订阅|支付|ugc|theme|font|api|endpoint|class|method)"
)
JIRA_BEHAVIOR_PATTERN = re.compile(
    r"(?i)(?:无法|不能|失败|报错|错误|异常|裁切|截断|不生效|仍|还是|循环|"
    r"未|需要|需|应|返回|切换|对齐|慢|fail|error|exception|incorrect|wrong)"
)
JIRA_VAGUE_MARKERS = ("历史问题记录",)


def _jira_code_context_sufficient(claim: dict[str, Any]) -> bool:
    confidence = claim.get("routingConfidence")
    summary = str(claim.get("inputSummary", "")).strip()
    return (
        isinstance(confidence, int)
        and not isinstance(confidence, bool)
        and confidence >= MIN_JIRA_CODE_ROUTING_CONFIDENCE
        and len(summary) >= 12
        and not any(marker in summary for marker in JIRA_VAGUE_MARKERS)
        and JIRA_TARGET_PATTERN.search(summary) is not None
        and JIRA_BEHAVIOR_PATTERN.search(summary) is not None
    )


@dataclass(frozen=True)
class RepoSpec:
    """worker 被授权操作的一个仓库：本地克隆位置、默认分支、用哪个 token。"""

    repository: str
    path: Path
    default_branch: str
    token_env: str
    code_policy_path: Path


def _token_env_for(repository: str, routing_orgs: frozenset[str]) -> str:
    owner = repository.split("/", 1)[0]
    return "GITHUB_ROUTING_TOKEN" if owner in routing_orgs else "GITHUB_ISSUE_TOKEN"


def _build_repo_catalog(
    authorized_repository: str,
    repository_path: Path,
    repositories_root: Path,
    default_code_policy_path: Path,
    scope_path: Path,
    routing_orgs: frozenset[str],
) -> dict[str, RepoSpec]:
    """授权仓库目录 = 默认仓 + 搜索范围文件里 enabled 的仓（单一事实来源）。"""
    catalog: dict[str, RepoSpec] = {
        authorized_repository: RepoSpec(
            repository=authorized_repository,
            path=repository_path,
            default_branch="main",
            token_env=_token_env_for(authorized_repository, routing_orgs),
            code_policy_path=default_code_policy_path,
        )
    }
    try:
        from src.repository_resolver import load_search_scope

        scope = load_search_scope(scope_path)
    except Exception:  # noqa: BLE001 - 目录构建失败不阻断 worker 启动
        return catalog
    for entry in scope.enabled_repositories:
        if entry.repository in catalog:
            # 默认仓的分支以范围文件为准（未来换默认分支不用改代码）
            existing = catalog[entry.repository]
            catalog[entry.repository] = RepoSpec(
                repository=existing.repository,
                path=existing.path,
                default_branch=entry.default_branch,
                token_env=existing.token_env,
                code_policy_path=existing.code_policy_path,
            )
            continue
        safe_name = entry.repository.replace("/", "__")
        policy_path = (
            Path("control-plane/config/code-policies") / f"{safe_name}.json"
        ).resolve()
        catalog[entry.repository] = RepoSpec(
            repository=entry.repository,
            path=(repositories_root / safe_name).resolve(),
            default_branch=entry.default_branch,
            token_env=_token_env_for(entry.repository, routing_orgs),
            code_policy_path=policy_path,
        )
    return catalog


class WorkerError(RuntimeError):
    """A safe worker failure without remote response contents."""


class StaleTaskError(WorkerError):
    """The queue item was already claimed or otherwise no longer pending."""


class PublishedIssueWorkerError(WorkerError):
    """A downstream step failed after one Issue reference became canonical."""

    def __init__(self, issue_number: int, issue_url: str, detail: str = "") -> None:
        message = "post-Issue execution failed safely"
        if detail:
            message = f"{message}: {detail[:300]}"
        super().__init__(message)
        self.issue_number = issue_number
        self.issue_url = issue_url


class _RepositoryFileLock:
    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path
        self._handle: Any = None

    def __enter__(self) -> "_RepositoryFileLock":
        lock_dir = self._lock_path.parent
        if lock_dir.is_symlink():
            raise WorkerError("worker repository lock directory must not be a symlink")
        lock_dir.mkdir(parents=True, exist_ok=True)
        self._handle = self._lock_path.open("a+", encoding="utf-8")
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *_args: Any) -> None:
        if self._handle is None:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None


def _repository_lock_path(repository_path: Path, lock_root: Path) -> Path:
    digest = hashlib.sha256(
        str(repository_path.resolve()).encode("utf-8")
    ).hexdigest()[:24]
    return lock_root / f"{digest}.lock"


def _ensure_repository_checkout(spec: RepoSpec, timeout_seconds: float = 300.0) -> None:
    """Clone one authorized base checkout under a cross-process lock."""
    lock_root = spec.path.parent / ".worker-locks"
    with _RepositoryFileLock(_repository_lock_path(spec.path, lock_root)):
        if spec.path.is_dir():
            return
        token = os.environ.get(spec.token_env, "").strip()
        if not token:
            raise WorkerError(f"{spec.token_env} 未设置，无法克隆 {spec.repository}")
        if spec.path.is_symlink() or spec.path.exists():
            raise WorkerError("repository checkout path must be an unused regular path")
        spec.path.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://x-access-token:{token}@github.com/{spec.repository}.git"
        try:
            result = subprocess.run(
                [
                    "git",
                    "clone",
                    "--branch",
                    spec.default_branch,
                    "--",
                    url,
                    str(spec.path),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exception:
            raise WorkerError(
                f"authorized repository clone failed for {spec.repository}"
            ) from exception
        if result.returncode != 0:
            raise WorkerError(
                f"authorized repository clone failed for {spec.repository}"
            )


class TaskRepositoryWorktree:
    """One fenced linked worktree owned by one canonical task ID."""

    def __init__(
        self,
        repository: str,
        base_repository_path: Path,
        worktrees_root: Path,
        default_branch: str,
        task_id: str,
    ) -> None:
        if not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository
        ):
            raise WorkerError("task worktree repository is invalid")
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", default_branch)
            or ".." in default_branch
            or "//" in default_branch
            or default_branch.endswith(("/", "."))
        ):
            raise WorkerError("task worktree default branch is invalid")
        if base_repository_path.is_symlink():
            raise WorkerError("task worktree base repository must not be a symlink")
        if worktrees_root.is_symlink():
            raise WorkerError("task worktree root must not be a symlink")
        self.repository = repository
        self.base_repository_path = base_repository_path.resolve()
        self.worktrees_root = worktrees_root.resolve()
        self.default_branch = default_branch
        self.task_id = validate_task_id(task_id)
        safe_repository = repository.replace("/", "__")
        self.path = self.worktrees_root / safe_repository / self.task_id
        self.start_branch = f"worker/task/{self.task_id}"
        self.base_ref = f"refs/worker/task/{self.task_id}/base"
        self.cleanup_error: str | None = None
        self._active = False
        self._lock_path = _repository_lock_path(
            self.base_repository_path,
            self.worktrees_root / ".locks",
        )

    @staticmethod
    def _git(
        repository_path: Path,
        *args: str,
        timeout: float = 120.0,
        check: bool = True,
    ) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(repository_path), *args],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exception:
            raise WorkerError("task worktree Git operation failed safely") from exception
        if check and result.returncode != 0:
            raise WorkerError("task worktree Git operation failed safely")
        return result.stdout.strip()

    def _validate_paths(self) -> None:
        if (
            not self.base_repository_path.is_dir()
            or self.base_repository_path.is_symlink()
        ):
            raise WorkerError("task worktree base repository is missing or unsafe")
        if self.worktrees_root.is_symlink():
            raise WorkerError("task worktree root must not be a symlink")
        try:
            self.path.relative_to(self.worktrees_root)
        except ValueError as exception:
            raise WorkerError("task worktree path escaped its configured root") from exception
        try:
            self.worktrees_root.relative_to(self.base_repository_path)
        except ValueError:
            pass
        else:
            raise WorkerError("task worktree root must be outside the base repository")

    def _remove_registered_worktree(self) -> None:
        if self.path.is_symlink():
            raise WorkerError("task worktree path unexpectedly became a symlink")
        self._git(
            self.base_repository_path,
            "worktree",
            "remove",
            "--force",
            str(self.path),
            check=False,
        )
        if not self.path.exists():
            return
        marker = self.path / ".git"
        if not marker.is_file() or marker.is_symlink():
            raise WorkerError("refusing to remove an unowned task worktree directory")
        try:
            marker_text = marker.read_text(encoding="utf-8").strip()
        except OSError as exception:
            raise WorkerError("task worktree ownership could not be verified") from exception
        if not marker_text.startswith("gitdir: "):
            raise WorkerError("task worktree ownership marker is invalid")
        git_dir = Path(marker_text.removeprefix("gitdir: ").strip()).resolve()
        common_dir = Path(
            self._git(self.base_repository_path, "rev-parse", "--git-common-dir")
        )
        if not common_dir.is_absolute():
            common_dir = (self.base_repository_path / common_dir).resolve()
        try:
            git_dir.relative_to(common_dir / "worktrees")
        except ValueError as exception:
            raise WorkerError("task worktree is not owned by the base repository") from exception
        shutil.rmtree(self.path)

    def __enter__(self) -> Path:
        self._validate_paths()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _RepositoryFileLock(self._lock_path):
            if self.path.exists() or self.path.is_symlink():
                self._remove_registered_worktree()
            self._git(self.base_repository_path, "worktree", "prune")
            self._git(
                self.base_repository_path,
                "branch",
                "-D",
                self.start_branch,
                check=False,
            )
            self._git(
                self.base_repository_path,
                "update-ref",
                "-d",
                self.base_ref,
                check=False,
            )
            self._git(
                self.base_repository_path,
                "fetch",
                "origin",
                (
                    f"+refs/heads/{self.default_branch}:"
                    f"{self.base_ref}"
                ),
            )
            self._git(
                self.base_repository_path,
                "worktree",
                "add",
                "-b",
                self.start_branch,
                str(self.path),
                self.base_ref,
            )
        self._active = True
        return self.path

    def __exit__(self, exception_type: Any, *_args: Any) -> None:
        if not self._active:
            return
        try:
            with _RepositoryFileLock(self._lock_path):
                self._remove_registered_worktree()
                self._git(
                    self.base_repository_path,
                    "branch",
                    "-D",
                    self.start_branch,
                    check=False,
                )
                self._git(
                    self.base_repository_path,
                    "update-ref",
                    "-d",
                    self.base_ref,
                    check=False,
                )
                self._git(self.base_repository_path, "worktree", "prune")
        except WorkerError as cleanup_error:
            self.cleanup_error = str(cleanup_error)
            LOGGER.exception(
                "task_id=%s task worktree cleanup failed",
                self.task_id,
            )
        finally:
            self._active = False


@dataclass(frozen=True)
class QueueMessage:
    message_id: str
    task_id: str
    attempt: int


class TaskQueue(Protocol):
    def next_message(self, timeout_seconds: int) -> QueueMessage | None:
        """Return one stream message, or None when the bounded wait expires."""

    def acknowledge(self, message: QueueMessage) -> None:
        """Acknowledge a message only after its MySQL terminal state is durable."""

    def retry_or_dead_letter(self, message: QueueMessage, reason: str) -> str:
        """Republish a bounded retry or atomically move the message to dead letter."""


class TaskClient(Protocol):
    def claim(self, task_id: str) -> dict[str, Any]:
        """Atomically claim and return the sanitized task contract."""

    def transition(self, task_id: str, target_status: str, detail: str) -> None:
        """Request a validated state transition from the control plane."""

    def record_progress(self, task_id: str, stage: str, detail: str) -> None:
        """Append a bounded, user-visible execution milestone."""

    def attach_issue(self, task_id: str, issue_number: int, issue_url: str) -> None:
        """Persist one repository-bound GitHub Issue reference."""

    def attach_agent_task(
        self, task_id: str, agent_task_id: str, agent_task_url: str
    ) -> None:
        """Persist one GitHub Copilot cloud-agent task reference."""

    def reserve_agent_task(
        self,
        task_id: str,
        submission_key: str,
        issue_sha256: str,
        policy_sha256: str,
    ) -> None:
        """Persist the exact submission contract before the remote POST."""

    def release_agent_task_reservation(self, task_id: str) -> None:
        """Release a reservation after GitHub confirms no remote task was created."""

    def heartbeat(self, task_id: str) -> None:
        """Refresh the durable PROCESSING lease during remote polling."""

    def attach_pull_request(
        self,
        task_id: str,
        pr_number: int,
        pr_url: str,
        test_summary: str,
    ) -> None:
        """Persist one tested Draft PR reference."""

    def create_dependency_tasks(
        self,
        task_id: str,
        dependencies: tuple["RepositoryDependency", ...],
    ) -> None:
        """Atomically create independently authorized child tasks."""


class ExecutionEngine(Protocol):
    def execute(self, claim: dict[str, Any]) -> "ExecutionResult":
        """Run one bounded local execution against a sanitized claim."""


@dataclass(frozen=True)
class RepositoryDependency:
    repository: str
    reason_code: str
    summary: str


@dataclass(frozen=True)
class ExecutionResult:
    target_status: str
    detail: str
    candidate_count: int = 0
    issue_number: int | None = None
    issue_url: str = ""
    pr_number: int | None = None
    pr_url: str = ""
    test_summary: str = ""
    dependencies: tuple[RepositoryDependency, ...] = ()


def require_loopback_url(value: str, allowed_schemes: set[str], label: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in allowed_schemes:
        raise WorkerError(f"{label} must use one of: {', '.join(sorted(allowed_schemes))}")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise WorkerError(f"{label} must target the local machine")
    return value.rstrip("/")


def require_redis_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"redis", "rediss"}:
        raise WorkerError("REDIS_URL must use redis or rediss")
    if not parsed.hostname or parsed.fragment:
        raise WorkerError("REDIS_URL is invalid")
    return value.rstrip("/")


def validate_task_id(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exception:
        raise WorkerError("queue item is not a valid task identifier") from exception
    if str(parsed) != value.lower():
        raise WorkerError("queue item is not a canonical task identifier")
    return str(parsed)


@dataclass(frozen=True)
class WorkerConfig:
    control_plane_url: str
    redis_url: str
    queue_key: str
    consumer_group: str
    consumer_name: str
    dead_letter_key: str
    metrics_key: str
    stale_idle_ms: int
    max_retries: int
    dead_letter_max_length: int
    wait_timeout_seconds: int
    request_timeout_seconds: float
    authorized_repository: str
    repository_path: Path
    issue_publication_enabled: bool
    issue_scope_path: Path
    issue_policy_path: Path
    issue_policy_sha256: str
    code_mode: str
    code_policy_path: Path
    code_model: str
    cloud_agent_token: str
    cloud_agent_poll_interval_seconds: float
    cloud_agent_max_wait_seconds: float
    github_timeout_seconds: float
    code_audit_dir: Path
    code_auto_approval_enabled: bool
    code_auto_approval_policy_path: Path
    code_auto_approval_policy_sha256: str
    repo_catalog: dict[str, RepoSpec]

    @classmethod
    def from_environment(cls, wait_timeout_seconds: int) -> "WorkerConfig":
        control_plane_url = require_loopback_url(
            os.getenv("CONTROL_PLANE_URL", DEFAULT_CONTROL_PLANE_URL),
            {"http"},
            "CONTROL_PLANE_URL",
        )
        redis_url = require_redis_url(
            os.getenv("REDIS_URL", DEFAULT_REDIS_URL)
        )
        queue_key = os.getenv("WORKER_QUEUE_KEY", DEFAULT_QUEUE_KEY).strip()
        if not queue_key or len(queue_key) > 200:
            raise WorkerError("WORKER_QUEUE_KEY must contain 1 to 200 characters")
        consumer_group = os.getenv(
            "WORKER_CONSUMER_GROUP", DEFAULT_CONSUMER_GROUP
        ).strip()
        if not consumer_group or len(consumer_group) > 128:
            raise WorkerError("WORKER_CONSUMER_GROUP must contain 1 to 128 characters")
        consumer_name = os.getenv(
            "WORKER_CONSUMER_NAME", f"mock-worker-{os.getpid()}"
        ).strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", consumer_name):
            raise WorkerError("WORKER_CONSUMER_NAME contains unsupported characters")
        dead_letter_key = os.getenv(
            "WORKER_DEAD_LETTER_KEY", f"{queue_key}:dead-letter"
        ).strip()
        metrics_key = os.getenv(
            "WORKER_QUEUE_METRICS_KEY", f"{queue_key}:metrics"
        ).strip()
        if not dead_letter_key or len(dead_letter_key) > 200:
            raise WorkerError("WORKER_DEAD_LETTER_KEY must contain 1 to 200 characters")
        if not metrics_key or len(metrics_key) > 200:
            raise WorkerError("WORKER_QUEUE_METRICS_KEY must contain 1 to 200 characters")
        try:
            stale_idle_ms = int(os.getenv("WORKER_STALE_IDLE_MS", "1800000"))
            max_retries = int(os.getenv("WORKER_MAX_RETRIES", "3"))
            dead_letter_max_length = int(
                os.getenv("WORKER_DEAD_LETTER_MAX_LENGTH", "10000")
            )
        except ValueError as exception:
            raise WorkerError("Redis reliability settings must be integers") from exception
        if stale_idle_ms < 1000 or stale_idle_ms > 86_400_000:
            raise WorkerError("WORKER_STALE_IDLE_MS must be between 1000 and 86400000")
        if max_retries < 0 or max_retries > 20:
            raise WorkerError("WORKER_MAX_RETRIES must be between 0 and 20")
        if dead_letter_max_length < 100 or dead_letter_max_length > 10_000_000:
            raise WorkerError(
                "WORKER_DEAD_LETTER_MAX_LENGTH must be between 100 and 10000000"
            )
        request_timeout = float(os.getenv("WORKER_REQUEST_TIMEOUT_SECONDS", "5"))
        if not math.isfinite(request_timeout) or request_timeout <= 0 or request_timeout > 30:
            raise WorkerError("WORKER_REQUEST_TIMEOUT_SECONDS must be between 0 and 30")
        authorized_repository = os.getenv(
            "WORKER_AUTHORIZED_REPOSITORY",
            DEFAULT_AUTHORIZED_REPOSITORY,
        ).strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", authorized_repository):
            raise WorkerError("WORKER_AUTHORIZED_REPOSITORY must be owner/repository")
        repository_path = Path(
            os.getenv("WORKER_REPOSITORY_PATH", DEFAULT_REPOSITORY_PATH)
        ).resolve()
        repositories_root = Path(
            os.getenv("WORKER_REPOS_ROOT", DEFAULT_WORKER_REPOS_ROOT)
        ).resolve()
        if repositories_root.is_symlink():
            raise WorkerError("WORKER_REPOS_ROOT must not be a symbolic link")
        issue_publication_value = os.getenv(
            "WORKER_ISSUE_PUBLICATION_ENABLED", "false"
        ).strip().lower()
        if issue_publication_value not in {"true", "false"}:
            raise WorkerError("WORKER_ISSUE_PUBLICATION_ENABLED must be true or false")
        issue_publication_enabled = issue_publication_value == "true"
        issue_scope_path = Path(
            os.getenv(
                "WORKER_ISSUE_SCOPE_PATH",
                "control-plane/config/repository-search-scope.json",
            )
        ).resolve()
        issue_policy_path = Path(
            os.getenv(
                "WORKER_ISSUE_POLICY_PATH",
                "control-plane/config/repository-auto-publish-policy.json",
            )
        ).resolve()
        issue_policy_sha256 = os.getenv("WORKER_ISSUE_POLICY_SHA256", "").strip()
        if issue_publication_enabled and not re.fullmatch(
            r"[0-9a-f]{64}", issue_policy_sha256
        ):
            raise WorkerError(
                "WORKER_ISSUE_POLICY_SHA256 is required when Issue publication is enabled"
            )
        code_mode = os.getenv("WORKER_CODE_MODE", "disabled").strip().lower()
        if code_mode not in {"disabled", "cloud_agent"}:
            raise WorkerError(
                "WORKER_CODE_MODE must be disabled or cloud_agent"
            )
        if code_mode != "disabled" and not issue_publication_enabled:
            raise WorkerError(
                "WORKER_CODE_MODE requires WORKER_ISSUE_PUBLICATION_ENABLED=true"
            )
        code_policy_path = Path(
            os.getenv(
                "WORKER_CODE_POLICY_PATH",
                "control-plane/config/code-policies/KikaTech__backend-aicompanion.json",
            )
        ).resolve()
        code_model = os.getenv("WORKER_CODE_MODEL", "").strip()
        cloud_agent_token = os.getenv("GITHUB_COPILOT_TOKEN", "").strip()
        if code_mode == "cloud_agent" and (
            not cloud_agent_token
            or any(character in cloud_agent_token for character in "\r\n")
        ):
            raise WorkerError(
                "GITHUB_COPILOT_TOKEN is required for cloud-agent execution"
            )
        try:
            cloud_agent_poll_interval_seconds = float(
                os.getenv("WORKER_CLOUD_AGENT_POLL_INTERVAL_SECONDS", "10")
            )
            cloud_agent_max_wait_seconds = float(
                os.getenv("WORKER_CLOUD_AGENT_MAX_WAIT_SECONDS", "3600")
            )
        except ValueError as exception:
            raise WorkerError(
                "cloud-agent polling settings must be numbers"
            ) from exception
        if (
            not math.isfinite(cloud_agent_poll_interval_seconds)
            or cloud_agent_poll_interval_seconds < 0.1
            or cloud_agent_poll_interval_seconds > 300
        ):
            raise WorkerError(
                "WORKER_CLOUD_AGENT_POLL_INTERVAL_SECONDS must be between 0.1 and 300"
            )
        if (
            not math.isfinite(cloud_agent_max_wait_seconds)
            or cloud_agent_max_wait_seconds < cloud_agent_poll_interval_seconds
            or cloud_agent_max_wait_seconds > 7200
        ):
            raise WorkerError(
                "WORKER_CLOUD_AGENT_MAX_WAIT_SECONDS must be between one poll and 7200"
            )
        github_timeout_seconds = float(
            os.getenv("WORKER_GITHUB_TIMEOUT_SECONDS", "30")
        )
        if (
            not math.isfinite(github_timeout_seconds)
            or github_timeout_seconds < 1
            or github_timeout_seconds > 120
        ):
            raise WorkerError("WORKER_GITHUB_TIMEOUT_SECONDS must be between 1 and 120")
        code_audit_dir = Path(
            os.getenv(
                "WORKER_CODE_AUDIT_DIR",
                str(Path(DEFAULT_WORKER_AUDIT_ROOT) / "control-plane"),
            )
        ).resolve()
        if code_audit_dir.is_symlink():
            raise WorkerError("WORKER_CODE_AUDIT_DIR must not be a symbolic link")
        try:
            code_audit_dir.relative_to(repository_path)
        except ValueError:
            pass
        else:
            raise WorkerError(
                "WORKER_CODE_AUDIT_DIR must be outside the worker repository"
            )
        code_auto_approval_value = os.getenv(
            "WORKER_CODE_AUTO_APPROVAL_ENABLED", "false"
        ).strip().lower()
        if code_auto_approval_value not in {"true", "false"}:
            raise WorkerError(
                "WORKER_CODE_AUTO_APPROVAL_ENABLED must be true or false"
            )
        code_auto_approval_enabled = code_auto_approval_value == "true"
        if code_mode == "cloud_agent" and not code_auto_approval_enabled:
            raise WorkerError(
                "WORKER_CODE_MODE=cloud_agent requires automatic code preapproval"
            )
        if code_auto_approval_enabled and (
            not issue_publication_enabled or code_mode == "disabled"
        ):
            raise WorkerError(
                "automatic code approval requires Issue publication and a non-disabled code mode"
            )
        code_auto_approval_policy_path = Path(
            os.getenv(
                "WORKER_CODE_AUTO_APPROVAL_POLICY_PATH",
                "control-plane/config/code-preapproval-manifest.json",
            )
        ).resolve()
        code_auto_approval_policy_sha256 = os.getenv(
            "WORKER_CODE_AUTO_APPROVAL_POLICY_SHA256", ""
        ).strip()
        if code_auto_approval_enabled and not re.fullmatch(
            r"[0-9a-f]{64}", code_auto_approval_policy_sha256
        ):
            raise WorkerError(
                "WORKER_CODE_AUTO_APPROVAL_POLICY_SHA256 is required when automatic code approval is enabled"
            )
        routing_orgs = frozenset(
            org.strip()
            for org in os.getenv(
                "WORKER_ROUTING_TOKEN_ORGS", DEFAULT_ROUTING_TOKEN_ORGS
            ).split(",")
            if org.strip()
        )
        repo_catalog = _build_repo_catalog(
            authorized_repository,
            repository_path,
            repositories_root,
            code_policy_path,
            issue_scope_path,
            routing_orgs,
        )
        return cls(
            control_plane_url=control_plane_url,
            redis_url=redis_url,
            queue_key=queue_key,
            consumer_group=consumer_group,
            consumer_name=consumer_name,
            dead_letter_key=dead_letter_key,
            metrics_key=metrics_key,
            stale_idle_ms=stale_idle_ms,
            max_retries=max_retries,
            dead_letter_max_length=dead_letter_max_length,
            wait_timeout_seconds=wait_timeout_seconds,
            request_timeout_seconds=request_timeout,
            authorized_repository=authorized_repository,
            repository_path=repository_path,
            issue_publication_enabled=issue_publication_enabled,
            issue_scope_path=issue_scope_path,
            issue_policy_path=issue_policy_path,
            issue_policy_sha256=issue_policy_sha256,
            code_mode=code_mode,
            code_policy_path=code_policy_path,
            code_model=code_model,
            cloud_agent_token=cloud_agent_token,
            cloud_agent_poll_interval_seconds=cloud_agent_poll_interval_seconds,
            cloud_agent_max_wait_seconds=cloud_agent_max_wait_seconds,
            github_timeout_seconds=github_timeout_seconds,
            code_audit_dir=code_audit_dir,
            code_auto_approval_enabled=code_auto_approval_enabled,
            code_auto_approval_policy_path=code_auto_approval_policy_path,
            code_auto_approval_policy_sha256=code_auto_approval_policy_sha256,
            repo_catalog=repo_catalog,
        )


class SyntheticExecutionEngine:
    """Compatibility engine used only by direct unit tests."""

    def execute(self, claim: dict[str, Any]) -> ExecutionResult:
        return ExecutionResult(
            target_status="COMPLETED",
            detail="mock execution completed; no external systems were called",
        )


class LocalRepositoryExecutionEngine:
    """Reuse the existing repository locator against one verified checkout."""

    _LOCATOR_NOISE_PATTERN = re.compile(
        r"<!--.*?-->|\b[0-9a-f]{32,64}\b",
        re.DOTALL,
    )

    def __init__(
        self,
        authorized_repository: str,
        repository_path: Path,
        default_branch: str = "main",
    ) -> None:
        self._authorized_repository = authorized_repository
        self._repository_path = repository_path
        self._default_branch = default_branch

    @staticmethod
    def _git(repository_path: Path, *args: str) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(repository_path), *args],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exception:
            raise WorkerError("authorized repository checkout could not be verified") from exception
        return result.stdout.strip()

    @staticmethod
    def _origin_slug(url: str) -> str:
        """从 origin URL 提取 owner/repo；兼容 URL 里嵌 token 的写法。"""
        match = re.search(
            r"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$",
            url.strip(),
            re.IGNORECASE,
        )
        return match.group(1).lower() if match else ""

    def _verify_checkout(self, claimed_repository: str) -> str:
        if claimed_repository != self._authorized_repository:
            raise WorkerError("claimed repository is not authorized for this worker")
        if not self._repository_path.is_dir() or self._repository_path.is_symlink():
            raise WorkerError("authorized repository checkout is missing or unsafe")

        expected_slug = self._authorized_repository.lower()
        actual_origin = self._git(self._repository_path, "remote", "get-url", "origin")
        if self._origin_slug(actual_origin) != expected_slug:
            raise WorkerError("authorized repository checkout has an unexpected origin")
        current_branch = self._git(self._repository_path, "branch", "--show-current")
        if current_branch != self._default_branch:
            raise WorkerError("authorized repository checkout must remain on its default branch")
        if self._git(self._repository_path, "status", "--porcelain"):
            raise WorkerError("authorized repository checkout must remain clean")
        return self._git(self._repository_path, "rev-parse", "HEAD")

    def execute(self, claim: dict[str, Any]) -> ExecutionResult:
        from src.repo_locator import locate_issue

        repository = str(claim.get("matchedRepository", ""))
        approved_issue = claim.get("approvedIssue")
        if approved_issue is not None:
            if not isinstance(approved_issue, dict):
                raise WorkerError("approved Issue snapshot is invalid")
            title = approved_issue.get("title")
            body = approved_issue.get("body")
            if not isinstance(title, str) or not isinstance(body, str):
                raise WorkerError("approved Issue snapshot is incomplete")
            requirement = self._LOCATOR_NOISE_PATTERN.sub(" ", f"{title}\n{body}").strip()
        else:
            requirement = str(claim.get("normalizedRequirement", "")).strip()
        commit = self._verify_checkout(repository)
        try:
            location = locate_issue(
                self._repository_path,
                requirement,
                requirement,
                top_k=5,
            )
        except ValueError as exception:
            raise WorkerError("existing repository locator rejected the task safely") from exception

        candidates = location.get("candidates")
        if not isinstance(candidates, list):
            raise WorkerError("existing repository locator returned an invalid result")
        paths = [
            item.get("path")
            for item in candidates
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        ]
        if not paths:
            return ExecutionResult(
                target_status="NEEDS_CONTEXT",
                detail=(
                    "旧代码定位逻辑未找到候选文件；请补充函数名或文件路径；"
                    "未修改仓库，也未调用模型、Copilot 或 GitHub 写接口"
                ),
            )
        safe_paths = ", ".join(paths[:5])
        return ExecutionResult(
            target_status="COMPLETED",
            candidate_count=len(paths),
            detail=(
                f"只读代码定位完成；提交 {commit[:12]}；候选文件：{safe_paths}；"
                "未修改仓库，也未调用模型、Copilot 或 GitHub 写接口"
            )[:1000],
        )


class _DisabledRepositorySearchAdapter:
    def search(self, repository: str, term: str, max_hits: int) -> Any:
        raise ValueError("preselected repository unexpectedly required remote code search")


class _DisabledApprovedIssueCandidateClient:
    def list_open_issues(
        self,
        repository: str,
        required_labels: list[str] | tuple[str, ...],
        limit: int,
    ) -> list[str]:
        raise ValueError("exact-Issue dispatch unexpectedly attempted candidate polling")


class ApprovedIssueDispatchExecutionEngine:
    """Reuse the original exact-Issue dispatcher without weakening any gate."""

    PR_URL_PATTERN = re.compile(
        r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/([1-9][0-9]*)"
    )

    def __init__(
        self,
        repository_path: Path,
        policy_path: Path,
        mode: str,
        model: str,
        github_timeout_seconds: float,
        audit_dir: Path,
        default_branch: str = "main",
        github_token: str = "",
        worktrees_root: Path | None = None,
    ) -> None:
        if mode not in {"dry_run", "execute", "publish_pr"}:
            raise ValueError("approved-Issue dispatch mode is invalid")
        self._repository_path = repository_path.resolve()
        self._policy_path = policy_path.resolve()
        self._mode = mode
        self._model = model
        self._github_timeout_seconds = github_timeout_seconds
        self._audit_dir = audit_dir.resolve()
        self._default_branch = default_branch
        self._github_token = github_token.strip()
        self._worktrees_root = (
            worktrees_root
            if worktrees_root is not None
            else Path(
                os.getenv(
                    "WORKER_WORKTREES_ROOT",
                    DEFAULT_WORKER_WORKTREES_ROOT,
                )
            )
        ).resolve()
        try:
            self._audit_dir.relative_to(self._repository_path)
        except ValueError:
            pass
        else:
            raise ValueError(
                "approved-Issue audit directory must be outside the repository"
            )

    def execute(
        self,
        claim: dict[str, Any],
        issue_url: str,
        trusted_policy_sha256: str = "",
        allow_missing_human_context: bool = False,
        issue_snapshot: dict[str, Any] | None = None,
    ) -> ExecutionResult:
        if not self._github_token:
            return self._execute_inner(
                claim,
                issue_url,
                trusted_policy_sha256,
                allow_missing_human_context,
            )
        # gh 与 git 子进程都走临时进程环境；不把凭据写入 remote URL。
        auth = base64.b64encode(
            f"x-access-token:{self._github_token}".encode("utf-8")
        ).decode("ascii")
        overrides = {
            "GH_TOKEN": self._github_token,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {auth}",
        }
        previous = {key: os.environ.get(key) for key in overrides}
        os.environ.update(overrides)
        try:
            return self._execute_inner(
                claim,
                issue_url,
                trusted_policy_sha256,
                allow_missing_human_context,
            )
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def _execute_inner(
        self,
        claim: dict[str, Any],
        issue_url: str,
        trusted_policy_sha256: str,
        allow_missing_human_context: bool,
    ) -> ExecutionResult:
        from src.approved_issue_dispatcher import (
            GitHubCLIDispatchStateInspector,
            GitRemoteBranchClaimer,
            dispatch_once,
        )
        from src.copilot_code_modifier import (
            CopilotCLICodeModifier,
            GitHubCLIIssueSnapshotClient,
            _atomic_write,
        )

        task_id = validate_task_id(str(claim.get("taskId", "")))
        repository = str(claim.get("matchedRepository", "")).strip()
        if not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository
        ):
            raise WorkerError("approved-Issue task repository is invalid")
        execute = self._mode in {"execute", "publish_pr"}
        publish_pr = self._mode == "publish_pr"
        audit_path = self._audit_dir / f"task-{task_id}-{self._mode}.json"
        try:
            with TaskRepositoryWorktree(
                repository,
                self._repository_path,
                self._worktrees_root,
                self._default_branch,
                task_id,
            ) as task_repository:
                try:
                    relative_policy = self._policy_path.relative_to(
                        self._repository_path
                    )
                    task_policy_path = task_repository / relative_policy
                except ValueError:
                    task_policy_path = self._policy_path
                report = dispatch_once(
                    task_repository,
                    task_policy_path,
                    _DisabledApprovedIssueCandidateClient(),
                    GitHubCLIIssueSnapshotClient(self._github_timeout_seconds),
                    GitHubCLIDispatchStateInspector(self._github_timeout_seconds),
                    CopilotCLICodeModifier(),
                    max_candidates=1,
                    execute=execute,
                    publish_pr=publish_pr,
                    model=self._model,
                    target_issue_url=issue_url,
                    trusted_external_policy_sha256=trusted_policy_sha256,
                    allow_missing_human_context=allow_missing_human_context,
                    claimer=(
                        GitRemoteBranchClaimer(max(self._github_timeout_seconds, 120.0))
                        if execute
                        else None
                    ),
                )
                _atomic_write(audit_path, report)
        except (FileExistsError, OSError, ValueError) as exception:
            raise WorkerError(
                "original approved-Issue dispatcher failed closed"
            ) from exception

        status = report.get("status")
        dispatch = report.get("dispatch")
        dispatch = dispatch if isinstance(dispatch, dict) else {}
        modifier_report = dispatch.get("modifier_report")
        modifier_report = modifier_report if isinstance(modifier_report, dict) else {}
        tests = modifier_report.get("tests")
        tests = tests if isinstance(tests, list) else []
        changes = modifier_report.get("changes")
        changes = changes if isinstance(changes, dict) else {}
        changed_paths = changes.get("paths")
        changed_paths = changed_paths if isinstance(changed_paths, list) else []
        audit_reference = audit_path.name
        test_summary = (
            f"策略测试 {len(tests)} 项通过；变更文件 {len(changed_paths)} 个；"
            f"审计文件 {audit_reference}"
        )
        if status == "draft_pr_created":
            publication = modifier_report.get("publication")
            publication = publication if isinstance(publication, dict) else {}
            pr_url = publication.get("draft_pr_url")
            match = self.PR_URL_PATTERN.fullmatch(pr_url if isinstance(pr_url, str) else "")
            if match is None:
                raise WorkerError("approved-Issue dispatcher returned an invalid Draft PR")
            return ExecutionResult(
                target_status="AWAITING_PR_REVIEW",
                detail="原 CLI 的审批、Claim、Copilot、差异和测试门禁全部通过；Draft PR 等待人工审核",
                pr_number=int(match.group(1)),
                pr_url=pr_url,
                test_summary=test_summary,
            )
        if status == "tested":
            return ExecutionResult(
                target_status="COMPLETED",
                detail="原 CLI 的审批、Claim、Copilot、差异和测试门禁全部通过；未发布 Draft PR",
                test_summary=test_summary,
            )
        if status == "ready":
            return ExecutionResult(
                target_status="NEEDS_CONTEXT",
                detail="原 CLI 只读预检通过；需要显式启用 execute 或 publish_pr 模式",
            )
        failure_reason = dispatch.get("failure_reason")
        safe_reason = (
            failure_reason
            if isinstance(failure_reason, str) and re.fullmatch(r"[a-z0-9_]{1,100}", failure_reason)
            else "approval_or_dispatch_gate_blocked"
        )
        return ExecutionResult(
            target_status="NEEDS_CONTEXT",
            detail=f"原 CLI 门禁阻止后续执行：{safe_reason}；请检查审批标签、Issue 快照、策略或既有 Claim/PR",
        )


class CloudAgentDispatchExecutionEngine:
    """Run exact approved Issues through GitHub's cloud-agent task API."""

    def __init__(
        self,
        token: str,
        repository_token: str,
        task_client: TaskClient,
        policy_path: Path,
        model: str,
        github_timeout_seconds: float,
        poll_interval_seconds: float,
        max_wait_seconds: float,
    ) -> None:
        from src.cloud_agent import CloudAgentExecution, GitHubCloudAgentClient

        self._execution = CloudAgentExecution(
            GitHubCloudAgentClient(
                token,
                github_timeout_seconds,
                repository_token=repository_token,
            ),
            task_client,
            policy_path,
            model,
            poll_interval_seconds,
            max_wait_seconds,
        )

    def preflight(self, repository: str) -> None:
        try:
            self._execution.preflight(repository)
        except ValueError as exception:
            raise WorkerError(
                f"Cloud Agent 权限预检失败：{exception}"
            ) from exception

    def execute(
        self,
        claim: dict[str, Any],
        issue_url: str,
        trusted_policy_sha256: str = "",
        allow_missing_human_context: bool = False,
        issue_snapshot: dict[str, Any] | None = None,
    ) -> ExecutionResult:
        if issue_snapshot is None:
            raise WorkerError("cloud-agent dispatch requires an exact Issue snapshot")
        try:
            task, pull_request, outcome = self._execution.execute(
                claim,
                issue_url,
                issue_snapshot,
                trusted_policy_sha256,
                allow_missing_human_context,
            )
        except ValueError as exception:
            raise WorkerError(
                f"GitHub cloud-agent execution failed closed: {exception}"
            ) from exception
        if pull_request is not None:
            summary = (
                f"Cloud Agent Draft PR 已通过策略校验；变更文件 "
                f"{len(pull_request.changed_paths)} 个；新增 {pull_request.additions} 行；"
                f"删除 {pull_request.deletions} 行；提交 {pull_request.head_sha[:12]}；"
                "测试和 CI 状态以 PR 页面为准"
            )
            return ExecutionResult(
                target_status="AWAITING_PR_REVIEW",
                detail=(
                    "GitHub Copilot Cloud Agent 已创建 Draft PR；"
                    "仓库、目标分支、Draft 状态、路径和变更规模均通过控制面校验；"
                    "等待人工审核，不自动合并或部署"
                ),
                pr_number=pull_request.number,
                pr_url=pull_request.url,
                test_summary=summary,
                dependencies=tuple(
                    RepositoryDependency(
                        dependency.repository,
                        dependency.reason_code,
                        dependency.summary,
                    )
                    for dependency in pull_request.dependencies
                ),
            )
        if outcome == "agent_still_running":
            return ExecutionResult(
                target_status="NEEDS_CONTEXT",
                detail=(
                    f"Cloud Agent task {task.id} 仍在运行；task 引用已持久化，"
                    "稍后重新排队将继续轮询，不会重复创建"
                ),
            )
        if outcome == "agent_waiting_for_user":
            return ExecutionResult(
                target_status="NEEDS_CONTEXT",
                detail=(
                    f"Cloud Agent task {task.id} 正在等待人工补充上下文；"
                    "请在 GitHub task 页面处理后重新排队"
                ),
            )
        if outcome == "agent_submission_ambiguous":
            return ExecutionResult(
                target_status="NEEDS_CONTEXT",
                detail=(
                    "Cloud Agent 提交预留已持久化，但没有可验证的远端 task ID；"
                    "可能存在已创建但响应丢失的任务，已停止自动重试以避免重复 PR，"
                    "需要人工核对 GitHub task 列表"
                ),
            )
        if outcome == "agent_coding_access_not_enabled":
            return ExecutionResult(
                target_status="NEEDS_CONTEXT",
                detail=(
                    "目标仓库尚未启用 GitHub Copilot Coding Agent；"
                    "GitHub 已确认远程任务未创建，本地提交预留已释放。"
                    "启用权限后在对话中发送“继续执行”即可从原 Issue 重新排队"
                ),
            )
        if outcome == "agent_completed_without_code_changes":
            return ExecutionResult(
                target_status="NEEDS_CONTEXT",
                detail=(
                    "Cloud Agent 已完成，但 Draft PR 没有产生代码变更；"
                    "请补充可定位的报错上下文或复现条件后重新执行"
                ),
            )
        return ExecutionResult(
            target_status="NEEDS_CONTEXT",
            detail=(
                "Cloud Agent 未创建可验证的 Draft PR；"
                f"安全结果：{outcome}"
            ),
        )


class CodeExecutionPreapprovalEngine:
    """Apply repository-owned approval labels only under reviewed policy bytes."""

    def __init__(
        self,
        issue_client: Any,
        policy_path: Path,
        confirmed_policy_sha256: str,
        issue_publication_policy_path: Path,
        issue_code_policy_path: Path,
        code_policy_paths: dict[str, Path] | None = None,
    ) -> None:
        self._issue_client = issue_client
        self._policy_path = policy_path.resolve()
        self._confirmed_policy_sha256 = confirmed_policy_sha256
        self._issue_publication_policy_path = issue_publication_policy_path.resolve()
        self._issue_code_policy_path = issue_code_policy_path.resolve()
        # 多仓 manifest 模式：policy_path 指向 manifest，按仓取代码策略路径
        self._code_policy_paths = (
            {repo: path.resolve() for repo, path in code_policy_paths.items()}
            if code_policy_paths is not None
            else None
        )

    def apply(
        self,
        claim: dict[str, Any],
        repository: str,
        issue_number: int,
        issue_url: str,
        publication_status: str,
        issue_snapshot: dict[str, Any] | None = None,
    ) -> "CodePreapprovalResult":
        from src.code_execution_preapproval import (
            load_code_execution_preapproval_policy,
            load_preapproval_policy_for_repository,
        )

        if self._code_policy_paths is not None:
            policy = load_preapproval_policy_for_repository(
                self._policy_path,
                self._confirmed_policy_sha256,
                repository,
                self._issue_publication_policy_path,
                self._code_policy_paths.get(repository),
            )
        else:
            policy = load_code_execution_preapproval_policy(
                self._policy_path,
                self._confirmed_policy_sha256,
                self._issue_publication_policy_path,
                self._issue_code_policy_path,
            )
        if repository != policy.repository:
            raise WorkerError("code preapproval repository is not authorized")
        expected_url = f"https://github.com/{repository}/issues/{issue_number}"
        if issue_url != expected_url:
            raise WorkerError("code preapproval Issue reference is inconsistent")
        stored_issue_sha256 = claim.get("agentIssueSha256")
        stored_policy_sha256 = claim.get("agentPolicySha256")
        reserved_snapshot = (
            stored_issue_sha256 is not None or stored_policy_sha256 is not None
        )
        current_issue = None
        if reserved_snapshot:
            from src.cloud_agent import approved_issue_from_snapshot

            if issue_snapshot is None:
                raise WorkerError(
                    "resumed cloud-agent approval requires an exact Issue snapshot"
                )
            current_issue = approved_issue_from_snapshot(
                repository, issue_url, issue_snapshot
            )
            if (
                stored_issue_sha256 != current_issue.sha256
                or stored_policy_sha256 != policy.issue_code_policy_sha256
            ):
                raise WorkerError(
                    "resumed cloud-agent Issue snapshot or policy no longer matches"
                )
        labels = policy.labels_for(
            str(claim.get("sourceType", "")), publication_status
        )
        if not labels:
            return CodePreapprovalResult((), policy.issue_code_policy_sha256)
        if reserved_snapshot:
            if current_issue is None or not set(labels).issubset(current_issue.labels):
                raise WorkerError(
                    "resumed cloud-agent Issue no longer has its required approval labels"
                )
            return CodePreapprovalResult((), policy.issue_code_policy_sha256)
        try:
            applied = self._issue_client.add_labels(repository, issue_number, labels)
        except (AttributeError, ValueError) as exception:
            raise WorkerError("code approval labels could not be applied") from exception
        if tuple(applied) != labels:
            raise WorkerError("code approval label result is inconsistent")
        return CodePreapprovalResult(labels, policy.issue_code_policy_sha256)


@dataclass(frozen=True)
class CodePreapprovalResult:
    labels: tuple[str, ...]
    issue_code_policy_sha256: str


class _RepoEngines:
    """一个授权仓库对应的一套执行引擎（定位 / Issue / 派发 / 预审批）。"""

    def __init__(
        self,
        location_engine: LocalRepositoryExecutionEngine,
        issue_client: Any,
        downstream_engine: Any | None,
        preapproval_engine: CodeExecutionPreapprovalEngine | None,
    ) -> None:
        self.location_engine = location_engine
        self.issue_client = issue_client
        self.downstream_engine = downstream_engine
        self.preapproval_engine = preapproval_engine


class RepositoryEngineRegistry:
    """按任务路由到的仓库，按需克隆并组装该仓的执行引擎（带缓存）。"""

    def __init__(self, config: WorkerConfig, task_client: TaskClient | None = None) -> None:
        self._config = config
        self._task_client = task_client
        self._cache: dict[str, _RepoEngines] = {}

    def resolve(self, repository: str) -> _RepoEngines:
        spec = self._config.repo_catalog.get(repository)
        if spec is None:
            raise WorkerError("claimed repository is not authorized for this worker")
        cached = self._cache.get(repository)
        if cached is not None:
            return cached
        token = os.environ.get(spec.token_env, "").strip()
        if not token:
            raise WorkerError(f"{spec.token_env} 未设置，无法操作 {repository}")
        from src.repository_issue_automation import GitHubRESTIssueClient

        if self._config.code_mode == "disabled":
            _ensure_repository_checkout(spec)
        location_engine = LocalRepositoryExecutionEngine(
            spec.repository, spec.path, spec.default_branch
        )
        issue_client = GitHubRESTIssueClient(token, self._config.request_timeout_seconds)
        downstream_engine = (
            CloudAgentDispatchExecutionEngine(
                self._config.cloud_agent_token,
                token,
                self._task_client,
                spec.code_policy_path,
                self._config.code_model,
                self._config.github_timeout_seconds,
                self._config.cloud_agent_poll_interval_seconds,
                self._config.cloud_agent_max_wait_seconds,
            )
            if self._config.code_mode == "cloud_agent"
            and self._task_client is not None
            else None
        )
        preapproval_engine = (
            CodeExecutionPreapprovalEngine(
                issue_client,
                self._config.code_auto_approval_policy_path,
                self._config.code_auto_approval_policy_sha256,
                self._config.issue_policy_path,
                spec.code_policy_path,
                code_policy_paths={
                    repo: repo_spec.code_policy_path
                    for repo, repo_spec in self._config.repo_catalog.items()
                },
            )
            if self._config.code_auto_approval_enabled
            else None
        )
        engines = _RepoEngines(
            location_engine, issue_client, downstream_engine, preapproval_engine
        )
        self._cache[repository] = engines
        return engines


class NaturalLanguageIssueExecutionEngine:
    """Generate a source-profiled Issue before the approved downstream flow."""

    def __init__(
        self,
        location_engine: LocalRepositoryExecutionEngine,
        issue_client: Any,
        scope_path: Path,
        policy_path: Path,
        confirmed_policy_sha256: str,
        downstream_engine: Any | None = None,
        code_preapproval_engine: CodeExecutionPreapprovalEngine | None = None,
        engine_registry: RepositoryEngineRegistry | None = None,
        progress_client: TaskClient | None = None,
    ) -> None:
        self._location_engine = location_engine
        self._issue_client = issue_client
        self._scope_path = scope_path.resolve()
        self._policy_path = policy_path.resolve()
        self._confirmed_policy_sha256 = confirmed_policy_sha256
        self._downstream_engine = downstream_engine
        self._code_preapproval_engine = code_preapproval_engine
        self._engine_registry = engine_registry
        self._progress_client = progress_client

    def _progress(self, claim: dict[str, Any], stage: str, detail: str) -> None:
        if self._progress_client is None:
            return
        self._progress_client.record_progress(
            validate_task_id(str(claim.get("taskId", ""))),
            stage,
            detail,
        )

    def execute(self, claim: dict[str, Any]) -> ExecutionResult:
        from src import ai_issue_generator
        from src.issue_entry import compose_evidence
        from src.repository_issue_automation import (
            ROUTING_MODE_DEMO_SINGLE_REPOSITORY,
            automate_repository_issue,
            load_auto_publish_policy,
        )
        from src.repository_resolver import load_search_scope

        repository = str(claim.get("matchedRepository", ""))
        requirement = str(claim.get("normalizedRequirement", "")).strip()
        if self._engine_registry is not None:
            engines = self._engine_registry.resolve(repository)
            issue_client = engines.issue_client
            downstream_engine = engines.downstream_engine
            preapproval_engine = engines.preapproval_engine
            location_engine = engines.location_engine
        else:
            issue_client = self._issue_client
            downstream_engine = self._downstream_engine
            preapproval_engine = self._code_preapproval_engine
            location_engine = self._location_engine
        if downstream_engine is not None:
            self._progress(
                claim,
                "PREPARING_CODE_CHANGE",
                f"正在确认 {repository} 的 Cloud Agent 仓库权限和模型策略。",
            )
            downstream_engine.preflight(repository)
        try:
            attached_number = claim.get("issueNumber")
            attached_url = claim.get("issueUrl")
            if attached_number is not None or attached_url:
                # Retry path: the task already references a published Issue from an
                # earlier run. Reuse it instead of generating a duplicate.
                if (
                    isinstance(attached_number, bool)
                    or not isinstance(attached_number, int)
                    or attached_number < 1
                    or not isinstance(attached_url, str)
                    or attached_url
                    != f"https://github.com/{repository}/issues/{attached_number}"
                ):
                    raise WorkerError("claim carries an inconsistent attached Issue reference")
                publication: dict[str, Any] = {
                    "issue_number": attached_number,
                    "issue_url": attached_url,
                }
                status = "resumed"
            else:
                self._progress(
                    claim,
                    "DRAFTING_ISSUE",
                    "正在整理需求、仓库上下文和已脱敏证据，生成 Issue 草稿。",
                )
                if claim.get("sourceType") == "LOG":
                    original_summary = str(claim.get("inputSummary", "")).strip()
                    evidence = self._compose_log_evidence(
                        claim,
                        original_summary or requirement,
                    )
                    input_type = "sanitized_evidence"
                elif claim.get("sourceType") == "JIRA":
                    original_summary = str(claim.get("inputSummary", "")).strip()
                    evidence = self._compose_jira_evidence(
                        claim,
                        original_summary or requirement,
                        repository,
                    )
                    input_type = "natural_language"
                else:
                    evidence = compose_evidence(requirement)
                    input_type = "natural_language"
                    facts = evidence.get("facts")
                    if (
                        repository
                        and isinstance(facts, dict)
                        and "repository" not in facts
                    ):
                        facts["repository"] = repository
                gateway = ai_issue_generator.GatewayConfig.from_env()
                generation = None
                last_error: ValueError | None = None
                for _attempt in range(3):
                    try:
                        generation = ai_issue_generator.generate_issue(
                            evidence,
                            ai_issue_generator.OpenAICompatibleChatProvider(
                                gateway, gateway.model
                            ),
                            ai_issue_generator.OpenAICompatibleChatProvider(
                                gateway, gateway.review_model
                            ),
                        )
                    except ValueError as exc:
                        last_error = exc
                        if _attempt < 2:
                            # 网关限流（429）等瞬时故障：退避后再试
                            time.sleep(10 * (_attempt + 1))
                        continue
                    break
                if generation is None:
                    detail = f": {last_error}" if last_error else ""
                    raise WorkerError(
                        f"Issue generation failed repeatedly{detail}"
                    ) from last_error
                self._progress(
                    claim,
                    "VALIDATING_ISSUE",
                    "Issue 草稿已生成，正在校验必要字段、证据边界和发布策略。",
                )
                scope = load_search_scope(self._scope_path)
                policy = load_auto_publish_policy(
                    self._policy_path,
                    self._confirmed_policy_sha256,
                    scope,
                    self._scope_path,
                )
                if policy.provider != "github_rest_api":
                    raise ValueError("worker Issue publication policy must use github_rest_api")
                self._progress(
                    claim,
                    "PUBLISHING_ISSUE",
                    f"发布门禁已通过，正在为 {repository} 创建或复用 GitHub Issue。",
                )
                automation = automate_repository_issue(
                    generation,
                    evidence,
                    scope,
                    _DisabledRepositorySearchAdapter(),
                    "github-tree-probe",
                    policy,
                    issue_client,
                    True,
                    preselected_repository=repository,
                    routing_mode=ROUTING_MODE_DEMO_SINGLE_REPOSITORY,
                    input_type=input_type,
                )
                publication = automation.get("publication")
                if not isinstance(publication, dict):
                    raise WorkerError("Issue automation returned an invalid publication result")
                status = publication.get("status")
        except ValueError as exception:
            raise WorkerError(
                f"Issue generation or publication failed closed: {exception}"
            ) from exception

        if status not in {"created", "deduplicated", "resumed"}:
            missing: list[str] = []
            review = generation.get("review") if isinstance(generation, dict) else None
            if isinstance(review, dict):
                fields = review.get("missing_critical_fields")
                if isinstance(fields, list):
                    missing = [str(field) for field in fields[:6] if str(field).strip()]
            if not missing and isinstance(generation, dict):
                draft = generation.get("draft")
                if isinstance(draft, dict):
                    info = draft.get("missing_information")
                    if isinstance(info, list):
                        missing = [str(item) for item in info[:6] if str(item).strip()]
            missing_text = "；缺失信息：" + "、".join(missing) if missing else ""
            return ExecutionResult(
                target_status="NEEDS_CONTEXT",
                detail=(
                    f"Issue 自动化未达到可发布状态（{status}）{missing_text}；"
                    "请在对话中补充上述信息，我会重新生成 Issue；"
                    "未启动 Copilot，也未修改仓库"
                ),
            )
        issue_number = publication.get("issue_number")
        issue_url = publication.get("issue_url")
        if not isinstance(issue_number, int) or not isinstance(issue_url, str):
            raise WorkerError("Issue automation returned an incomplete Issue reference")

        try:
            self._progress(
                claim,
                "ISSUE_READY",
                f"Issue #{issue_number} 已确认，正在重新读取并绑定精确快照。",
            )
            issue_snapshot = issue_client.get_issue(repository, issue_number)
            ai_issue_generator.compact_evidence(
                {
                    "html_url": issue_snapshot.get("url"),
                    "title": issue_snapshot.get("title"),
                    "body": issue_snapshot.get("body"),
                    "number": issue_snapshot.get("number"),
                    "repository_url": issue_snapshot.get("repository_url"),
                    "labels": [],
                }
            )
            preapproval_result: CodePreapprovalResult | None = None
            if preapproval_engine is not None:
                self._progress(
                    claim,
                    "PREPARING_CODE_CHANGE",
                    "正在校验代码执行策略、Issue 快照和预审批标签。",
                )
                preapproval_result = preapproval_engine.apply(
                    claim,
                    repository,
                    issue_number,
                    issue_url,
                    str(status),
                    issue_snapshot,
                )
                if preapproval_result.labels:
                    issue_snapshot = issue_client.get_issue(repository, issue_number)
            downstream = (
                self._execute_downstream(
                    downstream_engine,
                    claim,
                    issue_url,
                    (
                        preapproval_result.issue_code_policy_sha256
                        if preapproval_result is not None
                        else ""
                    ),
                    (
                        claim.get("sourceType") == "LOG"
                        or (
                            claim.get("sourceType") == "JIRA"
                            and _jira_code_context_sufficient(claim)
                        )
                    ),
                    issue_snapshot,
                )
                if downstream_engine is not None
                else None
            )
            if downstream is not None and downstream.target_status == "AWAITING_PR_REVIEW":
                self._progress(
                    claim,
                    "VALIDATING_DRAFT_PR",
                    "代码修改已返回，正在校验 Draft PR、目标分支和变更范围。",
                )
                from src.cloud_agent import approved_issue_from_snapshot

                final_issue_snapshot = issue_client.get_issue(repository, issue_number)
                submitted_issue = approved_issue_from_snapshot(
                    repository, issue_url, issue_snapshot
                )
                final_issue = approved_issue_from_snapshot(
                    repository, issue_url, final_issue_snapshot
                )
                if final_issue.sha256 != submitted_issue.sha256:
                    raise WorkerError(
                        "Issue changed while downstream code execution was running"
                    )
        except (AttributeError, ValueError, WorkerError) as exception:
            raise PublishedIssueWorkerError(
                issue_number, issue_url, str(exception)
            ) from exception
        if downstream is not None:
            return ExecutionResult(
                target_status=downstream.target_status,
                detail=(
                    f"Issue #{issue_number} 已{('创建' if status == 'created' else '恢复复用')}并重新读取；"
                    + (
                        "公司预授权策略已写入代码审批标签；"
                        if preapproval_result is not None
                        and preapproval_result.labels
                        else ""
                    )
                    + downstream.detail
                )[:1000],
                candidate_count=downstream.candidate_count,
                issue_number=issue_number,
                issue_url=issue_url,
                pr_number=downstream.pr_number,
                pr_url=downstream.pr_url,
                test_summary=downstream.test_summary,
            )
        location = location_engine.execute({**claim, "approvedIssue": issue_snapshot})
        return ExecutionResult(
            target_status=location.target_status,
            detail=(f"Issue #{issue_number} 已{('创建' if status == 'created' else '恢复复用')}并重新读取；"
                    + location.detail)[:1000],
            candidate_count=location.candidate_count,
            issue_number=issue_number,
            issue_url=issue_url,
        )

    def _execute_downstream(
        self,
        downstream_engine: Any,
        claim: dict[str, Any],
        issue_url: str,
        policy_sha256: str,
        allow_missing_human_context: bool,
        issue_snapshot: dict[str, Any],
    ) -> ExecutionResult:
        self._progress(
            claim,
            "CODING_AND_TESTING",
            "Cloud Agent 正在修改代码、运行仓库检查并准备 Draft PR。",
        )
        return downstream_engine.execute(
            claim,
            issue_url,
            policy_sha256,
            allow_missing_human_context,
            issue_snapshot=issue_snapshot,
        )

    @staticmethod
    def _compose_jira_evidence(
        claim: dict[str, Any],
        summary: str,
        repository: str,
    ) -> dict[str, Any]:
        from src.ai_issue_generator import EVIDENCE_SCHEMA_VERSION

        source = claim.get("logIncident")
        reference = source.get("sourceReference") if isinstance(source, dict) else None
        if (
            not isinstance(reference, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]*-[1-9][0-9]*", reference)
        ):
            raise ValueError("JIRA claim has no valid sanitized source reference")
        if not summary.strip():
            raise ValueError("JIRA claim has no sanitized summary")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("JIRA claim repository must be owner/repository")
        return {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "source": {
                "type": "jira",
                "reference": reference,
                "url": "",
            },
            "safety": {
                "status": "sanitized",
                "ai_allowed": True,
                "security_review_required": False,
                "redacted_categories": [],
            },
            "facts": {
                "reported_description": summary.strip(),
                "repository": repository,
            },
        }

    @staticmethod
    def _compose_log_evidence(
        claim: dict[str, Any],
        summary: str,
    ) -> dict[str, Any]:
        from src.ai_issue_generator import EVIDENCE_SCHEMA_VERSION

        incident = claim.get("logIncident")
        if not isinstance(incident, dict):
            raise ValueError("LOG claim has no sanitized incident evidence")

        def required_int(name: str, minimum: int = 0) -> int:
            value = incident.get(name)
            if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
                raise ValueError(f"LOG claim has invalid {name}")
            return value

        reference = incident.get("sourceReference")
        first_seen = incident.get("firstSeenAt")
        last_seen = incident.get("lastSeenAt")
        endpoints = incident.get("affectedEndpoints")
        basis = incident.get("aggregationBasis")
        if (
            not isinstance(reference, str)
            or not re.fullmatch(r"(?:incident_ref|event_ref):[0-9a-f]{16,64}", reference)
            or not isinstance(first_seen, str)
            or not isinstance(last_seen, str)
            or not isinstance(endpoints, list)
            or any(not isinstance(item, str) or not item.strip() for item in endpoints)
            or not isinstance(basis, str)
            or not basis.strip()
        ):
            raise ValueError("LOG claim has invalid observability fields")

        user_min = incident.get("affectedUserCountMin")
        user_max = incident.get("affectedUserCountMax")
        if (user_min is None) != (user_max is None):
            raise ValueError("LOG claim has an incomplete affected-user range")
        if user_min is not None and (
            not isinstance(user_min, int)
            or isinstance(user_min, bool)
            or not isinstance(user_max, int)
            or isinstance(user_max, bool)
            or user_min < 0
            or user_min > user_max
        ):
            raise ValueError("LOG claim has an invalid affected-user range")

        current_count = required_int("currentScanEventCount", 1)
        historical_count = required_int("historicalEventCount", 1)
        incident_count = required_int("incidentGroupCount", 1)
        identifier_count = required_int("userIdentifierEventCount")
        if (
            current_count > historical_count
            or incident_count > historical_count
            or identifier_count > historical_count
        ):
            raise ValueError("LOG claim has inconsistent occurrence counts")
        historical_complete = incident.get("historicalCountComplete")
        if not isinstance(historical_complete, bool):
            raise ValueError("LOG claim has invalid historicalCountComplete")

        def explicit_line(*labels: str) -> str:
            label_pattern = "|".join(re.escape(label) for label in labels)
            match = re.search(
                rf"(?i)(?:^|[;；|])\s*(?:{label_pattern})\s*[:：]\s*"
                rf"(.+?)\s*(?=[;；|]|$)",
                summary,
            )
            return match.group(1).strip() if match else ""

        current_behavior = explicit_line("current behavior", "observed behavior", "当前行为", "实际行为")
        expected_behavior = explicit_line("expected behavior", "期望行为", "预期行为")
        facts: dict[str, Any] = {"reported_description": summary}
        repository = str(claim.get("matchedRepository", "")).strip()
        if repository:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
                raise WorkerError("claimed task repository must be owner/repository")
            facts["repository"] = repository
        if current_behavior:
            facts["current_behavior"] = current_behavior
        if expected_behavior:
            facts["expected_behavior"] = expected_behavior

        return {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "source": {
                "type": "kibana",
                "reference": reference,
                "url": "",
            },
            "safety": {
                "status": "sanitized",
                "ai_allowed": True,
                "security_review_required": False,
                "redacted_categories": [],
            },
            "facts": facts,
            "event": {
                "level": "ERROR",
                "summary": current_behavior or summary,
                "event_count": current_count,
                "statistics": {
                    "batch_event_count": current_count,
                    "total_event_count": historical_count,
                    "candidate_count": incident_count,
                    "affected_endpoints": endpoints,
                    "affected_user_count_min": user_min,
                    "affected_user_count_max": user_max,
                    "user_identifier_event_count": identifier_count,
                    "historical_count_complete": historical_complete,
                    "aggregation_components": {
                        "services": [],
                        "paths": endpoints,
                        "exceptions": [],
                        "systems": [],
                        "top_frames": [],
                    },
                },
                "grouping_basis": basis,
            },
            "runtime": {
                "first_seen_at": first_seen,
                "last_seen_at": last_seen,
            },
        }


class RedisTaskQueue:
    _ALLOWED_FIELDS = {"taskId", "attempt", "enqueuedAt", "schemaVersion"}

    def __init__(
        self,
        redis_url: str,
        queue_key: str,
        consumer_group: str,
        consumer_name: str,
        dead_letter_key: str,
        metrics_key: str,
        stale_idle_ms: int,
        max_retries: int,
        dead_letter_max_length: int,
    ) -> None:
        try:
            import redis
        except ImportError as exception:
            raise WorkerError(
                "redis dependency is missing; install requirements-worker.txt"
            ) from exception
        self._client = redis.Redis.from_url(redis_url, decode_responses=True)
        self._response_error = redis.exceptions.ResponseError
        self._queue_key = queue_key
        self._consumer_group = consumer_group
        self._consumer_name = consumer_name
        self._dead_letter_key = dead_letter_key
        self._metrics_key = metrics_key
        self._stale_idle_ms = stale_idle_ms
        self._max_retries = max_retries
        self._dead_letter_max_length = dead_letter_max_length
        self._ensure_consumer_group()

    def _ensure_consumer_group(self) -> None:
        try:
            self._client.xgroup_create(
                self._queue_key,
                self._consumer_group,
                id="0-0",
                mkstream=True,
            )
        except self._response_error as exception:
            if "BUSYGROUP" not in str(exception):
                raise WorkerError("local Redis consumer group is unavailable") from exception
        except Exception as exception:
            raise WorkerError("local Redis consumer group is unavailable") from exception

    def next_message(self, timeout_seconds: int) -> QueueMessage | None:
        try:
            reclaimed = self._client.xautoclaim(
                self._queue_key,
                self._consumer_group,
                self._consumer_name,
                self._stale_idle_ms,
                start_id="0-0",
                count=1,
            )
            reclaimed_messages = reclaimed[1] if len(reclaimed) > 1 else []
            if reclaimed_messages:
                self._client.hincrby(self._metrics_key, "stale_reclaimed", 1)
                return self._decode_or_quarantine(reclaimed_messages[0])
            streams = self._client.xreadgroup(
                self._consumer_group,
                self._consumer_name,
                {self._queue_key: ">"},
                count=1,
                block=timeout_seconds * 1000,
            )
        except Exception as exception:
            raise WorkerError("local Redis stream is unavailable") from exception
        if not streams:
            return None
        _, messages = streams[0]
        if not messages:
            return None
        return self._decode_or_quarantine(messages[0])

    def acknowledge(self, message: QueueMessage) -> None:
        try:
            pipeline = self._client.pipeline(transaction=True)
            pipeline.xack(self._queue_key, self._consumer_group, message.message_id)
            pipeline.xdel(self._queue_key, message.message_id)
            pipeline.hincrby(self._metrics_key, "acknowledged", 1)
            pipeline.execute()
        except Exception as exception:
            raise WorkerError("could not acknowledge Redis stream message") from exception

    def retry_or_dead_letter(self, message: QueueMessage, reason: str) -> str:
        safe_reason = reason if reason in {"control_plane_unavailable"} else "worker_retry"
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        try:
            pipeline = self._client.pipeline(transaction=True)
            if message.attempt < self._max_retries:
                pipeline.xadd(
                    self._queue_key,
                    {
                        "taskId": message.task_id,
                        "attempt": str(message.attempt + 1),
                        "enqueuedAt": now,
                        "schemaVersion": "1",
                    },
                )
                pipeline.hincrby(self._metrics_key, "retried", 1)
                disposition = "retried"
            else:
                pipeline.xadd(
                    self._dead_letter_key,
                    {
                        "taskId": message.task_id,
                        "attempt": str(message.attempt),
                        "originalMessageId": message.message_id,
                        "failedAt": now,
                        "reason": safe_reason,
                        "schemaVersion": "1",
                    },
                    maxlen=self._dead_letter_max_length,
                    approximate=True,
                )
                pipeline.hincrby(self._metrics_key, "dead_lettered", 1)
                disposition = "dead_lettered"
            pipeline.xack(self._queue_key, self._consumer_group, message.message_id)
            pipeline.xdel(self._queue_key, message.message_id)
            pipeline.execute()
            return disposition
        except Exception as exception:
            raise WorkerError("could not retry Redis stream message") from exception

    def _decode_or_quarantine(self, raw_message: tuple[str, dict[str, str]]) -> QueueMessage | None:
        message_id, fields = raw_message
        try:
            if set(fields) != self._ALLOWED_FIELDS:
                raise WorkerError("stream message fields do not match the schema")
            if fields.get("schemaVersion") != "1":
                raise WorkerError("stream message schema is unsupported")
            task_id = validate_task_id(fields.get("taskId", ""))
            attempt = int(fields.get("attempt", ""))
            if attempt < 0 or attempt > self._max_retries:
                raise WorkerError("stream message attempt is invalid")
            return QueueMessage(message_id, task_id, attempt)
        except (ValueError, WorkerError):
            self._quarantine_invalid(message_id)
            return None

    def _quarantine_invalid(self, message_id: str) -> None:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        try:
            pipeline = self._client.pipeline(transaction=True)
            pipeline.xadd(
                self._dead_letter_key,
                {
                    "originalMessageId": message_id,
                    "failedAt": now,
                    "reason": "invalid_message_contract",
                    "schemaVersion": "1",
                },
                maxlen=self._dead_letter_max_length,
                approximate=True,
            )
            pipeline.xack(self._queue_key, self._consumer_group, message_id)
            pipeline.xdel(self._queue_key, message_id)
            pipeline.hincrby(self._metrics_key, "invalid_dead_lettered", 1)
            pipeline.execute()
        except Exception as exception:
            raise WorkerError("could not quarantine invalid Redis message") from exception


class ControlPlaneClient:
    def __init__(self, base_url: str, timeout_seconds: float) -> None:
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds

    def claim(self, task_id: str) -> dict[str, Any]:
        try:
            response = self._post_json(f"/api/internal/tasks/{task_id}/claim", None)
        except HTTPError as exception:
            if exception.code == 409:
                raise StaleTaskError("task is no longer claimable") from exception
            raise WorkerError("control plane rejected the task claim") from exception
        except (URLError, TimeoutError) as exception:
            raise WorkerError("local control plane is unavailable") from exception
        return self._validate_claim(task_id, response)

    def transition(self, task_id: str, target_status: str, detail: str) -> None:
        try:
            self._post_json(
                f"/api/internal/tasks/{task_id}/transitions",
                {"targetStatus": target_status, "detail": detail},
            )
        except (HTTPError, URLError, TimeoutError) as exception:
            raise WorkerError(f"control plane rejected transition to {target_status}") from exception

    def record_progress(self, task_id: str, stage: str, detail: str) -> None:
        try:
            self._post_json(
                f"/api/internal/tasks/{task_id}/progress",
                {"stage": stage, "detail": detail},
            )
        except (HTTPError, URLError, TimeoutError) as exception:
            raise WorkerError("control plane rejected the worker progress event") from exception

    def attach_issue(self, task_id: str, issue_number: int, issue_url: str) -> None:
        try:
            self._post_json(
                f"/api/internal/tasks/{task_id}/issue",
                {"issueNumber": issue_number, "issueUrl": issue_url},
            )
        except (HTTPError, URLError, TimeoutError) as exception:
            raise WorkerError("control plane rejected the GitHub Issue reference") from exception

    def attach_agent_task(
        self, task_id: str, agent_task_id: str, agent_task_url: str
    ) -> None:
        try:
            self._post_json(
                f"/api/internal/tasks/{task_id}/agent-task",
                {
                    "agentTaskId": agent_task_id,
                    "agentTaskUrl": agent_task_url,
                },
            )
        except (HTTPError, URLError, TimeoutError) as exception:
            raise WorkerError(
                "control plane rejected the cloud-agent task reference"
            ) from exception

    def reserve_agent_task(
        self,
        task_id: str,
        submission_key: str,
        issue_sha256: str,
        policy_sha256: str,
    ) -> None:
        try:
            self._post_json(
                f"/api/internal/tasks/{task_id}/agent-task-reservation",
                {
                    "submissionKey": submission_key,
                    "issueSha256": issue_sha256,
                    "policySha256": policy_sha256,
                },
            )
        except (HTTPError, URLError, TimeoutError) as exception:
            raise WorkerError(
                "control plane rejected the cloud-agent submission reservation"
            ) from exception

    def release_agent_task_reservation(self, task_id: str) -> None:
        try:
            self._post_json(
                f"/api/internal/tasks/{task_id}/agent-task-reservation/release",
                None,
            )
        except (HTTPError, URLError, TimeoutError) as exception:
            raise WorkerError(
                "control plane rejected release of cloud-agent submission reservation"
            ) from exception

    def heartbeat(self, task_id: str) -> None:
        try:
            self._post_json(f"/api/internal/tasks/{task_id}/heartbeat", None)
        except (HTTPError, URLError, TimeoutError) as exception:
            raise WorkerError("control plane rejected the worker heartbeat") from exception

    def attach_pull_request(
        self,
        task_id: str,
        pr_number: int,
        pr_url: str,
        test_summary: str,
    ) -> None:
        try:
            self._post_json(
                f"/api/internal/tasks/{task_id}/pull-request",
                {
                    "prNumber": pr_number,
                    "prUrl": pr_url,
                    "testSummary": test_summary,
                },
            )
        except (HTTPError, URLError, TimeoutError) as exception:
            raise WorkerError("control plane rejected the Draft PR reference") from exception

    def create_dependency_tasks(
        self,
        task_id: str,
        dependencies: tuple[RepositoryDependency, ...],
    ) -> None:
        try:
            self._post_json(
                f"/api/internal/tasks/{task_id}/dependencies",
                {
                    "dependencies": [
                        {
                            "targetRepository": dependency.repository,
                            "reasonCode": dependency.reason_code,
                            "summary": dependency.summary,
                        }
                        for dependency in dependencies
                    ],
                },
            )
        except (HTTPError, URLError, TimeoutError) as exception:
            raise WorkerError(
                "control plane rejected the cross-repository dependency"
            ) from exception

    def _post_json(self, path: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            self._base_url + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=self._timeout_seconds) as response:
            body = response.read()
        if not body:
            return {}
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exception:
            raise WorkerError("control plane returned invalid JSON") from exception
        if not isinstance(parsed, dict):
            raise WorkerError("control plane returned an invalid response shape")
        return parsed

    @staticmethod
    def _validate_claim(task_id: str, claim: dict[str, Any]) -> dict[str, Any]:
        if claim.get("taskId") != task_id:
            raise WorkerError("claimed task identifier does not match the queue item")
        if claim.get("executionMode") != "MOCK":
            raise WorkerError("this worker accepts MOCK tasks only")
        source_type = claim.get("sourceType")
        if source_type not in {"NATURAL_LANGUAGE", "LOG", "JIRA"}:
            raise WorkerError(
                "this worker accepts NATURAL_LANGUAGE, LOG and JIRA tasks only"
            )
        expected_profile = {
            "LOG": "LOG_INCIDENT",
            "JIRA": "JIRA_ISSUE",
        }.get(source_type, "NATURAL_LANGUAGE")
        if claim.get("issueProfile") != expected_profile:
            raise WorkerError("claimed task has an inconsistent Issue profile")
        requirement = claim.get("normalizedRequirement")
        if not isinstance(requirement, str) or not requirement.strip():
            raise WorkerError("claimed task has no normalized requirement")
        repository = claim.get("matchedRepository")
        if not isinstance(repository, str) or not repository.strip():
            raise WorkerError("claimed task has no matched repository")
        if source_type == "LOG" and not isinstance(claim.get("logIncident"), dict):
            raise WorkerError("claimed LOG task has no incident evidence")
        agent_task_id = claim.get("agentTaskId")
        agent_task_url = claim.get("agentTaskUrl")
        if (agent_task_id is None) != (agent_task_url is None):
            raise WorkerError("claimed task has an incomplete cloud-agent reference")
        reservation = (
            claim.get("agentSubmissionKey"),
            claim.get("agentIssueSha256"),
            claim.get("agentPolicySha256"),
        )
        if any(value is not None for value in reservation):
            if any(
                not isinstance(value, str)
                or re.fullmatch(r"[0-9a-f]{64}", value) is None
                for value in reservation
            ):
                raise WorkerError(
                    "claimed task has an incomplete cloud-agent submission reservation"
                )
        elif agent_task_id is not None:
            raise WorkerError(
                "claimed cloud-agent task has no submission reservation"
            )
        return claim


def process_task(
    task_id: str,
    client: TaskClient,
    engine: ExecutionEngine | None = None,
) -> str:
    execution_engine = engine or SyntheticExecutionEngine()
    try:
        claim = client.claim(task_id)
    except StaleTaskError:
        LOGGER.info("task_id=%s result=stale", task_id)
        return "stale"
    except WorkerError:
        transition_recorded = False
        try:
            client.transition(
                task_id,
                "FAILED",
                "mock worker could not validate the claim; no external systems were called",
            )
            transition_recorded = True
        except WorkerError:
            pass
        LOGGER.error("task_id=%s result=claim_failed", task_id)
        return "failed" if transition_recorded else "retry"

    try:
        execution = execution_engine.execute(claim)
        if execution.issue_number is not None or execution.issue_url:
            if execution.issue_number is None or not execution.issue_url:
                raise WorkerError("execution engine returned an incomplete Issue reference")
            client.attach_issue(
                task_id,
                execution.issue_number,
                execution.issue_url,
            )
        if execution.target_status == "NEEDS_CONTEXT":
            client.transition(task_id, "NEEDS_CONTEXT", execution.detail)
            LOGGER.info("task_id=%s result=needs_context", task_id)
            return "needs_context"
        if execution.target_status == "AWAITING_PR_REVIEW":
            if execution.pr_number is None or not execution.pr_url or not execution.test_summary:
                raise WorkerError("execution engine returned an incomplete Draft PR result")
            client.transition(
                task_id,
                "TESTING",
                execution.test_summary,
            )
            client.attach_pull_request(
                task_id,
                execution.pr_number,
                execution.pr_url,
                execution.test_summary,
            )
            if execution.dependencies:
                try:
                    client.create_dependency_tasks(task_id, execution.dependencies)
                except WorkerError as exception:
                    detail = str(exception).strip() or "dependency escalation rejected"
                    client.record_progress(
                        task_id,
                        "CROSS_REPO_DEPENDENCY_REJECTED",
                        "主仓 Draft PR 已保留，但跨仓子任务未创建：" + detail[:300],
                    )
            client.transition(
                task_id,
                "AWAITING_PR_REVIEW",
                execution.detail,
            )
            LOGGER.info("task_id=%s result=awaiting_pr_review", task_id)
            return "awaiting_pr_review"
        if execution.target_status != "COMPLETED":
            raise WorkerError("execution engine returned an unsupported terminal status")
        client.transition(
            task_id,
            "TESTING",
            (
                "旧代码定位完成，开始本地合成校验；"
                f"候选文件 {execution.candidate_count} 个；未修改仓库"
            ),
        )
        client.transition(
            task_id,
            "COMPLETED",
            execution.detail,
        )
    except PublishedIssueWorkerError as exception:
        transition_recorded = False
        try:
            client.attach_issue(
                task_id,
                exception.issue_number,
                exception.issue_url,
            )
            detail = str(exception).replace("post-Issue execution failed safely", "").strip(": ")
            client.transition(
                task_id,
                "FAILED",
                "Issue 已记录，但后续审批标签或 Cloud Agent 执行安全失败；未合并、未部署"
                + (f"；原因：{detail}" if detail else ""),
            )
            transition_recorded = True
        except WorkerError:
            pass
        LOGGER.error("task_id=%s result=post_issue_failed", task_id)
        return "failed" if transition_recorded else "retry"
    except WorkerError as exception:
        transition_recorded = False
        detail = str(exception).strip() or "unknown worker error"
        if len(detail) > 400:
            detail = detail[:397] + "..."
        try:
            client.transition(
                task_id,
                "FAILED",
                f"worker failed safely: {detail}",
            )
            transition_recorded = True
        except WorkerError:
            pass
        LOGGER.error("task_id=%s result=failed reason=%s", task_id, detail)
        return "failed" if transition_recorded else "retry"

    LOGGER.info("task_id=%s result=completed", task_id)
    return "completed"


def run_once(
    queue: TaskQueue,
    client: TaskClient,
    timeout_seconds: int,
    engine: ExecutionEngine | None = None,
) -> str:
    message = queue.next_message(timeout_seconds)
    if message is None:
        LOGGER.info("result=no_task")
        return "no_task"
    result = process_task(message.task_id, client, engine)
    if result == "retry":
        disposition = queue.retry_or_dead_letter(
            message,
            "control_plane_unavailable",
        )
        LOGGER.warning("task_id=%s queue_result=%s", message.task_id, disposition)
        return disposition
    queue.acknowledge(message)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run local mock tasks (continuous by default)"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="process at most one queue item and exit",
    )
    parser.add_argument(
        "--wait-timeout",
        type=int,
        default=5,
        help="bounded Redis wait in seconds per iteration (default: 5)",
    )
    parser.add_argument(
        "--error-backoff",
        type=int,
        default=5,
        help="seconds to wait after an unexpected worker error (default: 5)",
    )
    args = parser.parse_args(argv)
    if args.wait_timeout < 1 or args.wait_timeout > 60:
        parser.error("--wait-timeout must be between 1 and 60 seconds")
    if args.error_backoff < 1 or args.error_backoff > 300:
        parser.error("--error-backoff must be between 1 and 300 seconds")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        config = WorkerConfig.from_environment(args.wait_timeout)
        queue = RedisTaskQueue(
            config.redis_url,
            config.queue_key,
            config.consumer_group,
            config.consumer_name,
            config.dead_letter_key,
            config.metrics_key,
            config.stale_idle_ms,
            config.max_retries,
            config.dead_letter_max_length,
        )
        client = ControlPlaneClient(
            config.control_plane_url,
            config.request_timeout_seconds,
        )
        default_spec = config.repo_catalog.get(config.authorized_repository)
        default_branch = default_spec.default_branch if default_spec else "main"
        location_engine = LocalRepositoryExecutionEngine(
            config.authorized_repository,
            config.repository_path,
            default_branch,
        )
        engine: ExecutionEngine = location_engine
        if config.issue_publication_enabled:
            from src.repository_issue_automation import GitHubRESTIssueClient

            issue_client = GitHubRESTIssueClient.from_environment(
                config.request_timeout_seconds
            )
            engine_registry = RepositoryEngineRegistry(config, client)
            repository_token = (
                os.environ.get(default_spec.token_env, "").strip()
                if default_spec is not None
                else os.environ.get("GITHUB_ISSUE_TOKEN", "").strip()
            )
            downstream_engine = (
                CloudAgentDispatchExecutionEngine(
                    config.cloud_agent_token,
                    repository_token,
                    client,
                    config.code_policy_path,
                    config.code_model,
                    config.github_timeout_seconds,
                    config.cloud_agent_poll_interval_seconds,
                    config.cloud_agent_max_wait_seconds,
                )
                if config.code_mode == "cloud_agent"
                else None
            )
            code_preapproval_engine = (
                CodeExecutionPreapprovalEngine(
                    issue_client,
                    config.code_auto_approval_policy_path,
                    config.code_auto_approval_policy_sha256,
                    config.issue_policy_path,
                    config.code_policy_path,
                )
                if config.code_auto_approval_enabled
                else None
            )
            engine = NaturalLanguageIssueExecutionEngine(
                location_engine,
                issue_client,
                config.issue_scope_path,
                config.issue_policy_path,
                config.issue_policy_sha256,
                downstream_engine,
                code_preapproval_engine,
                engine_registry=engine_registry,
                progress_client=client,
            )
    except (WorkerError, ValueError) as exception:
        LOGGER.error("result=worker_error reason=%s", exception)
        return 1

    if args.once:
        try:
            result = run_once(queue, client, config.wait_timeout_seconds, engine)
        except (WorkerError, ValueError) as exception:
            LOGGER.error("result=worker_error reason=%s", exception)
            return 1
        return 0 if result in {
            "completed",
            "awaiting_pr_review",
            "needs_context",
            "stale",
            "no_task",
        } else 1

    LOGGER.info(
        "mode=continuous queue_key=%s wait_timeout=%ds",
        config.queue_key,
        config.wait_timeout_seconds,
    )
    try:
        while True:
            try:
                run_once(queue, client, config.wait_timeout_seconds, engine)
            except (WorkerError, ValueError) as exception:
                LOGGER.error("result=worker_error reason=%s", exception)
                time.sleep(args.error_backoff)
    except KeyboardInterrupt:
        LOGGER.info("mode=continuous stopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
