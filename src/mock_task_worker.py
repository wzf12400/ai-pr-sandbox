"""Local worker for the Java control-plane mock execution path.

The queue contains task identifiers only. Task state and the sanitized work
contract are fetched from the control plane after an atomic claim. This module
does not call GitHub, Jira, company logs, a model, or Copilot.

Default mode runs continuously: each iteration is still a bounded single-task
execution with a bounded Redis wait, so individual tasks keep their safety
bounds while the process stays alive to consume new work. Use --once for a
single bounded pass (used by tests and scripts).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import math
import os
import re
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
DEFAULT_AUTHORIZED_REPOSITORY = "wzf12400/ai-pr-sandbox"
DEFAULT_REPOSITORY_PATH = ".worker-repos/ai-pr-sandbox"


class WorkerError(RuntimeError):
    """A safe worker failure without remote response contents."""


class StaleTaskError(WorkerError):
    """The queue item was already claimed or otherwise no longer pending."""


class PublishedIssueWorkerError(WorkerError):
    """A downstream step failed after one Issue reference became canonical."""

    def __init__(self, issue_number: int, issue_url: str) -> None:
        super().__init__("post-Issue execution failed safely")
        self.issue_number = issue_number
        self.issue_url = issue_url


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

    def attach_issue(self, task_id: str, issue_number: int, issue_url: str) -> None:
        """Persist one repository-bound GitHub Issue reference."""

    def attach_pull_request(
        self,
        task_id: str,
        pr_number: int,
        pr_url: str,
        test_summary: str,
    ) -> None:
        """Persist one tested Draft PR reference."""


class ExecutionEngine(Protocol):
    def execute(self, claim: dict[str, Any]) -> "ExecutionResult":
        """Run one bounded local execution against a sanitized claim."""


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


def require_loopback_url(value: str, allowed_schemes: set[str], label: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in allowed_schemes:
        raise WorkerError(f"{label} must use one of: {', '.join(sorted(allowed_schemes))}")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise WorkerError(f"{label} must target the local machine")
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
    github_timeout_seconds: float
    code_audit_dir: Path
    code_auto_approval_enabled: bool
    code_auto_approval_policy_path: Path
    code_auto_approval_policy_sha256: str

    @classmethod
    def from_environment(cls, wait_timeout_seconds: int) -> "WorkerConfig":
        control_plane_url = require_loopback_url(
            os.getenv("CONTROL_PLANE_URL", DEFAULT_CONTROL_PLANE_URL),
            {"http"},
            "CONTROL_PLANE_URL",
        )
        redis_url = require_loopback_url(
            os.getenv("REDIS_URL", DEFAULT_REDIS_URL),
            {"redis", "rediss"},
            "REDIS_URL",
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
        if code_mode not in {"disabled", "dry_run", "execute", "publish_pr"}:
            raise WorkerError(
                "WORKER_CODE_MODE must be disabled, dry_run, execute, or publish_pr"
            )
        if code_mode != "disabled" and not issue_publication_enabled:
            raise WorkerError(
                "WORKER_CODE_MODE requires WORKER_ISSUE_PUBLICATION_ENABLED=true"
            )
        code_policy_path = Path(
            os.getenv(
                "WORKER_CODE_POLICY_PATH",
                str(repository_path / ".github" / "issue-code-policy.json"),
            )
        ).resolve()
        code_model = os.getenv("WORKER_CODE_MODEL", "").strip()
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
                str(repository_path / ".issue-code-output" / "control-plane"),
            )
        ).resolve()
        try:
            code_audit_dir.relative_to(repository_path)
        except ValueError as exception:
            raise WorkerError("WORKER_CODE_AUDIT_DIR must be inside the worker repository") from exception
        code_auto_approval_value = os.getenv(
            "WORKER_CODE_AUTO_APPROVAL_ENABLED", "false"
        ).strip().lower()
        if code_auto_approval_value not in {"true", "false"}:
            raise WorkerError(
                "WORKER_CODE_AUTO_APPROVAL_ENABLED must be true or false"
            )
        code_auto_approval_enabled = code_auto_approval_value == "true"
        if code_auto_approval_enabled and (
            not issue_publication_enabled or code_mode == "disabled"
        ):
            raise WorkerError(
                "automatic code approval requires Issue publication and a non-disabled code mode"
            )
        code_auto_approval_policy_path = Path(
            os.getenv(
                "WORKER_CODE_AUTO_APPROVAL_POLICY_PATH",
                "control-plane/config/code-execution-preapproval-policy.json",
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
            github_timeout_seconds=github_timeout_seconds,
            code_audit_dir=code_audit_dir,
            code_auto_approval_enabled=code_auto_approval_enabled,
            code_auto_approval_policy_path=code_auto_approval_policy_path,
            code_auto_approval_policy_sha256=code_auto_approval_policy_sha256,
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

    def __init__(self, authorized_repository: str, repository_path: Path) -> None:
        self._authorized_repository = authorized_repository
        self._repository_path = repository_path.resolve()

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

    def _verify_checkout(self, claimed_repository: str) -> str:
        if claimed_repository != self._authorized_repository:
            raise WorkerError("claimed repository is not authorized for this worker")
        if not self._repository_path.is_dir() or self._repository_path.is_symlink():
            raise WorkerError("authorized repository checkout is missing or unsafe")

        expected_origin = (
            f"https://github.com/{self._authorized_repository}.git".lower().rstrip("/")
        )
        actual_origin = self._git(self._repository_path, "remote", "get-url", "origin")
        if actual_origin.lower().rstrip("/") != expected_origin:
            raise WorkerError("authorized repository checkout has an unexpected origin")
        if self._git(self._repository_path, "branch", "--show-current") != "main":
            raise WorkerError("authorized repository checkout must remain on main")
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
    ) -> None:
        if mode not in {"dry_run", "execute", "publish_pr"}:
            raise ValueError("approved-Issue dispatch mode is invalid")
        self._repository_path = repository_path.resolve()
        self._policy_path = policy_path.resolve()
        self._mode = mode
        self._model = model
        self._github_timeout_seconds = github_timeout_seconds
        self._audit_dir = audit_dir.resolve()
        try:
            self._audit_dir.relative_to(self._repository_path)
        except ValueError as exception:
            raise ValueError("approved-Issue audit directory must be inside the repository") from exception

    def execute(self, claim: dict[str, Any], issue_url: str) -> ExecutionResult:
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
        execute = self._mode in {"execute", "publish_pr"}
        publish_pr = self._mode == "publish_pr"
        try:
            report = dispatch_once(
                self._repository_path,
                self._policy_path,
                _DisabledApprovedIssueCandidateClient(),
                GitHubCLIIssueSnapshotClient(self._github_timeout_seconds),
                GitHubCLIDispatchStateInspector(self._github_timeout_seconds),
                CopilotCLICodeModifier(),
                max_candidates=1,
                execute=execute,
                publish_pr=publish_pr,
                model=self._model,
                target_issue_url=issue_url,
                claimer=(
                    GitRemoteBranchClaimer(max(self._github_timeout_seconds, 120.0))
                    if execute
                    else None
                ),
            )
            audit_path = self._audit_dir / f"task-{task_id}-{self._mode}.json"
            _atomic_write(audit_path, report)
        except (FileExistsError, OSError, ValueError) as exception:
            raise WorkerError("original approved-Issue dispatcher failed closed") from exception

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
        audit_reference = audit_path.relative_to(self._repository_path).as_posix()
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


class CodeExecutionPreapprovalEngine:
    """Apply repository-owned approval labels only under reviewed policy bytes."""

    def __init__(
        self,
        issue_client: Any,
        policy_path: Path,
        confirmed_policy_sha256: str,
        issue_publication_policy_path: Path,
        issue_code_policy_path: Path,
    ) -> None:
        self._issue_client = issue_client
        self._policy_path = policy_path.resolve()
        self._confirmed_policy_sha256 = confirmed_policy_sha256
        self._issue_publication_policy_path = issue_publication_policy_path.resolve()
        self._issue_code_policy_path = issue_code_policy_path.resolve()

    def apply(
        self,
        claim: dict[str, Any],
        repository: str,
        issue_number: int,
        issue_url: str,
        publication_status: str,
    ) -> tuple[str, ...]:
        from src.code_execution_preapproval import (
            load_code_execution_preapproval_policy,
        )

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
        labels = policy.labels_for(
            str(claim.get("sourceType", "")), publication_status
        )
        if not labels:
            return ()
        try:
            applied = self._issue_client.add_labels(repository, issue_number, labels)
        except (AttributeError, ValueError) as exception:
            raise WorkerError("code approval labels could not be applied") from exception
        if tuple(applied) != labels:
            raise WorkerError("code approval label result is inconsistent")
        return labels


class NaturalLanguageIssueExecutionEngine:
    """Generate a source-profiled Issue before the approved downstream flow."""

    def __init__(
        self,
        location_engine: LocalRepositoryExecutionEngine,
        issue_client: Any,
        scope_path: Path,
        policy_path: Path,
        confirmed_policy_sha256: str,
        downstream_engine: ApprovedIssueDispatchExecutionEngine | None = None,
        code_preapproval_engine: CodeExecutionPreapprovalEngine | None = None,
    ) -> None:
        self._location_engine = location_engine
        self._issue_client = issue_client
        self._scope_path = scope_path.resolve()
        self._policy_path = policy_path.resolve()
        self._confirmed_policy_sha256 = confirmed_policy_sha256
        self._downstream_engine = downstream_engine
        self._code_preapproval_engine = code_preapproval_engine

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
        try:
            if claim.get("sourceType") == "LOG":
                evidence = self._compose_log_evidence(claim, requirement)
                input_type = "sanitized_evidence"
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
                    continue
                if generation.get("state") != "blocked":
                    break
            if generation is None:
                raise WorkerError(
                    "Issue generation failed repeatedly"
                ) from last_error
            scope = load_search_scope(self._scope_path)
            policy = load_auto_publish_policy(
                self._policy_path,
                self._confirmed_policy_sha256,
                scope,
                self._scope_path,
            )
            if policy.provider != "github_rest_api":
                raise ValueError("worker Issue publication policy must use github_rest_api")
            automation = automate_repository_issue(
                generation,
                evidence,
                scope,
                _DisabledRepositorySearchAdapter(),
                "github-tree-probe",
                policy,
                self._issue_client,
                True,
                preselected_repository=repository,
                routing_mode=ROUTING_MODE_DEMO_SINGLE_REPOSITORY,
                input_type=input_type,
            )
        except ValueError as exception:
            raise WorkerError("Issue generation or publication failed closed") from exception

        publication = automation.get("publication")
        if not isinstance(publication, dict):
            raise WorkerError("Issue automation returned an invalid publication result")
        status = publication.get("status")
        if status not in {"created", "deduplicated"}:
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
            issue_snapshot = self._issue_client.get_issue(repository, issue_number)
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
            applied_labels: tuple[str, ...] = ()
            if self._code_preapproval_engine is not None:
                applied_labels = self._code_preapproval_engine.apply(
                    claim,
                    repository,
                    issue_number,
                    issue_url,
                    str(status),
                )
            downstream = (
                self._downstream_engine.execute(claim, issue_url)
                if self._downstream_engine is not None
                else None
            )
        except (AttributeError, ValueError, WorkerError) as exception:
            raise PublishedIssueWorkerError(issue_number, issue_url) from exception
        if downstream is not None:
            return ExecutionResult(
                target_status=downstream.target_status,
                detail=(
                    f"Issue #{issue_number} 已{('创建' if status == 'created' else '去重复用')}并重新读取；"
                    + (
                        "公司预授权策略已写入代码审批标签；"
                        if applied_labels
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
        location = self._location_engine.execute({**claim, "approvedIssue": issue_snapshot})
        return ExecutionResult(
            target_status=location.target_status,
            detail=(f"Issue #{issue_number} 已{('创建' if status == 'created' else '去重复用')}并重新读取；"
                    + location.detail)[:1000],
            candidate_count=location.candidate_count,
            issue_number=issue_number,
            issue_url=issue_url,
        )

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

    def attach_issue(self, task_id: str, issue_number: int, issue_url: str) -> None:
        try:
            self._post_json(
                f"/api/internal/tasks/{task_id}/issue",
                {"issueNumber": issue_number, "issueUrl": issue_url},
            )
        except (HTTPError, URLError, TimeoutError) as exception:
            raise WorkerError("control plane rejected the GitHub Issue reference") from exception

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
        if source_type not in {"NATURAL_LANGUAGE", "LOG"}:
            raise WorkerError("this worker accepts NATURAL_LANGUAGE and LOG tasks only")
        expected_profile = (
            "LOG_INCIDENT" if source_type == "LOG" else "NATURAL_LANGUAGE"
        )
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
            client.transition(
                task_id,
                "FAILED",
                "Issue 已记录，但后续审批标签或原 CLI 执行安全失败；未合并、未部署",
            )
            transition_recorded = True
        except WorkerError:
            pass
        LOGGER.error("task_id=%s result=post_issue_failed", task_id)
        return "failed" if transition_recorded else "retry"
    except WorkerError:
        transition_recorded = False
        try:
            client.transition(
                task_id,
                "FAILED",
                "mock worker failed safely; no external systems were called",
            )
            transition_recorded = True
        except WorkerError:
            pass
        LOGGER.error("task_id=%s result=failed", task_id)
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
        location_engine = LocalRepositoryExecutionEngine(
            config.authorized_repository,
            config.repository_path,
        )
        engine: ExecutionEngine = location_engine
        if config.issue_publication_enabled:
            from src.repository_issue_automation import GitHubRESTIssueClient

            issue_client = GitHubRESTIssueClient.from_environment(
                config.request_timeout_seconds
            )
            downstream_engine = (
                ApprovedIssueDispatchExecutionEngine(
                    config.repository_path,
                    config.code_policy_path,
                    config.code_mode,
                    config.code_model,
                    config.github_timeout_seconds,
                    config.code_audit_dir,
                )
                if config.code_mode != "disabled"
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
