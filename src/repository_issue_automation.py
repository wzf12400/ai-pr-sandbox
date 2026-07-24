"""Deterministically approve, deduplicate, and publish one resolved Issue."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Protocol, Sequence, Tuple

from src import ai_issue_generator
from src.issue_intake import find_sensitive_data
from src.repository_resolver import (
    MINIMUM_MARGIN,
    MINIMUM_RESOLVED_SCORE,
    MINIMUM_STRONG_FAMILIES,
    POLICY_VERSION as RESOLUTION_POLICY_VERSION,
    REPOSITORY_PATTERN,
    RESOLUTION_SCHEMA_VERSION,
    RepositorySearchAdapter,
    RepositorySearchScope,
    resolve_repository,
)


AUTO_POLICY_SCHEMA_VERSION = "repository-auto-publish-policy/v1"
AUTOMATION_SCHEMA_VERSION = "repository-issue-automation/v1"
FINGERPRINT_VERSION = "repository-issue-fingerprint/v1"
POLICY_ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,80}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
ISSUE_URL_PATTERN = re.compile(r"https://github\.com/[^/]+/[^/]+/issues/(\d+)")
MAX_POLICY_BYTES = 64_000
MAX_ISSUES_SCANNED = 100
ALLOWED_GENERATION_STATES = {"ready_for_human_review", "needs_human_context"}
ALLOWED_ADAPTERS = {"github-code-search", "github-tree-probe"}


def _mapping(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalized(value: Any) -> str:
    text = " ".join(_text(value).casefold().split())
    return "" if text == "unknown" else text


def _exact_keys(
    payload: Mapping[str, Any], required: Sequence[str], field: str
) -> None:
    missing = sorted(set(required) - set(payload))
    extra = sorted(set(payload) - set(required))
    if missing:
        raise ValueError(f"{field} is missing fields: {', '.join(missing)}")
    if extra:
        raise ValueError(f"{field} contains unsupported fields: {', '.join(extra)}")


@dataclass(frozen=True)
class RepositoryAutoPublishPolicy:
    policy_id: str
    policy_sha256: str
    scope_id: str
    scope_sha256: str
    provider: str
    max_issues_per_run: int
    allowed_generation_states: frozenset[str]
    allowed_adapters: frozenset[str]


def load_auto_publish_policy(
    path: Path,
    confirmed_sha256: str,
    scope: RepositorySearchScope,
    scope_path: Path,
) -> RepositoryAutoPublishPolicy:
    if path.is_symlink() or scope_path.is_symlink():
        raise ValueError("automatic publication policy and scope must not be symbolic links")
    try:
        raw = path.read_bytes()
        scope_raw = scope_path.read_bytes()
    except OSError as exc:
        raise ValueError("unable to read automatic publication policy or scope") from exc
    if not raw or len(raw) > MAX_POLICY_BYTES:
        raise ValueError("automatic publication policy size is invalid")
    digest = hashlib.sha256(raw).hexdigest()
    if not SHA256_PATTERN.fullmatch(confirmed_sha256) or digest != confirmed_sha256:
        raise ValueError("automatic publication policy SHA-256 confirmation does not match")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("automatic publication policy must contain valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("automatic publication policy must be an object")
    required = (
        "schema_version",
        "policy_id",
        "scope_id",
        "scope_sha256",
        "provider",
        "max_issues_per_run",
        "allowed_generation_states",
        "allowed_adapters",
    )
    _exact_keys(payload, required, "automatic publication policy")
    if payload.get("schema_version") != AUTO_POLICY_SCHEMA_VERSION:
        raise ValueError(f"automatic publication policy must use {AUTO_POLICY_SCHEMA_VERSION}")
    policy_id = _text(payload.get("policy_id"))
    if not POLICY_ID_PATTERN.fullmatch(policy_id):
        raise ValueError("automatic publication policy_id is invalid")
    scope_id = _text(payload.get("scope_id"))
    scope_digest = _text(payload.get("scope_sha256"))
    actual_scope_digest = hashlib.sha256(scope_raw).hexdigest()
    if scope_id != scope.scope_id or scope_digest != actual_scope_digest:
        raise ValueError("automatic publication policy is not bound to the reviewed scope")
    if payload.get("provider") != "github_cli":
        raise ValueError("automatic publication provider must be github_cli")
    if payload.get("max_issues_per_run") != 1:
        raise ValueError("automatic publication is limited to one Issue per invocation")
    raw_states = payload.get("allowed_generation_states")
    if not isinstance(raw_states, list) or not raw_states:
        raise ValueError("allowed_generation_states must be a nonempty array")
    states = frozenset(_text(state) for state in raw_states)
    if "" in states or not states <= ALLOWED_GENERATION_STATES:
        raise ValueError("automatic publication policy contains an unsupported generation state")
    raw_adapters = payload.get("allowed_adapters")
    if not isinstance(raw_adapters, list) or not raw_adapters:
        raise ValueError("allowed_adapters must be a nonempty array")
    adapters = frozenset(_text(adapter) for adapter in raw_adapters)
    if "" in adapters or not adapters <= ALLOWED_ADAPTERS:
        raise ValueError("automatic publication policy contains an unsupported adapter")
    return RepositoryAutoPublishPolicy(
        policy_id=policy_id,
        policy_sha256=digest,
        scope_id=scope_id,
        scope_sha256=scope_digest,
        provider="github_cli",
        max_issues_per_run=1,
        allowed_generation_states=states,
        allowed_adapters=adapters,
    )


class GitHubIssueClient(Protocol):
    def list_issues(self, repository: str, limit: int) -> List[Dict[str, Any]]:
        ...

    def create_issue(self, repository: str, title: str, body: str) -> str:
        ...


class GitHubCLIIssueClient:
    def __init__(self, timeout_seconds: float = 30.0):
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("GitHub Issue timeout must be between 1 and 120 seconds")
        self.timeout_seconds = timeout_seconds

    def list_issues(self, repository: str, limit: int) -> List[Dict[str, Any]]:
        if not REPOSITORY_PATTERN.fullmatch(repository) or not 1 <= limit <= MAX_ISSUES_SCANNED:
            raise ValueError("GitHub Issue search arguments are invalid")
        command = [
            "gh",
            "issue",
            "list",
            "--repo",
            repository,
            "--state",
            "all",
            "--limit",
            str(limit),
            "--json",
            "number,title,body,url,state",
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("GitHub Issue search could not be completed") from exc
        if completed.returncode != 0:
            raise ValueError("GitHub Issue search failed closed")
        try:
            issues = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("GitHub Issue search returned invalid JSON") from exc
        if not isinstance(issues, list):
            raise ValueError("GitHub Issue search returned an invalid result")
        return [issue for issue in issues[:limit] if isinstance(issue, dict)]

    def create_issue(self, repository: str, title: str, body: str) -> str:
        if not REPOSITORY_PATTERN.fullmatch(repository):
            raise ValueError("GitHub Issue repository is invalid")
        if not title.strip() or len(title) > 160 or not body.strip():
            raise ValueError("GitHub Issue title or body is invalid")
        temporary_path = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", prefix="issue-body-", suffix=".md", delete=False
            ) as handle:
                handle.write(body)
                temporary_path = handle.name
            completed = subprocess.run(
                [
                    "gh",
                    "issue",
                    "create",
                    "--repo",
                    repository,
                    "--title",
                    title,
                    "--body-file",
                    temporary_path,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("GitHub Issue creation could not be completed") from exc
        finally:
            if temporary_path:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass
        if completed.returncode != 0:
            raise ValueError("GitHub Issue creation failed closed")
        issue_url = completed.stdout.strip()
        if not ISSUE_URL_PATTERN.fullmatch(issue_url):
            raise ValueError("GitHub Issue creation returned an invalid URL")
        return issue_url


def issue_fingerprint(generation: Mapping[str, Any], repository: str) -> str:
    draft = _mapping(generation.get("draft"))
    obj = _mapping(draft.get("object"))
    interface = _mapping(draft.get("interface"))
    error = _mapping(draft.get("error"))
    problem = _mapping(draft.get("problem"))
    material = {
        "version": FINGERPRINT_VERSION,
        "repository": repository.casefold(),
        "service": _normalized(obj.get("service")),
        "module": _normalized(obj.get("module")),
        "code_object": _normalized(obj.get("code_object")),
        "interface_method": _normalized(interface.get("method")),
        "interface_path": _normalized(interface.get("path_or_topic")),
        "error_code": _normalized(error.get("error_code")),
        "exception_type": _normalized(error.get("exception_type")),
        "error_message": _normalized(error.get("message")),
        "current_behavior": _normalized(problem.get("current_behavior")),
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _fingerprint_marker(fingerprint: str) -> str:
    return f"<!-- {FINGERPRINT_VERSION}:{fingerprint} -->"


def render_automated_issue_body(
    generation: Mapping[str, Any], repository: str, policy: RepositoryAutoPublishPolicy
) -> Tuple[str, str]:
    fingerprint = issue_fingerprint(generation, repository)
    body = ai_issue_generator.render_markdown(dict(generation))
    body = body.replace(
        "> AI-generated local draft. Human confirmation is required; automatic publication is disabled.",
        "> AI-generated draft. Publication was authorized by a reviewed deterministic policy; AI did not authorize this Issue.",
        1,
    )
    body = body.replace(
        "- AI implementation allowed: no",
        "- AI implementation permission: separate downstream policy required",
        1,
    )
    body += (
        "\n\n## Automated routing audit\n\n"
        f"- Resolution policy: `{RESOLUTION_POLICY_VERSION}`\n"
        f"- Publication policy: `{policy.policy_id}`\n"
        f"- Publication policy SHA-256: `{policy.policy_sha256}`\n"
        "- Code modification approval: separate downstream policy required\n\n"
        f"{_fingerprint_marker(fingerprint)}\n"
    )
    if find_sensitive_data(body):
        raise ValueError("rendered GitHub Issue failed sensitive-data validation")
    return body, fingerprint


def match_existing_issues(
    generation: Mapping[str, Any], repository: str, issues: Sequence[Mapping[str, Any]]
) -> Dict[str, Any]:
    fingerprint = issue_fingerprint(generation, repository)
    marker = _fingerprint_marker(fingerprint)
    draft = _mapping(generation.get("draft"))
    title = _normalized(draft.get("title"))
    obj = _mapping(draft.get("object"))
    interface = _mapping(draft.get("interface"))
    error = _mapping(draft.get("error"))
    signals = {
        "title": title,
        "code_object": _normalized(obj.get("code_object")),
        "exception_type": _normalized(error.get("exception_type")),
        "interface_path": _normalized(interface.get("path_or_topic")),
        "interface_method": _normalized(interface.get("method")),
    }
    candidates = []
    for issue in issues[:MAX_ISSUES_SCANNED]:
        body = _text(issue.get("body"))
        normalized_body = body.casefold()
        exact = marker in body
        score = 100 if exact else 0
        reasons = ["exact deterministic fingerprint"] if exact else []
        if not exact:
            if title and _normalized(issue.get("title")) == title:
                score += 35
                reasons.append("exact normalized title")
            for name, weight in (
                ("code_object", 30),
                ("exception_type", 20),
                ("interface_path", 15),
                ("interface_method", 10),
            ):
                value = signals[name]
                if value and value in normalized_body:
                    score += weight
                    reasons.append(name)
            score = min(score, 100)
        if score < 80:
            continue
        number = issue.get("number")
        url = _text(issue.get("url"))
        state = _text(issue.get("state"))
        if not isinstance(number, int) or not ISSUE_URL_PATTERN.fullmatch(url):
            continue
        candidates.append(
            {
                "number": number,
                "url": url,
                "state": state,
                "score": score,
                "reasons": reasons,
                "exact_fingerprint": exact,
            }
        )
    candidates.sort(key=lambda item: (-item["score"], item["number"]))
    exact_candidates = [item for item in candidates if item["exact_fingerprint"]]
    if len(exact_candidates) == 1:
        status = "existing_issue_candidate"
        selected = exact_candidates[0]
    elif len(exact_candidates) > 1 or len(candidates) > 1:
        status = "ambiguous_existing_issues"
        selected = None
    elif len(candidates) == 1:
        status = "existing_issue_candidate"
        selected = candidates[0]
    else:
        status = "new_issue"
        selected = None
    return {
        "status": status,
        "fingerprint": fingerprint,
        "selected": selected,
        "candidates": candidates,
        "issues_scanned": min(len(issues), MAX_ISSUES_SCANNED),
        "raw_issue_bodies_persisted": False,
    }


def automate_repository_issue(
    generation: Mapping[str, Any],
    evidence: Mapping[str, Any],
    scope: RepositorySearchScope,
    search_adapter: RepositorySearchAdapter,
    adapter_name: str,
    policy: RepositoryAutoPublishPolicy,
    issue_client: GitHubIssueClient,
    auto_publish: bool,
) -> Dict[str, Any]:
    generation_policy = _mapping(generation.get("policy"))
    review = _mapping(generation.get("review"))
    validation = _mapping(generation.get("validation"))
    safety = _mapping(evidence.get("safety"))
    pre_resolution_rules = {
        "generation_state_allowed": generation.get("state")
        in policy.allowed_generation_states,
        "generation_locally_valid": validation.get("valid") is True
        and validation.get("errors") in ([], None),
        "review_safety_passed": review.get("verdict") != "reject"
        and review.get("sensitive_data_detected") is False,
        "evidence_ai_allowed": safety.get("ai_allowed") is True,
        "security_review_not_required": safety.get("security_review_required") is not True,
        "ai_did_not_authorize_actions": generation_policy.get(
            "human_confirmation_required"
        )
        is True
        and generation_policy.get("publication_allowed") is False
        and generation_policy.get("implementation_allowed") is False,
        "adapter_allowed": adapter_name in policy.allowed_adapters,
    }
    if all(pre_resolution_rules.values()):
        resolution = resolve_repository(generation, scope, search_adapter)
    else:
        digest = _text(generation.get("input_sha256"))
        resolution = {
            "schema_version": RESOLUTION_SCHEMA_VERSION,
            "draft_ref": f"draft_ref:{digest[:16]}",
            "input_sha256": digest,
            "scope_id": scope.scope_id,
            "status": "blocked",
            "selected_repository": None,
            "decision": {
                "policy_version": RESOLUTION_POLICY_VERSION,
                "minimum_resolved_score": MINIMUM_RESOLVED_SCORE,
                "minimum_margin": MINIMUM_MARGIN,
                "minimum_strong_families": MINIMUM_STRONG_FAMILIES,
                "top_score": None,
                "runner_up_score": None,
                "margin": None,
                "reasons": ["pre-resolution automatic publication gate failed closed"],
            },
            "candidates": [],
            "search_audit": {
                "provider": "github",
                "repositories_enabled": len(scope.enabled_repositories),
                "queries_executed": 0,
                "candidate_repositories_verified": 0,
                "raw_source_snippets_persisted": False,
            },
        }
    output: Dict[str, Any] = {
        "schema_version": AUTOMATION_SCHEMA_VERSION,
        "policy": {
            "policy_id": policy.policy_id,
            "policy_sha256": policy.policy_sha256,
            "scope_id": policy.scope_id,
            "scope_sha256": policy.scope_sha256,
        },
        "resolution": resolution,
        "issue_match": {
            "status": "not_resolved",
            "candidates": [],
            "issues_scanned": 0,
            "raw_issue_bodies_persisted": False,
        },
        "approval": {"approved": False, "rules": {}},
        "publication": {
            "requested": auto_publish,
            "status": "blocked",
            "repository": None,
            "issue_url": None,
            "issue_number": None,
        },
    }
    rules = {
        **pre_resolution_rules,
        "resolution_policy_matches": _mapping(resolution.get("decision")).get(
            "policy_version"
        )
        == RESOLUTION_POLICY_VERSION,
        "repository_uniquely_resolved": resolution.get("status") == "resolved",
    }
    output["approval"]["rules"] = rules
    if not all(rules.values()):
        return output

    repository = _text(resolution.get("selected_repository"))
    try:
        issues = issue_client.list_issues(repository, MAX_ISSUES_SCANNED)
        issue_match = match_existing_issues(generation, repository, issues)
    except ValueError:
        output["issue_match"]["status"] = "blocked"
        return output
    output["issue_match"] = issue_match
    output["publication"]["repository"] = repository
    if issue_match["status"] == "ambiguous_existing_issues":
        return output
    if issue_match["status"] == "existing_issue_candidate":
        selected = issue_match["selected"]
        output["approval"]["approved"] = True
        output["publication"].update(
            {
                "status": "deduplicated",
                "issue_url": selected["url"],
                "issue_number": selected["number"],
            }
        )
        return output

    body, _ = render_automated_issue_body(generation, repository, policy)
    output["approval"]["approved"] = True
    output["publication"]["status"] = "approved_not_published"
    if not auto_publish:
        return output
    try:
        issue_url = issue_client.create_issue(
            repository,
            _text(_mapping(generation.get("draft")).get("title")),
            body,
        )
    except ValueError:
        output["publication"]["status"] = "blocked"
        return output
    match = ISSUE_URL_PATTERN.fullmatch(issue_url)
    output["publication"].update(
        {
            "status": "created",
            "issue_url": issue_url,
            "issue_number": int(match.group(1)),
        }
    )
    return output
