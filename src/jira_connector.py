"""Jira connector: poll Jira via a session cookie, map issues to sanitized intake records,
and route them deterministically to authorized repositories.

Design contract (see HANDOFF.md):
- Authentication is a session cookie from env (JIRA_SESSION_COOKIE); it never enters
  the frontend, git, or log output.
- Raw Jira payloads never reach the model: issues are minimized into
  issue-intake/v1-shaped dicts and scanned with issue_intake.find_sensitive_data.
  Any finding blocks the issue (fail-closed) and is only recorded in the shadow log.
- Attachments are metadata-only (filename/mimeType/size/URL reference); content is
  never downloaded.
- Routing is deterministic: explicit component/label bindings and keyword scoring
  from control-plane/config/jira-projects.json. Ambiguity or signal conflict always
  lands on NEEDS_CONTEXT; the connector never guesses.
- Default mode is shadow (dry-run): decisions are appended to the shadow log and
  nothing is dispatched. --dispatch only sends when the decision is auto-eligible
  and the control plane is a loopback address.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from src.issue_draft import _atomic_write_json, _atomic_write_text
from src.issue_intake import find_sensitive_data


BASE_URL_ENV = "JIRA_BASE_URL"
COOKIE_ENV = "JIRA_SESSION_COOKIE"
CONFIG_PATH = Path("control-plane/config/jira-projects.json")
STATE_PATH = Path(".jira-connector-state.json")
SHADOW_LOG_PATH = Path(".jira-shadow-log.jsonl")
SURVEY_PATH = Path(".jira-survey.json")

SCHEMA_VERSION = "jira-projects/v1"
ISSUE_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,19}-\d{1,7}$")
PROJECT_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,19}$")
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
MAX_SEARCH_RESULTS = 100
MAX_SUMMARY_CHARS = 400
MAX_FIELD_CHARS = 4000
MAX_ATTACHMENTS = 20
HTTP_TIMEOUT_SECONDS = 15
MIN_INTERVAL_SECONDS = 60
MAX_INTERVAL_SECONDS = 3600
KEYWORD_SCORE_PER_HIT = 25
KEYWORD_RESOLVED_SCORE = 50
KEYWORD_MIN_MARGIN = 25
AI_ROUTE_MIN_CONFIDENCE = 70
ANCHOR_ROUTE_CONFIDENCE = 95

REQUEST_TYPE_MAP = {
    "bug": "Bug",
    "defect": "Bug",
    "缺陷": "Bug",
    "story": "Feature",
    "new feature": "Feature",
    "新需求": "Feature",
    "improvement": "Feature",
    "改进": "Feature",
    "task": "Unknown",
    "任务": "Unknown",
    "子任务": "Unknown",
    "technical task": "Unknown",
    "epic": "Unknown",
}


@dataclass(frozen=True)
class RouteDecision:
    status: str  # "RESOLVED" | "NEEDS_CONTEXT" | "BLOCKED_SENSITIVE"
    repository: str = ""
    basis: str = ""
    confidence: int = 0
    candidates: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Config


def load_config(path: Path = CONFIG_PATH) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"jira projects config schema_version must be {SCHEMA_VERSION}")
    projects = payload.get("projects")
    if not isinstance(projects, dict):
        raise ValueError("jira projects config requires a projects object")
    for key, project in projects.items():
        _validate_project(key, project)
    return payload


def _validate_project(key: str, project: Dict[str, Any]) -> None:
    if not PROJECT_KEY_PATTERN.fullmatch(key):
        raise ValueError(f"invalid project key: {key!r}")
    if not isinstance(project, dict):
        raise ValueError(f"project {key} must be an object")
    repositories = project.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise ValueError(f"project {key} requires a nonempty repositories array")
    seen = set()
    for entry in repositories:
        repo = entry.get("repository", "")
        if not REPOSITORY_PATTERN.fullmatch(repo):
            raise ValueError(f"project {key} has invalid repository: {repo!r}")
        if repo in seen:
            raise ValueError(f"project {key} binds repository twice: {repo}")
        seen.add(repo)
        for list_name in ("components", "labels", "keywords"):
            values = entry.get(list_name, [])
            if not isinstance(values, list) or not all(isinstance(v, str) and v for v in values):
                raise ValueError(f"project {key} {list_name} must be an array of nonempty strings")


# ---------------------------------------------------------------------------
# HTTP


class JiraAuthError(RuntimeError):
    """会话失效（401/403 或被 SSO 网关重定向到登录页），需要刷新 Cookie。"""


def _require_env() -> tuple[str, str]:
    base = os.environ.get(BASE_URL_ENV, "").strip().rstrip("/")
    cookie = os.environ.get(COOKIE_ENV, "").strip()
    if not base:
        raise RuntimeError(f"{BASE_URL_ENV} is not set")
    if urllib.parse.urlsplit(base).scheme != "https":
        raise RuntimeError(f"{BASE_URL_ENV} must use https")
    if not cookie:
        raise RuntimeError(f"{COOKIE_ENV} is not set")
    return base, cookie


def _get_json(path: str) -> Any:
    base, cookie = _require_env()
    request = urllib.request.Request(
        f"{base}{path}",
        headers={"Cookie": cookie, "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            final_url = response.geturl()
            if "login" in final_url and "/rest/" not in final_url:
                raise JiraAuthError(
                    f"Jira session expired (redirected to {final_url}); "
                    f"refresh {COOKIE_ENV}"
                )
            try:
                return json.loads(response.read().decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise JiraAuthError(
                    f"Jira returned non-JSON (likely SSO login page); "
                    f"refresh {COOKIE_ENV}"
                ) from exc
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise JiraAuthError(
                f"Jira session rejected (HTTP {exc.code}); refresh {COOKIE_ENV}"
            ) from exc
        raise RuntimeError(f"Jira API error: HTTP {exc.code} for {path}") from exc


def _verify_session() -> Dict[str, Any]:
    data = _get_json("/rest/api/2/myself")
    if not isinstance(data, dict) or not data.get("name"):
        raise RuntimeError("Jira session check returned an unexpected payload")
    return data


# ---------------------------------------------------------------------------
# Survey (metadata reconnaissance; output stays local and gitignored)


def survey() -> Dict[str, Any]:
    user = _verify_session()
    projects = _get_json("/rest/api/2/project")
    priorities = _get_json("/rest/api/2/priority")
    issue_types = _get_json("/rest/api/2/issuetype")
    report: Dict[str, Any] = {
        "surveyed_at": datetime.now(timezone.utc).isoformat(),
        "user": {"name": user.get("name"), "displayName": user.get("displayName")},
        "priorities": [p.get("name") for p in priorities if isinstance(p, dict)],
        "issue_types": [t.get("name") for t in issue_types if isinstance(t, dict)],
        "projects": [],
    }
    for project in projects if isinstance(projects, list) else []:
        key = project.get("key")
        entry: Dict[str, Any] = {
            "key": key,
            "name": project.get("name"),
            "components": [],
            "issue_types": [],
        }
        try:
            components = _get_json(f"/rest/api/2/project/{key}/components")
            entry["components"] = [c.get("name") for c in components if isinstance(c, dict)]
        except RuntimeError as exc:
            entry["components_error"] = str(exc)
        try:
            meta = _get_json(
                f"/rest/api/2/issue/createmeta?projectKeys={key}&expand=projects.issuetypes"
            )
            for meta_project in meta.get("projects", []):
                for issue_type in meta_project.get("issuetypes", []):
                    entry["issue_types"].append(issue_type.get("name"))
        except RuntimeError as exc:
            entry["createmeta_error"] = str(exc)
        report["projects"].append(entry)
    _atomic_write_json(SURVEY_PATH, report)
    return report


# ---------------------------------------------------------------------------
# Routing (deterministic; mirrors the control-plane keyword thresholds)


def _normalized_hits(values: Iterable[str], needles: Iterable[str]) -> List[str]:
    haystack = [value.strip().lower() for value in values if value and value.strip()]
    hits = []
    for needle in needles:
        lowered = needle.strip().lower()
        if lowered and any(lowered == value for value in haystack):
            hits.append(needle)
    return hits


def _keyword_scores(text: str, repositories: List[Dict[str, Any]]) -> List[tuple[str, int, List[str]]]:
    normalized = text.lower()
    scored = []
    for entry in repositories:
        matches = sorted({k for k in entry.get("keywords", []) if k.lower() in normalized})
        if matches:
            scored.append((entry["repository"], min(100, len(matches) * KEYWORD_SCORE_PER_HIT), matches))
    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored


def route_issue(issue: Dict[str, Any], project: Dict[str, Any]) -> RouteDecision:
    repositories = project["repositories"]
    candidates = [entry["repository"] for entry in repositories]

    if len(repositories) == 1 and not project.get("strict_matching"):
        return RouteDecision(
            "RESOLVED",
            repository=repositories[0]["repository"],
            basis="single repository binding for project",
            confidence=100,
            candidates=candidates,
        )

    fields = issue.get("fields") or {}
    components = [c.get("name", "") for c in fields.get("components") or [] if isinstance(c, dict)]
    labels = [label for label in fields.get("labels") or [] if isinstance(label, str)]

    component_hits = {
        entry["repository"]: _normalized_hits(components, entry.get("components", []))
        for entry in repositories
    }
    component_winners = sorted(repo for repo, hits in component_hits.items() if hits)
    label_hits = {
        entry["repository"]: _normalized_hits(labels, entry.get("labels", []))
        for entry in repositories
    }
    label_winners = sorted(repo for repo, hits in label_hits.items() if hits)

    text = "\n".join(
        part
        for part in (fields.get("summary") or "", fields.get("description") or "")
        if part
    )[:MAX_FIELD_CHARS]
    scores = _keyword_scores(text, repositories)
    keyword_winner = ""
    keyword_basis = ""
    keyword_confidence = 0
    if scores:
        top_repo, top_score, top_matches = scores[0]
        second = scores[1][1] if len(scores) > 1 else 0
        if top_score >= KEYWORD_RESOLVED_SCORE and top_score - second >= KEYWORD_MIN_MARGIN:
            keyword_winner = top_repo
            keyword_basis = "keywords: " + ", ".join(top_matches)
            keyword_confidence = top_score

    explicit = sorted(set(component_winners) | set(label_winners))
    if len(explicit) > 1:
        return RouteDecision(
            "NEEDS_CONTEXT",
            basis="conflicting bindings: components=" + ",".join(component_winners)
            + " labels=" + ",".join(label_winners),
            candidates=candidates,
        )
    if explicit:
        winner = explicit[0]
        basis_parts = []
        if component_hits[winner]:
            basis_parts.append("components: " + ", ".join(component_hits[winner]))
        if label_hits[winner]:
            basis_parts.append("labels: " + ", ".join(label_hits[winner]))
        if keyword_winner and keyword_winner != winner:
            return RouteDecision(
                "NEEDS_CONTEXT",
                basis=f"binding says {winner} but keywords say {keyword_winner}",
                candidates=candidates,
            )
        return RouteDecision(
            "RESOLVED",
            repository=winner,
            basis="; ".join(basis_parts),
            confidence=100,
            candidates=candidates,
        )
    if keyword_winner:
        return RouteDecision(
            "RESOLVED",
            repository=keyword_winner,
            basis=keyword_basis,
            confidence=keyword_confidence,
            candidates=candidates,
        )
    return RouteDecision(
        "NEEDS_CONTEXT",
        basis="no component/label binding and keyword score below threshold or ambiguous",
        confidence=scores[0][1] if scores else 0,
        candidates=candidates,
    )


# ---------------------------------------------------------------------------
# Fallback routing: anchor code search + AI profile classification
#
# route_issue stays pure/deterministic. route_issue_with_fallbacks layers two
# evidence-based stages on top for issues that land on NEEDS_CONTEXT:
#   2nd: anchor code search (when the issue text carries technical anchors)
#   3rd: AI classification against auto-generated repo profiles
# Both stages are fail-open: any error leaves the NEEDS_CONTEXT decision
# untouched, so the worst case is "待人工" — never a wrong guess.


def _ai_config_from_env() -> Optional[Dict[str, str]]:
    base = os.environ.get("AI_BASE_URL", "").strip()
    key = os.environ.get("AI_API_KEY", "").strip()
    if not base or not key:
        return None
    return {
        "base_url": base,
        "api_key": key,
        "model": os.environ.get("AI_MODEL", "ailemac/gpt-5-mini").strip(),
        "safety_id": os.environ.get("AI_SAFETY_IDENTIFIER", "").strip(),
    }


def _anchor_fallback(
    text: str, candidates: List[str]
) -> Optional[RouteDecision]:
    token = os.environ.get("GITHUB_ROUTING_TOKEN", "").strip()
    if not token:
        return None
    try:
        from src import repo_anchor_router

        anchors = repo_anchor_router.extract_anchors(text, [], [])
        if not anchors:
            return None
        orgs = sorted({repo.split("/", 1)[0] for repo in candidates})
        cache = repo_anchor_router._load_cache()
        hits_per_anchor: Dict[str, Any] = {}
        for anchor in anchors[: repo_anchor_router.MAX_QUERIES]:
            try:
                hits_per_anchor[anchor] = repo_anchor_router._search_code(
                    anchor, orgs, token, cache
                )
            except repo_anchor_router.SearchBudgetExceeded:
                break
        anchor_decision = repo_anchor_router.decide(hits_per_anchor, candidates)
        if anchor_decision:
            return RouteDecision(
                "RESOLVED",
                repository=anchor_decision["repository"],
                basis=anchor_decision["basis"][:240],
                confidence=ANCHOR_ROUTE_CONFIDENCE,
                candidates=candidates,
            )
    except Exception:  # noqa: BLE001 - fail-open, deterministic path already answered
        return None
    return None


def _ai_fallback(
    issue: Dict[str, Any], project: Dict[str, Any], candidates: List[str]
) -> Optional[RouteDecision]:
    if not project.get("ai_routing"):
        return None
    ai = _ai_config_from_env()
    if ai is None:
        return None
    fields = issue.get("fields") or {}
    project_key = _text((fields.get("project") or {}).get("key"), 24)
    try:
        from src import repo_profiler

        ai_decision = repo_profiler.classify_issue(
            project_key,
            _text(fields.get("summary"), MAX_SUMMARY_CHARS),
            _text(fields.get("description")),
            candidates,
            ai,
            min_confidence=AI_ROUTE_MIN_CONFIDENCE,
        )
    except Exception:  # noqa: BLE001 - fail-open
        return None
    if ai_decision is None:
        return None
    return RouteDecision(
        "RESOLVED",
        repository=ai_decision["repository"],
        basis=ai_decision["basis"][:240],
        confidence=ai_decision["confidence"],
        candidates=candidates,
    )


def route_issue_with_fallbacks(issue: Dict[str, Any], project: Dict[str, Any]) -> RouteDecision:
    """确定性路由 + 证据兜底（锚点搜索 → AI 画像分类）。

    兜底的置信度都低于 100，因此不会触发 auto_dispatch（该门槛要求 100），
    只会在面板标出建议仓库，等人工确认或后续策略放开。
    """
    decision = route_issue(issue, project)
    if decision.status != "NEEDS_CONTEXT":
        return decision
    candidates = [entry["repository"] for entry in project["repositories"]]
    fields = issue.get("fields") or {}
    text = "\n".join(
        part
        for part in (fields.get("summary") or "", fields.get("description") or "")
        if part
    )[:MAX_FIELD_CHARS]
    fallback: Optional[RouteDecision] = None
    try:
        fallback = _anchor_fallback(text, candidates)
        if fallback is None:
            fallback = _ai_fallback(issue, project, candidates)
    except Exception:  # noqa: BLE001 - 兜底阶段永远不许炸掉主流程
        fallback = None
    return fallback if fallback is not None else decision


# ---------------------------------------------------------------------------
# Mapping Jira issue -> minimized intake dict


def _text(value: Any, limit: int = MAX_FIELD_CHARS) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def map_request_type(issue_type_name: str) -> str:
    return REQUEST_TYPE_MAP.get(issue_type_name.strip().lower(), "Unknown")


def issue_to_intake(issue: Dict[str, Any], base_url: str, project: Dict[str, Any]) -> Dict[str, Any]:
    fields = issue.get("fields") or {}
    key = _text(issue.get("key"), 32)
    severity_map = project.get("severity_map") or {}
    priority_name = _text((fields.get("priority") or {}).get("name"), 40)
    attachments = []
    for attachment in (fields.get("attachment") or [])[:MAX_ATTACHMENTS]:
        if not isinstance(attachment, dict):
            continue
        attachments.append(
            _text(attachment.get("filename"), 200)
            + f" ({_text(attachment.get('mimeType'), 100)})"
        )
    comments = fields.get("comment") or {}
    comment_bodies = [
        _text(c.get("body"), 500) for c in (comments.get("comments") or [])[:10] if isinstance(c, dict)
    ]
    description = _text(fields.get("description"))
    return {
        "schema_version": "issue-intake/v1",
        "source_type": "jira",
        "source_reference": key,
        "source_url": f"{base_url}/browse/{key}",
        "project_key": _text((fields.get("project") or {}).get("key"), 24),
        "summary": _text(fields.get("summary"), MAX_SUMMARY_CHARS),
        "request_type": map_request_type(_text((fields.get("issuetype") or {}).get("name"), 40)),
        "severity": severity_map.get(priority_name, "Unknown"),
        "target": {
            "product": _text((fields.get("project") or {}).get("name"), 120),
        },
        "problem": {
            "background": description,
            "current_behavior": "",
            "expected_behavior": "",
            "first_observed_at": _text(fields.get("created"), 40),
        },
        "runtime": {
            "occurred_at": _text(fields.get("created"), 40),
        },
        "attachments": [item for item in attachments if item],
        "impact": {},
        "acceptance_criteria": [],
        "automation_scope": "triage_only",
        "data_safety_status": "unreviewed",
        "_comments_excerpt": [body for body in comment_bodies if body],
    }


# ---------------------------------------------------------------------------
# Poll (shadow by default)


def build_jql(project_key: str, project: Dict[str, Any], watermark: str = "") -> str:
    # Note: issuetype/status name filters in JQL are locale-fragile on old Jira
    # versions (a Chinese display name can 400). Keep JQL minimal and filter
    # issue types client-side via the project config's issue_types list.
    conditions = [f"project = {project_key}"]
    extra = (project.get("jql_extra") or "").strip()
    if extra:
        conditions.append(f"({extra})")
    if watermark:
        conditions.append(f'updated >= "{watermark}"')
    return " AND ".join(conditions) + " ORDER BY updated ASC"


def _issue_type_allowed(issue: Dict[str, Any], project: Dict[str, Any]) -> bool:
    include = project.get("issue_types") or []
    if not include:
        return True
    name = ((issue.get("fields") or {}).get("issuetype") or {}).get("name") or ""
    return name in include


def _format_watermark(epoch_seconds: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch_seconds))


def _parse_jira_time(value: str) -> float:
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(value, fmt).timestamp()
        except ValueError:
            continue
    raise ValueError(f"unparseable Jira timestamp: {value!r}")


def _load_state(path: Path = STATE_PATH) -> Dict[str, Any]:
    if not path.exists():
        return {"version": 1, "projects": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload.get("projects"), dict):
        raise ValueError(f"invalid state file: {path}")
    return payload


def _append_shadow(record: Dict[str, Any], path: Path = SHADOW_LOG_PATH) -> None:
    line = json.dumps(record, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _loopback_control_plane_url() -> Optional[str]:
    base = os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8080").rstrip("/")
    if base.startswith(("http://127.0.0.1", "http://localhost")):
        return base
    return None


def _dispatch(intake: Dict[str, Any], decision: RouteDecision) -> Dict[str, Any]:
    base = _loopback_control_plane_url()
    if base is None:
        return {"result": "skipped", "detail": "control plane is not loopback"}
    payload = {
        "sourceType": "JIRA",
        "input": intake["summary"][:MAX_SUMMARY_CHARS],
        "jiraIssue": {
            "dataSafetyStatus": "SANITIZED",
            "sourceReference": intake["source_reference"],
            "issueUrl": intake["source_url"],
            "projectKey": intake["project_key"],
            "resolvedRepository": decision.repository,
            "mappingBasis": decision.basis[:240],
        },
    }
    request = urllib.request.Request(
        f"{base}/api/tasks",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
    except Exception as exception:
        return {"result": "failed", "detail": type(exception).__name__}
    task_id = body.get("id") if isinstance(body, dict) else None
    if not isinstance(task_id, str) or not task_id:
        return {"result": "failed", "detail": "invalid control-plane response"}
    return {"result": "created", "taskId": task_id, "taskStatus": body.get("status")}


ISSUE_DETAIL_FIELDS = "summary,description,issuetype,priority,project,components,labels,created,updated,comment,attachment"


def fetch_issue(issue_key: str) -> Dict[str, Any]:
    """Fetch one Jira issue with the connector's standard field set."""
    if not ISSUE_KEY_PATTERN.fullmatch(issue_key):
        raise ValueError(f"invalid issue key: {issue_key!r}")
    return _get_json(f"/rest/api/2/issue/{issue_key}?fields={ISSUE_DETAIL_FIELDS}")


def dispatch_issue(
    issue_key: str,
    config_path: Path = CONFIG_PATH,
    repository_override: str = "",
) -> Dict[str, Any]:
    """Fetch, map, sanitize-scan, route and dispatch a single issue by key.

    Used by the one-click action in the console; honors the same gates as poll
    (sensitive data and ambiguous routing fail closed). A human may pass
    repository_override to resolve a NEEDS_CONTEXT decision explicitly; the
    override must be one of the project's configured repositories.
    """
    config = load_config(config_path)
    base, _cookie = _require_env()
    issue = fetch_issue(issue_key)
    project_key = ((issue.get("fields") or {}).get("project") or {}).get("key") or ""
    project = config["projects"].get(project_key)
    if project is None:
        return {"result": "failed", "detail": f"项目 {project_key} 未接入配置"}
    intake = issue_to_intake(issue, base, project)
    findings = find_sensitive_data(
        {name: value for name, value in intake.items() if not name.startswith("_")}
    )
    if findings:
        return {
            "result": "failed",
            "detail": "敏感数据拦截: "
            + ", ".join(sorted(f.path for f in findings))[:200],
        }
    decision = route_issue_with_fallbacks(issue, project)
    if decision.status != "RESOLVED":
        allowed = [r["repository"] for r in project["repositories"]]
        if repository_override and repository_override in allowed:
            decision = RouteDecision(
                "RESOLVED",
                repository=repository_override,
                basis=f"manual override by console user (was: {decision.basis})"[:240],
                confidence=100,
                candidates=allowed,
            )
        else:
            return {"result": "failed", "detail": f"路由未决: {decision.basis}"}
    outcome = _dispatch(intake, decision)
    _append_shadow(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "issue": issue_key,
            "project": project_key,
            "summary": intake["summary"][:120],
            "excerpt": intake["problem"]["background"][:300],
            "url": intake["source_url"],
            "severity": intake["severity"],
            "decision": decision.status,
            "repository": decision.repository,
            "basis": decision.basis,
            "confidence": decision.confidence,
            "dispatch": outcome,
            "manual": True,
        }
    )
    return outcome


def poll(
    dispatch: bool = False,
    include_backlog: bool = False,
    config_path: Path = CONFIG_PATH,
    state_path: Path = STATE_PATH,
    shadow_path: Path = SHADOW_LOG_PATH,
) -> Dict[str, Any]:
    config = load_config(config_path)
    base, _cookie = _require_env()
    _verify_session()
    state = _load_state(state_path)
    results: List[Dict[str, Any]] = []

    for project_key, project in sorted(config["projects"].items()):
        if not project.get("enabled"):
            continue
        project_state = state["projects"].setdefault(project_key, {})

        # First run initializes the watermark to "now" and skips the backlog,
        # so enabling a large project never floods the pipeline.
        if "watermark" not in project_state and not include_backlog:
            project_state["watermark_epoch"] = time.time()
            project_state["watermark"] = _format_watermark(time.time())
            project_state.setdefault("seen", {})
            _append_shadow(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "project": project_key,
                    "decision": "WATERMARK_INIT",
                    "basis": "backlog skipped; only issues updated after this point are polled",
                },
                shadow_path,
            )
            continue

        dispatch_budget = int(project.get("max_dispatch_per_poll", 1))
        watermark = project_state.get("watermark", "")
        jql = build_jql(project_key, project, watermark)
        path = (
            "/rest/api/2/search?jql="
            + urllib.parse.quote(jql)
            + f"&maxResults={MAX_SEARCH_RESULTS}"
            + f"&fields={ISSUE_DETAIL_FIELDS}"
        )
        data = _get_json(path)
        for issue in data.get("issues", []):
            key = issue.get("key", "")
            if not ISSUE_KEY_PATTERN.fullmatch(key):
                continue
            if key in project_state.get("seen", {}):
                continue
            if not _issue_type_allowed(issue, project):
                continue
            intake = issue_to_intake(issue, base, project)
            findings = find_sensitive_data(
                {name: value for name, value in intake.items() if not name.startswith("_")}
            )
            if findings:
                decision = RouteDecision(
                    "BLOCKED_SENSITIVE",
                    basis="sensitive data at: "
                    + ", ".join(sorted(f"{f.path}" for f in findings))[:240],
                )
            else:
                decision = route_issue_with_fallbacks(issue, project)
            record: Dict[str, Any] = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "issue": key,
                "project": project_key,
                "summary": intake["summary"][:120],
                "excerpt": intake["problem"]["background"][:300],
                "url": intake["source_url"],
                "severity": intake["severity"],
                "decision": decision.status,
                "repository": decision.repository,
                "basis": decision.basis,
                "confidence": decision.confidence,
            }
            auto_eligible = (
                decision.status == "RESOLVED"
                and project.get("auto_dispatch")
                and decision.confidence >= 100
            )
            auto_eligible = (
                decision.status == "RESOLVED"
                and project.get("auto_dispatch")
                and decision.confidence >= 100
            )
            if dispatch and auto_eligible and dispatch_budget > 0:
                dispatch_budget -= 1
                outcome = _dispatch(intake, decision)
                record["dispatch"] = outcome
            elif dispatch and auto_eligible:
                record["dispatch"] = {"result": "over_budget"}
            elif dispatch and decision.status == "RESOLVED" and not project.get("auto_dispatch"):
                record["dispatch"] = {"result": "held", "detail": "auto_dispatch is false"}
            else:
                record["dispatch"] = {"result": "shadow"}
            # Mark every processed issue as seen regardless of dispatch outcome,
            # so shadow runs never reprocess the same issue.
            project_state.setdefault("seen", {}).setdefault(
                key, record["dispatch"].get("taskId") or record["dispatch"]["result"]
            )
            _append_shadow(record, shadow_path)
            results.append(record)

            updated = _text((issue.get("fields") or {}).get("updated"), 40)
            if updated:
                epoch = _parse_jira_time(updated)
                if epoch > project_state.get("watermark_epoch", 0):
                    project_state["watermark_epoch"] = epoch
                    project_state["watermark"] = _format_watermark(epoch)

    _atomic_write_json(state_path, state)
    return {"polled": datetime.now(timezone.utc).isoformat(), "issues": results}


# ---------------------------------------------------------------------------
# CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Jira connector (shadow-first).")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("survey", help="Dump project/component/priority metadata to .jira-survey.json")
    poll_parser = sub.add_parser("poll", help="Poll configured projects")
    poll_parser.add_argument(
        "--dispatch",
        action="store_true",
        help="Actually create tasks for auto-eligible issues (default: shadow only)",
    )
    poll_parser.add_argument(
        "--include-backlog",
        action="store_true",
        help="Process the existing backlog instead of initializing the watermark to now",
    )
    poll_parser.add_argument(
        "--interval-seconds",
        type=int,
        default=0,
        help=f"Repeat forever at this interval ({MIN_INTERVAL_SECONDS}-{MAX_INTERVAL_SECONDS}s); 0 = run once",
    )
    return parser


def _summarize(result: Dict[str, Any]) -> str:
    counts: Dict[str, int] = {}
    for record in result["issues"]:
        counts[record["decision"]] = counts.get(record["decision"], 0) + 1
    return f"polled: {len(result['issues'])} new issues; decisions: {counts}"


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "survey":
            report = survey()
            print(
                f"surveyed {len(report['projects'])} projects as {report['user'].get('name')} "
                f"-> {SURVEY_PATH}"
            )
            return 0
        if args.interval_seconds:
            if not MIN_INTERVAL_SECONDS <= args.interval_seconds <= MAX_INTERVAL_SECONDS:
                raise ValueError(
                    f"--interval-seconds must be between {MIN_INTERVAL_SECONDS} and {MAX_INTERVAL_SECONDS}"
                )
            while True:
                print(_summarize(poll(dispatch=args.dispatch)), flush=True)
                time.sleep(args.interval_seconds)
        print(
            _summarize(
                poll(dispatch=args.dispatch, include_backlog=args.include_backlog)
            )
        )
        return 0
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
