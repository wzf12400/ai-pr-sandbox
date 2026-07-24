"""Core state and execution workflow for the terminal AI change agent."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src import ai_issue_generator
from src.approved_issue_dispatcher import (
    GitHubCLIApprovedIssueClient,
    GitHubCLIDispatchStateInspector,
    GitRemoteBranchClaimer,
    dispatch_once,
)
from src.copilot_code_modifier import (
    CopilotCLICodeModifier,
    GitHubCLIIssueSnapshotClient,
    REPOSITORY_PATTERN,
    _cleanup_empty_work_branch,
    _run_process,
    load_issue_code_policy,
)
from src.copilot_issue_provider import CopilotCLIIssueProvider
from src.issue_draft import _atomic_write_json, _atomic_write_text
from src.issue_entry import compose_evidence
from src.natural_language_issue_automation import (
    GitHubCLICodeSearchAdapter,
    GitHubCLIIssueClient,
)
from src.repository_issue_automation import (
    automate_repository_issue,
    load_auto_publish_policy,
    render_automated_issue_body,
)
from src.repository_resolver import load_search_scope


CONFIG_SCHEMA_VERSION = "local-ai-agent-config/v1"
RUN_SCHEMA_VERSION = "local-ai-agent-run/v1"
DEFAULT_CONFIG_PATH = Path(".issue-entry-state/control-center.json")
DEFAULT_RUNS_PATH = Path(".issue-entry-output/control-center")
MAX_REQUEST_BYTES = 64_000
MAX_DESCRIPTION_CHARS = 4_000
LOGIN_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
RUN_ID_PATTERN = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}")
MAX_RESUME_ATTEMPTS = 3
RESUMABLE_PRE_MODIFIER_FAILURES = frozenset(
    {
        "modifier_execution_failed",
        "modifier_localization_safety_blocked",
        "copilot_cli_preflight_failed",
    }
)
SAFE_ERROR_MESSAGES = {
    "configuration_changed": "配置在审批前发生变化，请重新生成计划。",
    "input_safety_blocked": (
        "需求中包含疑似密钥或不可识别的长随机字符串。"
        "请删除该片段，或用“[已脱敏]”替换后重试。"
    ),
    "generation_blocked": (
        "需求信息不足，或 Issue 草案未通过安全复核。"
        "请补充目标功能、预期行为和验收标准后重试。"
    ),
    "issue_draft_validation_failed": "Issue 草案结构或证据映射未通过本地校验。",
    "issue_review_rejected": "Issue 草案未通过独立安全复核。",
    "repository_not_resolved": "无法唯一定位目标仓库，请补充更明确的代码线索。",
    "issue_publication_failed": "GitHub Issue 发布失败。",
    "approval_label_failed": "代码审批标签写入失败。",
    "code_dispatch_failed": "代码修改流程被安全门禁阻止。",
    "modifier_localization_safety_blocked": (
        "Issue 的任务正文未通过本地代码定位安全检查。"
    ),
    "copilot_cli_preflight_failed": "GitHub Copilot CLI 可用性检查失败。",
    "resume_dispatch_failed": "保留 claim 的代码恢复流程被安全门禁阻止。",
    "unexpected_failure": "流程未完成；详细原因已限制在本地审计记录中。",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _generation_failure_code(generation: Mapping[str, Any]) -> str:
    validation = generation.get("validation", {})
    errors = validation.get("errors", []) if isinstance(validation, dict) else []
    if isinstance(errors, list) and errors:
        return "issue_draft_validation_failed"
    review = generation.get("review", {})
    if isinstance(review, dict) and review.get("verdict") == "reject":
        return "issue_review_rejected"
    return "generation_blocked"


def _is_resumable_empty_modification(
    dispatch_state: Mapping[str, Any],
) -> bool:
    modifier = dispatch_state.get("modifier_report")
    if not isinstance(modifier, dict):
        return False
    modification = modifier.get("modification", {})
    changes = modifier.get("changes", {})
    tests = modifier.get("tests", [])
    publication = modifier.get("publication", {})
    return bool(
        dispatch_state.get("failure_reason") == "modifier_execution_blocked"
        and modifier.get("status") == "blocked"
        and isinstance(modification, dict)
        and modification.get("status") == "completed"
        and modification.get("returncode") == 0
        and isinstance(changes, dict)
        and changes.get("valid") is False
        and changes.get("paths") == []
        and changes.get("added_lines") == 0
        and changes.get("deleted_lines") == 0
        and changes.get("diff_sha256") == hashlib.sha256(b"").hexdigest()
        and changes.get("reasons") == ["Copilot produced no file changes"]
        and tests == []
        and isinstance(publication, dict)
        and not publication.get("draft_pr_url")
    )


def _atomic_replace_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("local configuration path must not be a symbolic link")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _safe_process(args: Sequence[str], cwd: Path, timeout: float = 15.0) -> Dict[str, Any]:
    try:
        completed = subprocess.run(
            list(args),
            cwd=str(cwd),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"ok": False, "stdout": ""}
    return {
        "ok": completed.returncode == 0,
        "stdout": completed.stdout.strip()[:2_000],
    }


@dataclass(frozen=True)
class ManagedRepository:
    repository: str
    local_path: str
    enabled: bool
    policy_id: str
    policy_sha256: str
    base_branch: str
    allowed_models: Tuple[str, ...]
    default_model: str
    required_labels: Tuple[str, ...]
    allowed_write_paths: Tuple[str, ...]

    def public_dict(self) -> Dict[str, Any]:
        return {
            "repository": self.repository,
            "local_path": self.local_path,
            "enabled": self.enabled,
            "policy": {
                "policy_id": self.policy_id,
                "sha256": self.policy_sha256,
                "base_branch": self.base_branch,
                "allowed_models": list(self.allowed_models),
                "default_model": self.default_model,
                "required_labels": list(self.required_labels),
                "allowed_write_paths": list(self.allowed_write_paths),
            },
        }


@dataclass(frozen=True)
class ControlCenterConfig:
    github_login: str
    copilot_model: str
    repositories: Tuple[ManagedRepository, ...]
    sha256: str

    @property
    def enabled_repositories(self) -> Tuple[ManagedRepository, ...]:
        return tuple(item for item in self.repositories if item.enabled)

    def public_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "github": {"login": self.github_login},
            "copilot": {"model": self.copilot_model},
            "repositories": [item.public_dict() for item in self.repositories],
            "sha256": self.sha256,
        }


def _compose_managed_evidence(
    description: str,
    config: ControlCenterConfig,
) -> Dict[str, Any]:
    """Trust only exact repository identifiers already validated by local config."""
    normalized = description
    mentioned: List[str] = []
    for repository in config.enabled_repositories:
        matched = False
        for configured_reference in (
            repository.repository,
            repository.local_path,
        ):
            if configured_reference and configured_reference in normalized:
                normalized = normalized.replace(
                    configured_reference,
                    "已配置目标仓库",
                )
                matched = True
        if matched:
            mentioned.append(repository.repository)
    if len(mentioned) > 1:
        raise ValueError("description references more than one configured repository")
    evidence = compose_evidence(normalized)
    evidence.setdefault("facts", {})["requested_change"] = evidence["facts"][
        "reported_description"
    ]
    if mentioned:
        target_repository = mentioned[0]
    elif len(config.enabled_repositories) == 1:
        target_repository = config.enabled_repositories[0].repository
    else:
        target_repository = ""
    if target_repository:
        evidence.setdefault("facts", {})["repository"] = target_repository
    return evidence


class LocalConfigStore:
    """Persist only non-secret local preferences and checked-out repository paths."""

    def __init__(self, path: Path):
        self.path = path

    def _repository(self, value: Mapping[str, Any]) -> ManagedRepository:
        repository = str(value.get("repository", "")).strip()
        raw_path = str(value.get("local_path", "")).strip()
        enabled = value.get("enabled")
        if not REPOSITORY_PATTERN.fullmatch(repository):
            raise ValueError("repository must use OWNER/REPOSITORY")
        if enabled not in {True, False}:
            raise ValueError("repository enabled must be boolean")
        path = Path(raw_path)
        if not path.is_absolute() or path.is_symlink() or not path.is_dir():
            raise ValueError("repository local_path must be an existing absolute directory")
        resolved = path.resolve(strict=True)
        top = _safe_process(["git", "rev-parse", "--show-toplevel"], resolved)
        if not top["ok"] or Path(top["stdout"]).resolve() != resolved:
            raise ValueError("repository local_path must be the Git repository root")
        policy = load_issue_code_policy(resolved / ".github" / "issue-code-policy.json")
        if policy.repository.casefold() != repository.casefold():
            raise ValueError("repository does not match its tracked Issue code policy")
        origin = _safe_process(["git", "remote", "get-url", "origin"], resolved)
        expected_suffix = f"{repository}.git"
        if not origin["ok"] or not (
            origin["stdout"].rstrip("/").endswith(expected_suffix)
            or origin["stdout"].rstrip("/").endswith(repository)
        ):
            raise ValueError("repository does not match its origin remote")
        return ManagedRepository(
            repository=policy.repository,
            local_path=str(resolved),
            enabled=bool(enabled),
            policy_id=policy.policy_id,
            policy_sha256=policy.sha256,
            base_branch=policy.base_branch,
            allowed_models=policy.allowed_models,
            default_model=policy.default_model,
            required_labels=policy.required_labels,
            allowed_write_paths=policy.allowed_write_paths,
        )

    def parse(self, payload: Mapping[str, Any]) -> ControlCenterConfig:
        github = payload.get("github")
        copilot = payload.get("copilot")
        raw_repositories = payload.get("repositories")
        if not isinstance(github, dict) or not isinstance(copilot, dict):
            raise ValueError("GitHub and Copilot configuration are required")
        login = str(github.get("login", "")).strip()
        model = str(copilot.get("model", "")).strip()
        if not LOGIN_PATTERN.fullmatch(login):
            raise ValueError("GitHub login is invalid")
        if not isinstance(raw_repositories, list) or not raw_repositories:
            raise ValueError("at least one repository is required")
        repositories = tuple(
            self._repository(item)
            for item in raw_repositories
            if isinstance(item, dict)
        )
        if len(repositories) != len(raw_repositories):
            raise ValueError("repository configuration contains an invalid item")
        names = [item.repository.casefold() for item in repositories]
        paths = [item.local_path for item in repositories]
        if len(names) != len(set(names)) or len(paths) != len(set(paths)):
            raise ValueError("repository configuration contains duplicates")
        enabled = [item for item in repositories if item.enabled]
        if not enabled:
            raise ValueError("at least one repository must be enabled")
        common_models = set(enabled[0].allowed_models)
        for item in enabled[1:]:
            common_models.intersection_update(item.allowed_models)
        if not model:
            default_model = enabled[0].default_model
            model = (
                default_model
                if default_model in common_models
                else sorted(common_models)[0]
            )
        if model not in common_models:
            raise ValueError("selected Copilot model is not allowed by every enabled repository")
        normalized = {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "github": {"login": login},
            "copilot": {"model": model},
            "repositories": [
                {
                    "repository": item.repository,
                    "local_path": item.local_path,
                    "enabled": item.enabled,
                    "policy_sha256": item.policy_sha256,
                }
                for item in repositories
            ],
        }
        return ControlCenterConfig(
            github_login=login,
            copilot_model=model,
            repositories=repositories,
            sha256=_sha256(normalized),
        )

    def load(self) -> Optional[ControlCenterConfig]:
        if not self.path.exists():
            return None
        if self.path.is_symlink() or self.path.stat().st_size > MAX_REQUEST_BYTES:
            raise ValueError("local configuration file is invalid")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("local configuration file is unreadable") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != CONFIG_SCHEMA_VERSION:
            raise ValueError("local configuration schema is invalid")
        return self.parse(payload)

    def save(self, payload: Mapping[str, Any]) -> ControlCenterConfig:
        config = self.parse(payload)
        identity = inspect_identity(Path.cwd())
        actual_login = identity["github"].get("login")
        known_accounts = {
            item.get("login")
            for item in identity["github"].get("accounts", [])
            if isinstance(item, dict)
        }
        if actual_login != config.github_login and config.github_login in known_accounts:
            switched = _safe_process(
                [
                    "gh",
                    "auth",
                    "switch",
                    "--hostname",
                    "github.com",
                    "--user",
                    config.github_login,
                ],
                Path.cwd(),
            )
            if switched["ok"]:
                identity = inspect_identity(Path.cwd())
                actual_login = identity["github"].get("login")
        if actual_login != config.github_login:
            raise ValueError("configured GitHub account is not the active gh account")
        persisted = {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "github": {"login": config.github_login},
            "copilot": {"model": config.copilot_model},
            "repositories": [
                {
                    "repository": item.repository,
                    "local_path": item.local_path,
                    "enabled": item.enabled,
                }
                for item in config.repositories
            ],
        }
        _atomic_replace_json(self.path, persisted)
        return config


def inspect_identity(cwd: Path) -> Dict[str, Any]:
    accounts_result = _safe_process(
        [
            "gh",
            "auth",
            "status",
            "--json",
            "hosts",
            "--jq",
            '.hosts["github.com"]',
        ],
        cwd,
    )
    accounts: List[Dict[str, Any]] = []
    if accounts_result["ok"]:
        try:
            raw_accounts = json.loads(accounts_result["stdout"])
        except json.JSONDecodeError:
            raw_accounts = []
        if isinstance(raw_accounts, list):
            for item in raw_accounts:
                if not isinstance(item, dict):
                    continue
                candidate = str(item.get("login", "")).strip()
                if LOGIN_PATTERN.fullmatch(candidate):
                    accounts.append(
                        {
                            "login": candidate,
                            "active": item.get("active") is True,
                        }
                    )
    gh = _safe_process(["gh", "api", "user", "--jq", ".login"], cwd)
    login = gh["stdout"] if gh["ok"] and LOGIN_PATTERN.fullmatch(gh["stdout"]) else None
    if login and login not in {item["login"] for item in accounts}:
        accounts.append({"login": login, "active": True})
    copilot = _safe_process(["copilot", "--version"], cwd)
    return {
        "github": {
            "available": bool(_safe_process(["gh", "--version"], cwd)["ok"]),
            "authenticated": login is not None,
            "login": login,
            "accounts": accounts,
        },
        "copilot": {
            "available": copilot["ok"],
            "version": copilot["stdout"].splitlines()[0] if copilot["ok"] else None,
            "authentication": "current_local_user",
        },
    }


class GitHubCLIApprovalClient:
    def __init__(self, timeout_seconds: float = 30.0):
        self.timeout_seconds = timeout_seconds

    def ensure_and_apply(
        self, repo_path: Path, repository: str, issue_url: str, labels: Sequence[str]
    ) -> None:
        listed = _run_process(
            [
                "gh",
                "label",
                "list",
                "--repo",
                repository,
                "--limit",
                "100",
                "--json",
                "name",
            ],
            repo_path,
            self.timeout_seconds,
        )
        if listed.returncode != 0:
            raise ValueError("approval labels could not be inspected")
        try:
            existing = {
                item.get("name")
                for item in json.loads(listed.stdout)
                if isinstance(item, dict)
            }
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("approval label listing was invalid") from exc
        for label in labels:
            if label not in existing:
                created = _run_process(
                    [
                        "gh",
                        "label",
                        "create",
                        label,
                        "--repo",
                        repository,
                        "--color",
                        "0E8A16",
                        "--description",
                        "Human approval for guarded AI code modification",
                    ],
                    repo_path,
                    self.timeout_seconds,
                )
                if created.returncode != 0:
                    raise ValueError("required approval label could not be created")
            applied = _run_process(
                [
                    "gh",
                    "issue",
                    "edit",
                    issue_url,
                    "--repo",
                    repository,
                    "--add-label",
                    label,
                ],
                repo_path,
                self.timeout_seconds,
            )
            if applied.returncode != 0:
                raise ValueError("required approval label could not be applied")


class ControlCenterWorkflow:
    """Persist safe previews and bounded, explicitly approved recovery attempts."""

    def __init__(
        self,
        config_store: LocalConfigStore,
        runs_path: Path,
        *,
        approval_client: Optional[GitHubCLIApprovalClient] = None,
        issue_provider_factory: Optional[Any] = None,
    ):
        self.config_store = config_store
        self.runs_path = runs_path
        self.approval_client = approval_client or GitHubCLIApprovalClient()
        self.issue_provider_factory = (
            issue_provider_factory
            or (lambda model: CopilotCLIIssueProvider(model))
        )
        self._locks: Dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _run_dir(self, run_id: str) -> Path:
        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError("run id is invalid")
        return self.runs_path / run_id

    def _record_path(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "run.json"

    def _lock(self, run_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(run_id, threading.Lock())

    def _write_record(self, run_id: str, record: Mapping[str, Any]) -> None:
        _atomic_replace_json(self._record_path(run_id), record)

    def _write_failure_audit(
        self,
        run_id: str,
        *,
        stage: str,
        code: str,
        reason_category: str,
    ) -> None:
        """Record only a safe reason category, never the rejected input."""
        _atomic_replace_json(
            self._run_dir(run_id) / "failure-audit.json",
            {
                "schema_version": "local-ai-agent-failure-audit/v1",
                "run_id": run_id,
                "recorded_at": _utc_now(),
                "stage": stage,
                "code": code,
                "reason_category": reason_category,
                "raw_input_recorded": False,
            },
        )

    def read(self, run_id: str) -> Dict[str, Any]:
        path = self._record_path(run_id)
        if not path.exists() or path.is_symlink() or path.stat().st_size > MAX_REQUEST_BYTES:
            raise ValueError("run does not exist")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != RUN_SCHEMA_VERSION:
            raise ValueError("run record is invalid")
        return payload

    def create(self, description: str) -> Dict[str, Any]:
        description = description.strip()
        if not description or len(description) > MAX_DESCRIPTION_CHARS:
            raise ValueError("description must contain between 1 and 4000 characters")
        digest = hashlib.sha256(description.encode("utf-8")).hexdigest()
        return self._create_run(description, "natural_language", digest)

    def create_from_evidence(self, evidence: Mapping[str, Any]) -> Dict[str, Any]:
        compact = ai_issue_generator.compact_evidence(dict(evidence))
        digest = hashlib.sha256(_canonical_json(compact)).hexdigest()
        return self._create_run(compact, "sanitized_evidence", digest)

    def _create_run(
        self,
        input_payload: Any,
        input_type: str,
        input_sha256: str,
    ) -> Dict[str, Any]:
        config = self.config_store.load()
        if config is None:
            raise ValueError("configuration must be saved before starting")
        run_id = (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "-"
            + secrets.token_hex(4)
        )
        run_dir = self._run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=False)
        record = {
            "schema_version": RUN_SCHEMA_VERSION,
            "run_id": run_id,
            "status": "preparing",
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "config_sha256": config.sha256,
            "input_type": input_type,
            "input_sha256": input_sha256,
            "preview": None,
            "result": {"issue_url": None, "draft_pr_url": None},
            "failure": None,
        }
        self._write_record(run_id, record)
        threading.Thread(
            target=self._prepare,
            args=(run_id, input_payload, input_type),
            name=f"ai-agent-prepare-{run_id}",
            daemon=True,
        ).start()
        return record

    def _generated_policy(
        self, run_dir: Path, config: ControlCenterConfig
    ) -> Tuple[Path, Path, str]:
        scope_id = f"control-center-{config.sha256[:12]}"
        scope = {
            "schema_version": "repository-search-scope/v1",
            "scope_id": scope_id,
            "provider": "github",
            "repositories": [
                {
                    "repository": item.repository,
                    "enabled": item.enabled,
                    "default_branch": item.base_branch,
                    "labels": ["control-center"],
                }
                for item in config.repositories
            ],
            "limits": {
                "max_queries": 12,
                "max_candidate_repositories": min(10, len(config.repositories)),
                "max_hits_per_query": 20,
            },
        }
        scope_path = run_dir / "routing-scope.json"
        _atomic_write_json(scope_path, scope)
        scope_sha = hashlib.sha256(scope_path.read_bytes()).hexdigest()
        policy = {
            "schema_version": "repository-auto-publish-policy/v1",
            "policy_id": f"control-center-{config.sha256[:12]}",
            "scope_id": scope_id,
            "scope_sha256": scope_sha,
            "provider": "github_cli",
            "max_issues_per_run": 1,
            "allowed_generation_states": [
                "ready_for_human_review",
                "needs_human_context",
            ],
            "allowed_adapters": ["github-code-search"],
        }
        policy_path = run_dir / "publication-policy.json"
        _atomic_write_json(policy_path, policy)
        return scope_path, policy_path, hashlib.sha256(policy_path.read_bytes()).hexdigest()

    def _prepare(
        self,
        run_id: str,
        input_payload: Any,
        input_type: str = "natural_language",
    ) -> None:
        with self._lock(run_id):
            record = self.read(run_id)
            try:
                config = self.config_store.load()
                if config is None or config.sha256 != record["config_sha256"]:
                    raise RuntimeError("configuration_changed")
                run_dir = self._run_dir(run_id)
                try:
                    if input_type == "natural_language":
                        evidence = _compose_managed_evidence(
                            str(input_payload),
                            config,
                        )
                    elif input_type == "sanitized_evidence":
                        if not isinstance(input_payload, dict):
                            raise ValueError("sanitized evidence must be one object")
                        evidence = ai_issue_generator.compact_evidence(input_payload)
                        if len(config.enabled_repositories) == 1:
                            evidence = dict(evidence)
                            evidence["facts"] = dict(evidence.get("facts", {}))
                            evidence["facts"]["repository"] = (
                                config.enabled_repositories[0].repository
                            )
                    else:
                        raise ValueError("unsupported workflow input type")
                except ValueError as exc:
                    if str(exc) == "description contains unclassified high-entropy data":
                        self._write_failure_audit(
                            run_id,
                            stage="input_sanitization",
                            code="input_safety_blocked",
                            reason_category="unclassified_high_entropy",
                        )
                        raise RuntimeError("input_safety_blocked") from exc
                    raise
                _atomic_write_json(run_dir / "evidence.json", evidence)
                provider = self.issue_provider_factory(config.copilot_model)
                generation = ai_issue_generator.generate_issue(
                    evidence,
                    provider,
                    provider,
                )
                ai_issue_generator.write_result(
                    generation,
                    run_dir / "generation.json",
                    run_dir / "issue-draft.md",
                )
                if generation.get("state") == "blocked":
                    raise RuntimeError(_generation_failure_code(generation))
                scope_path, policy_path, policy_sha = self._generated_policy(
                    run_dir, config
                )
                scope = load_search_scope(scope_path)
                policy = load_auto_publish_policy(
                    policy_path, policy_sha, scope, scope_path
                )
                automation = automate_repository_issue(
                    generation,
                    evidence,
                    scope,
                    GitHubCLICodeSearchAdapter(30.0),
                    "github-code-search",
                    policy,
                    GitHubCLIIssueClient(30.0),
                    False,
                    preselected_repository=(
                        config.enabled_repositories[0].repository
                        if len(config.enabled_repositories) == 1
                        else ""
                    ),
                )
                _atomic_write_json(run_dir / "automation-preview.json", automation)
                publication = automation.get("publication", {})
                publication_status = publication.get("status")
                if publication_status not in {
                    "approved_not_published",
                    "deduplicated",
                }:
                    raise RuntimeError("repository_not_resolved")
                repository = publication["repository"]
                managed = next(
                    item
                    for item in config.enabled_repositories
                    if item.repository.casefold() == repository.casefold()
                )
                body, fingerprint = render_automated_issue_body(
                    generation, repository, policy
                )
                _atomic_write_text(run_dir / "publish-body.md", body)
                draft = generation.get("draft", {})
                issue_mode = (
                    "reuse_existing"
                    if publication_status == "deduplicated"
                    else "create"
                )
                preview = {
                    "title": str(draft.get("title", "")),
                    "repository": repository,
                    "body": body,
                    "fingerprint": fingerprint,
                    "issue_mode": issue_mode,
                    "existing_issue_url": (
                        str(publication.get("issue_url", ""))
                        if issue_mode == "reuse_existing"
                        else None
                    ),
                    "copilot_model": config.copilot_model,
                    "required_labels": list(managed.required_labels),
                    "allowed_write_paths": list(managed.allowed_write_paths),
                    "actions": [
                        (
                            "reuse_existing_github_issue"
                            if issue_mode == "reuse_existing"
                            else "publish_github_issue"
                        ),
                        "apply_code_approval_labels",
                        "claim_issue_snapshot",
                        "run_copilot",
                        "validate_changes",
                        "run_policy_tests",
                        "create_draft_pr",
                    ],
                    "auto_merge": False,
                    "deploy": False,
                }
                preview["approval_digest"] = _sha256(preview)
                record.update(
                    {
                        "status": "awaiting_approval",
                        "updated_at": _utc_now(),
                        "preview": preview,
                        "failure": None,
                    }
                )
            except RuntimeError as exc:
                code = str(exc)
                record.update(
                    {
                        "status": "blocked",
                        "updated_at": _utc_now(),
                        "failure": {
                            "code": code,
                            "message": SAFE_ERROR_MESSAGES.get(
                                code, SAFE_ERROR_MESSAGES["unexpected_failure"]
                            ),
                        },
                    }
                )
            except (OSError, ValueError, StopIteration):
                record.update(
                    {
                        "status": "blocked",
                        "updated_at": _utc_now(),
                        "failure": {
                            "code": "unexpected_failure",
                            "message": SAFE_ERROR_MESSAGES["unexpected_failure"],
                        },
                    }
                )
            self._write_record(run_id, record)

    def approve(self, run_id: str, approval_digest: str) -> Dict[str, Any]:
        with self._lock(run_id):
            record = self.read(run_id)
            if record.get("status") != "awaiting_approval":
                raise ValueError("run is not awaiting approval")
            expected = str(record.get("preview", {}).get("approval_digest", ""))
            if not secrets.compare_digest(expected, approval_digest):
                raise ValueError("approval digest does not match the displayed plan")
            record.update(
                {
                    "status": "executing",
                    "updated_at": _utc_now(),
                    "approval": {
                        "approved_at": _utc_now(),
                        "approval_digest": expected,
                        "combined_scope": [
                            "issue_publication",
                            "code_modification",
                            "draft_pr_publication",
                        ],
                    },
                }
            )
            self._write_record(run_id, record)
        threading.Thread(
            target=self._execute,
            args=(run_id,),
            name=f"ai-agent-execute-{run_id}",
            daemon=True,
        ).start()
        return record

    def prepare_resume(self, run_id: str) -> Dict[str, Any]:
        """Prepare a bounded, explicit retry from an exact retained claim."""
        with self._lock(run_id):
            record = self.read(run_id)
            if (
                record.get("status") == "awaiting_resume_approval"
                and isinstance(record.get("resume_preview"), dict)
            ):
                return record
            if (
                record.get("status") != "blocked"
                or record.get("failure", {}).get("code")
                not in (
                    {
                        "code_dispatch_failed",
                        "resume_dispatch_failed",
                    }
                    | RESUMABLE_PRE_MODIFIER_FAILURES
                )
            ):
                raise ValueError("该运行不符合保留 claim 的恢复条件。")
            run_dir = self._run_dir(run_id)
            resume_paths: Dict[int, Path] = {}
            for path in run_dir.iterdir():
                if path.name == "dispatch-resume.json":
                    resume_paths[1] = path
                    continue
                match = re.fullmatch(r"dispatch-resume-([2-9][0-9]*)\.json", path.name)
                if match:
                    resume_paths[int(match.group(1))] = path
            completed_attempts = sorted(resume_paths)
            if completed_attempts != list(range(1, len(completed_attempts) + 1)):
                raise ValueError("恢复审计序列不完整，不能继续。")
            if len(completed_attempts) >= MAX_RESUME_ATTEMPTS:
                raise ValueError("该运行已达到安全恢复次数上限。")
            if bool(completed_attempts) != (
                record.get("failure", {}).get("code") == "resume_dispatch_failed"
            ):
                raise ValueError("运行状态与恢复审计不一致。")
            resume_attempt = len(completed_attempts) + 1
            config = self.config_store.load()
            if config is None or config.sha256 != record.get("config_sha256"):
                raise ValueError("配置已变化，不能恢复原运行。")
            dispatch_path = (
                resume_paths[completed_attempts[-1]]
                if completed_attempts
                else run_dir / "dispatch.json"
            )
            if (
                not dispatch_path.exists()
                or dispatch_path.is_symlink()
                or dispatch_path.stat().st_size > MAX_REQUEST_BYTES
            ):
                raise ValueError("原调度审计不可用于恢复。")
            dispatch = json.loads(dispatch_path.read_text(encoding="utf-8"))
            candidates = dispatch.get("candidates", [])
            dispatch_state = dispatch.get("dispatch", {})
            claim = dispatch_state.get("claim", {})
            pre_modifier_failure = bool(
                dispatch_state.get("failure_reason")
                in RESUMABLE_PRE_MODIFIER_FAILURES
                and dispatch_state.get("modifier_report") is None
            )
            empty_modification = _is_resumable_empty_modification(
                dispatch_state
            )
            if (
                dispatch.get("status") != "blocked"
                or not (pre_modifier_failure or empty_modification)
                or not isinstance(candidates, list)
                or len(candidates) != 1
                or not candidates[0].get("approved")
                or not claim.get("claimed")
                or not claim.get("retained_on_failure")
            ):
                raise ValueError("原调度结果不允许自动恢复。")
            candidate = candidates[0]
            idempotency = candidate.get("idempotency", {})
            issue_url = str(candidate.get("url", ""))
            snapshot_sha256 = str(candidate.get("snapshot_sha256", ""))
            claim_branch = str(candidate.get("claim_branch", ""))
            work_branch = str(candidate.get("work_branch", ""))
            claim_commit = str(claim.get("remote_commit", ""))
            modifier_report = dispatch_state.get("modifier_report") or {}
            modifier_source = modifier_report.get("source", {})
            modifier_repository = modifier_report.get("repository", {})
            if (
                issue_url != record.get("result", {}).get("issue_url")
                or issue_url != dispatch_state.get("issue_url")
                or not re.fullmatch(r"[0-9a-f]{64}", snapshot_sha256)
                or not re.fullmatch(r"[0-9a-f]{40}", claim_commit)
                or claim.get("claim_commit") != claim_commit
                or claim_branch != claim.get("claim_branch")
                or idempotency.get("local_work_branch_exists")
                or idempotency.get("remote_work_branch_exists")
                or idempotency.get("existing_pr_url")
            ):
                raise ValueError("原 Issue、claim 或代码分支状态不允许恢复。")
            base_commit = str(dispatch.get("repository", {}).get("base_commit", ""))
            repository_name = str(
                dispatch.get("repository", {}).get("name", "")
            )
            empty_branch_already_removed = bool(
                empty_modification
                and modifier_report.get("modification", {}).get(
                    "work_branch_removed"
                )
                is True
            )
            remove_empty_work_branch = bool(
                empty_modification and not empty_branch_already_removed
            )
            if (
                not repository_name
                or (
                    empty_modification
                    and (
                        not re.fullmatch(r"[0-9a-f]{40}", base_commit)
                        or not work_branch
                        or modifier_source.get("url") != issue_url
                        or modifier_source.get("snapshot_sha256")
                        != snapshot_sha256
                        or modifier_source.get("repository")
                        != repository_name
                        or modifier_repository.get("repository")
                        != repository_name
                        or modifier_repository.get("base_commit")
                        != base_commit
                        or modifier_repository.get("work_branch")
                        != work_branch
                    )
                )
            ):
                raise ValueError("原 Issue、claim 或代码分支状态不允许恢复。")
            if empty_modification:
                managed = next(
                    (
                        item
                        for item in config.enabled_repositories
                        if item.repository.casefold()
                        == repository_name.casefold()
                    ),
                    None,
                )
                if managed is None:
                    raise ValueError("空代码分支状态已变化，不能自动恢复。")
                repo_path = Path(managed.local_path)
                branch_result = _run_process(
                    ["git", "branch", "--show-current"], repo_path, 30
                )
                status_result = _run_process(
                    ["git", "status", "--porcelain", "--untracked-files=all"],
                    repo_path,
                    30,
                )
                head_result = _run_process(
                    ["git", "rev-parse", "HEAD"], repo_path, 30
                )
                expected_branch = (
                    str(dispatch.get("repository", {}).get("base_branch", ""))
                    if empty_branch_already_removed
                    else work_branch
                )
                work_branch_result = _run_process(
                    [
                        "git",
                        "show-ref",
                        "--verify",
                        "--quiet",
                        f"refs/heads/{work_branch}",
                    ],
                    repo_path,
                    30,
                )
                branch_presence_valid = (
                    work_branch_result.returncode == 1
                    if empty_branch_already_removed
                    else work_branch_result.returncode == 0
                )
                if (
                    branch_result.returncode != 0
                    or status_result.returncode != 0
                    or head_result.returncode != 0
                    or branch_result.stdout.strip() != expected_branch
                    or status_result.stdout.strip()
                    or head_result.stdout.strip() != base_commit
                    or not branch_presence_valid
                ):
                    raise ValueError("空代码分支状态已变化，不能自动恢复。")
            preview = {
                "resume_attempt": resume_attempt,
                "source_audit": dispatch_path.name,
                "resume_output": (
                    "dispatch-resume.json"
                    if resume_attempt == 1
                    else f"dispatch-resume-{resume_attempt}.json"
                ),
                "issue_url": issue_url,
                "snapshot_sha256": snapshot_sha256,
                "repository": repository_name,
                "claim_branch": claim_branch,
                "claim_commit": claim_commit,
                "work_branch": work_branch,
                "base_commit": base_commit,
                "remove_empty_work_branch": remove_empty_work_branch,
                "copilot_model": config.copilot_model,
                "actions": [
                    *(
                        ["remove_empty_local_work_branch"]
                        if remove_empty_work_branch
                        else []
                    ),
                    "verify_retained_claim",
                    "verify_issue_snapshot",
                    "run_copilot",
                    "validate_changes",
                    "run_policy_tests",
                    "create_draft_pr",
                ],
                "auto_merge": False,
                "deploy": False,
            }
            preview["approval_digest"] = _sha256(preview)
            record.update(
                {
                    "status": "awaiting_resume_approval",
                    "updated_at": _utc_now(),
                    "resume_preview": preview,
                }
            )
            self._write_record(run_id, record)
            return record

    def cancel_resume(self, run_id: str) -> Dict[str, Any]:
        with self._lock(run_id):
            record = self.read(run_id)
            if record.get("status") != "awaiting_resume_approval":
                raise ValueError("run is not awaiting resume approval")
            record.update({"status": "blocked", "updated_at": _utc_now()})
            self._write_record(run_id, record)
            return record

    def approve_resume(self, run_id: str, approval_digest: str) -> Dict[str, Any]:
        with self._lock(run_id):
            record = self.read(run_id)
            if record.get("status") != "awaiting_resume_approval":
                raise ValueError("run is not awaiting resume approval")
            expected = str(
                record.get("resume_preview", {}).get("approval_digest", "")
            )
            if not secrets.compare_digest(expected, approval_digest):
                raise ValueError("approval digest does not match the displayed plan")
            record.update(
                {
                    "status": "executing",
                    "updated_at": _utc_now(),
                    "resume_approval": {
                        "approved_at": _utc_now(),
                        "approval_digest": expected,
                        "scope": [
                            "reuse_retained_claim",
                            "code_modification",
                            "draft_pr_publication",
                        ],
                    },
                }
            )
            self._write_record(run_id, record)
        threading.Thread(
            target=self._resume_execute,
            args=(run_id,),
            name=f"ai-agent-resume-{run_id}",
            daemon=True,
        ).start()
        return record

    def _resume_execute(self, run_id: str) -> None:
        with self._lock(run_id):
            record = self.read(run_id)
            preview = record.get("resume_preview", {})
            issue_url = str(preview.get("issue_url", ""))
            try:
                config = self.config_store.load()
                if config is None or config.sha256 != record["config_sha256"]:
                    raise RuntimeError("configuration_changed")
                repository = str(preview.get("repository", ""))
                managed = next(
                    item
                    for item in config.enabled_repositories
                    if item.repository.casefold() == repository.casefold()
                )
                repo_path = Path(managed.local_path)
                if preview.get("remove_empty_work_branch"):
                    policy = load_issue_code_policy(
                        repo_path / ".github" / "issue-code-policy.json"
                    )
                    current_head = _run_process(
                        ["git", "rev-parse", "HEAD"],
                        repo_path,
                        30,
                    ).stdout.strip()
                    if (
                        current_head != preview.get("base_commit")
                        or not _cleanup_empty_work_branch(
                            repo_path,
                            policy,
                            str(preview.get("work_branch", "")),
                        )
                    ):
                        raise RuntimeError("resume_dispatch_failed")
                resume_attempt = preview.get("resume_attempt")
                expected_output = (
                    "dispatch-resume.json"
                    if resume_attempt == 1
                    else f"dispatch-resume-{resume_attempt}.json"
                )
                if (
                    not isinstance(resume_attempt, int)
                    or not 1 <= resume_attempt <= MAX_RESUME_ATTEMPTS
                    or preview.get("resume_output") != expected_output
                ):
                    raise RuntimeError("resume_dispatch_failed")
                try:
                    dispatch = dispatch_once(
                        repo_path,
                        repo_path / ".github" / "issue-code-policy.json",
                        GitHubCLIApprovedIssueClient(30.0),
                        GitHubCLIIssueSnapshotClient(30.0),
                        GitHubCLIDispatchStateInspector(30.0),
                        CopilotCLICodeModifier(),
                        max_candidates=20,
                        execute=True,
                        publish_pr=True,
                        model=config.copilot_model,
                        target_issue_url=issue_url,
                        retained_claim_commit=str(
                            preview.get("claim_commit", "")
                        ),
                        expected_issue_snapshot_sha256=str(
                            preview.get("snapshot_sha256", "")
                        ),
                    )
                except (OSError, ValueError) as exc:
                    raise RuntimeError("resume_dispatch_failed") from exc
                _atomic_write_json(
                    self._run_dir(run_id) / expected_output,
                    dispatch,
                )
                modifier = dispatch.get("dispatch", {}).get("modifier_report") or {}
                draft_pr_url = modifier.get("publication", {}).get("draft_pr_url")
                if dispatch.get("status") != "draft_pr_created" or not draft_pr_url:
                    failure_reason = str(
                        dispatch.get("dispatch", {}).get("failure_reason", "")
                    )
                    raise RuntimeError(
                        failure_reason
                        if failure_reason in SAFE_ERROR_MESSAGES
                        else "resume_dispatch_failed"
                    )
                record.update(
                    {
                        "status": "completed",
                        "updated_at": _utc_now(),
                        "result": {
                            "issue_url": issue_url,
                            "draft_pr_url": draft_pr_url,
                        },
                        "failure": None,
                    }
                )
            except RuntimeError as exc:
                code = str(exc)
                record.update(
                    {
                        "status": "blocked",
                        "updated_at": _utc_now(),
                        "result": {
                            "issue_url": issue_url,
                            "draft_pr_url": None,
                        },
                        "failure": {
                            "code": code,
                            "message": SAFE_ERROR_MESSAGES.get(
                                code, SAFE_ERROR_MESSAGES["unexpected_failure"]
                            ),
                        },
                    }
                )
            except (OSError, ValueError, KeyError, StopIteration, json.JSONDecodeError):
                record.update(
                    {
                        "status": "blocked",
                        "updated_at": _utc_now(),
                        "result": {
                            "issue_url": issue_url,
                            "draft_pr_url": None,
                        },
                        "failure": {
                            "code": "unexpected_failure",
                            "message": SAFE_ERROR_MESSAGES["unexpected_failure"],
                        },
                    }
                )
            self._write_record(run_id, record)

    def _execute(self, run_id: str) -> None:
        with self._lock(run_id):
            record = self.read(run_id)
            issue_url: Optional[str] = None
            try:
                config = self.config_store.load()
                if config is None or config.sha256 != record["config_sha256"]:
                    raise RuntimeError("configuration_changed")
                run_dir = self._run_dir(run_id)
                generation = json.loads(
                    (run_dir / "generation.json").read_text(encoding="utf-8")
                )
                evidence = json.loads(
                    (run_dir / "evidence.json").read_text(encoding="utf-8")
                )
                scope_path = run_dir / "routing-scope.json"
                policy_path = run_dir / "publication-policy.json"
                scope = load_search_scope(scope_path)
                policy_sha = hashlib.sha256(policy_path.read_bytes()).hexdigest()
                policy = load_auto_publish_policy(
                    policy_path, policy_sha, scope, scope_path
                )
                automation = automate_repository_issue(
                    generation,
                    evidence,
                    scope,
                    GitHubCLICodeSearchAdapter(30.0),
                    "github-code-search",
                    policy,
                    GitHubCLIIssueClient(30.0),
                    True,
                    preselected_repository=(
                        config.enabled_repositories[0].repository
                        if len(config.enabled_repositories) == 1
                        else ""
                    ),
                )
                _atomic_write_json(run_dir / "automation-publication.json", automation)
                publication = automation.get("publication", {})
                if publication.get("status") not in {"created", "deduplicated"}:
                    raise RuntimeError("issue_publication_failed")
                issue_url = str(publication.get("issue_url", ""))
                repository = str(publication.get("repository", ""))
                if repository != record["preview"]["repository"]:
                    raise RuntimeError("configuration_changed")
                managed = next(
                    item
                    for item in config.enabled_repositories
                    if item.repository.casefold() == repository.casefold()
                )
                repo_path = Path(managed.local_path)
                try:
                    self.approval_client.ensure_and_apply(
                        repo_path,
                        repository,
                        issue_url,
                        managed.required_labels,
                    )
                except ValueError as exc:
                    raise RuntimeError("approval_label_failed") from exc
                try:
                    dispatch = dispatch_once(
                        repo_path,
                        repo_path / ".github" / "issue-code-policy.json",
                        GitHubCLIApprovedIssueClient(30.0),
                        GitHubCLIIssueSnapshotClient(30.0),
                        GitHubCLIDispatchStateInspector(30.0),
                        CopilotCLICodeModifier(),
                        max_candidates=20,
                        execute=True,
                        publish_pr=True,
                        model=config.copilot_model,
                        target_issue_url=issue_url,
                        claimer=GitRemoteBranchClaimer(120.0),
                    )
                except (OSError, ValueError) as exc:
                    raise RuntimeError("code_dispatch_failed") from exc
                _atomic_write_json(run_dir / "dispatch.json", dispatch)
                modifier = dispatch.get("dispatch", {}).get("modifier_report") or {}
                draft_pr_url = modifier.get("publication", {}).get("draft_pr_url")
                if dispatch.get("status") != "draft_pr_created" or not draft_pr_url:
                    failure_reason = str(
                        dispatch.get("dispatch", {}).get("failure_reason", "")
                    )
                    raise RuntimeError(
                        failure_reason
                        if failure_reason in SAFE_ERROR_MESSAGES
                        else "code_dispatch_failed"
                    )
                record.update(
                    {
                        "status": "completed",
                        "updated_at": _utc_now(),
                        "result": {
                            "issue_url": issue_url,
                            "draft_pr_url": draft_pr_url,
                        },
                        "failure": None,
                    }
                )
            except RuntimeError as exc:
                code = str(exc)
                record.update(
                    {
                        "status": "blocked",
                        "updated_at": _utc_now(),
                        "result": {
                            "issue_url": issue_url,
                            "draft_pr_url": None,
                        },
                        "failure": {
                            "code": code,
                            "message": SAFE_ERROR_MESSAGES.get(
                                code, SAFE_ERROR_MESSAGES["unexpected_failure"]
                            ),
                        },
                    }
                )
            except (OSError, ValueError, KeyError, StopIteration, json.JSONDecodeError):
                record.update(
                    {
                        "status": "blocked",
                        "updated_at": _utc_now(),
                        "result": {
                            "issue_url": issue_url,
                            "draft_pr_url": None,
                        },
                        "failure": {
                            "code": "unexpected_failure",
                            "message": SAFE_ERROR_MESSAGES["unexpected_failure"],
                        },
                    }
                )
            self._write_record(run_id, record)
