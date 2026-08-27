"""HTTP API for the Jira monitor console panel (loopback only).

Mirrors src/log_monitor_api.py's shape: read shadow-log state, trigger a
connector scan, dispatch one issue on click, and edit per-project rules.

Endpoints (all loopback-only by construction of the bind address):
- GET  /jira-monitor          -> status + project config + recent scanned issues
- POST /jira-monitor/scan     -> run one shadow poll and return fresh results
- POST /jira-monitor/dispatch -> {"issue": "KEYB-123"} one-click task creation
- POST /jira-monitor/rules    -> {"project": "KEYB", "enabled": true, "autoDispatch": false}
"""

from __future__ import annotations

import copy
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

from src import jira_connector, scan_coordination
from src.jira_connector import (
    CONFIG_PATH,
    SHADOW_LOG_PATH,
    STATE_PATH,
    dispatch_issue,
    load_config,
    poll,
)

HOST = "127.0.0.1"
PORT = 8098
MAX_SHADOW_ISSUES = 200
MAX_TASK_RESPONSE_BYTES = 1_100_000
MAX_BODY_BYTES = 4096
DEFAULT_SCAN_INTERVAL_SECONDS = 300
MIN_SCAN_INTERVAL_SECONDS = 60
WORKFLOW_STATUS_CACHE_TTL_SECONDS = 60
APP_ENV = os.environ.get("APP_ENV", "local").strip().lower()
if APP_ENV not in {"local", "staging", "production"}:
    raise ValueError("APP_ENV must be local, staging, or production")
ENV_PATH = Path(f".env.{APP_ENV}")
JIRA_ENV_KEYS = (
    "JIRA_BASE_URL",
    "JIRA_SESSION_COOKIE",
    "JIRA_USERNAME",
    "JIRA_PASSWORD",
)

_AUTO_SCAN_LOCK = threading.Lock()
_AUTO_SCAN_STATE: Dict[str, Any] = {"lastRunAt": None, "lastResult": None, "lastError": None}
_WORKFLOW_STATUS_LOCK = threading.Lock()
_WORKFLOW_STATUS_CACHE: Dict[str, Dict[str, Any]] = {}
_WORKFLOW_STATUS_REFRESHED_AT = 0.0


def _poll_with_session_refresh(
    dispatch: bool,
    state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    poll_options: Dict[str, Any] = {"dispatch": dispatch}
    if state is not None:
        poll_options.update(
            {
                "state": state,
                "persist_state": False,
                "return_checkpoint": True,
            }
        )
    try:
        return poll(**poll_options)
    except jira_connector.JiraAuthError as auth_error:
        try:
            from src import jira_session_refresh

            jira_session_refresh.refresh_session()
        except Exception as refresh_error:
            raise RuntimeError(
                f"{auth_error}（自动续期失败：{refresh_error}）"
            ) from refresh_error
        return poll(**poll_options)


def _local_checkpoint() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return {"version": 1, "projects": {}}
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {"version": 1, "projects": {}}
    if not isinstance(state, dict) or not isinstance(state.get("projects"), dict):
        return {"version": 1, "projects": {}}
    return state


def _coordinated_poll(dispatch: bool) -> Dict[str, Any]:
    lease = scan_coordination.acquire("JIRA")
    if not lease.get("acquired"):
        return {
            "issues": [],
            "coordination": {
                "acquired": False,
                "leaseExpiresAt": lease.get("leaseExpiresAt"),
            },
        }
    token = lease.get("leaseToken")
    if not isinstance(token, str) or not token:
        raise RuntimeError("control plane returned an invalid Jira lease")
    checkpoint = lease.get("checkpoint")
    if not isinstance(checkpoint, dict) or not isinstance(
        checkpoint.get("projects"), dict
    ):
        checkpoint = _local_checkpoint()
    starting_checkpoint = copy.deepcopy(checkpoint)
    working_checkpoint = copy.deepcopy(checkpoint)
    try:
        result = _poll_with_session_refresh(
            dispatch=dispatch, state=working_checkpoint
        )
        completed_checkpoint = result.pop("checkpoint", working_checkpoint)
        scan_coordination.complete("JIRA", token, completed_checkpoint)
        result["coordination"] = {"acquired": True}
        return result
    except Exception as exc:
        try:
            scan_coordination.complete(
                "JIRA", token, starting_checkpoint, error=str(exc)[:500]
            )
        except Exception as release_error:
            raise RuntimeError(
                f"{type(exc).__name__}; Jira scan lease release failed"
            ) from release_error
        raise


def _read_shadow_issues(path: Path = SHADOW_LOG_PATH) -> List[Dict[str, Any]]:
    """Latest record per issue key, newest first. Watermark markers excluded."""
    if not path.exists():
        return []
    latest: Dict[str, Dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = record.get("issue")
        if isinstance(key, str) and key:
            latest[key] = record
    issues = sorted(latest.values(), key=lambda r: r.get("ts", ""), reverse=True)
    return issues[:MAX_SHADOW_ISSUES]


def _read_persisted_issues() -> List[Dict[str, Any]]:
    request = urllib.request.Request(
        scan_coordination.control_plane_base() + "/api/tasks?sourceType=JIRA",
        headers={"Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        raw = response.read(MAX_TASK_RESPONSE_BYTES + 1)
    if len(raw) > MAX_TASK_RESPONSE_BYTES:
        raise RuntimeError("Jira task history response is too large")
    tasks = json.loads(raw.decode("utf-8"))
    if not isinstance(tasks, list):
        raise RuntimeError("Jira task history response is invalid")

    jira_base_url = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
    issues = []
    for task in tasks:
        if not isinstance(task, dict) or task.get("sourceType") != "JIRA":
            continue
        issue_key = task.get("sourceReference")
        task_id = task.get("id")
        if (
            not isinstance(issue_key, str)
            or not jira_connector.ISSUE_KEY_PATTERN.fullmatch(issue_key)
            or not isinstance(task_id, str)
            or not task_id
        ):
            continue
        repository = task.get("matchedRepository")
        repository = repository if isinstance(repository, str) else ""
        confidence = task.get("routingConfidence")
        issues.append(
            {
                "ts": task.get("createdAt") or "",
                "issue": issue_key,
                "project": issue_key.split("-", 1)[0],
                "summary": task.get("inputSummary") or issue_key,
                "url": f"{jira_base_url}/browse/{issue_key}" if jira_base_url else "",
                "severity": "",
                "decision": "RESOLVED" if repository else "NEEDS_CONTEXT",
                "repository": repository,
                "basis": task.get("routingBasis") or "",
                "confidence": confidence if isinstance(confidence, int) else 0,
                "dispatch": {
                    "result": "created",
                    "taskId": task_id,
                    "taskStatus": task.get("status"),
                },
            }
        )
    return issues


def _merge_issues(
    persisted: List[Dict[str, Any]],
    shadow: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    latest = {
        issue["issue"]: issue
        for issue in persisted
        if isinstance(issue.get("issue"), str)
    }
    for issue in shadow:
        key = issue.get("issue")
        if not isinstance(key, str):
            continue
        existing = latest.get(key, {})
        merged = {**existing, **issue}
        if existing.get("dispatch") and not issue.get("dispatch"):
            merged["dispatch"] = existing["dispatch"]
        latest[key] = merged
    return sorted(
        latest.values(),
        key=lambda issue: issue.get("ts", ""),
        reverse=True,
    )[:MAX_SHADOW_ISSUES]


def _enrich_workflow_statuses(
    issues: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    global _WORKFLOW_STATUS_CACHE, _WORKFLOW_STATUS_REFRESHED_AT

    issue_keys = sorted(
        {
            issue["issue"]
            for issue in issues
            if isinstance(issue.get("issue"), str)
            and jira_connector.ISSUE_KEY_PATTERN.fullmatch(issue["issue"])
        }
    )
    if not issue_keys:
        return issues

    with _WORKFLOW_STATUS_LOCK:
        now = time.monotonic()
        missing = any(key not in _WORKFLOW_STATUS_CACHE for key in issue_keys)
        stale = now - _WORKFLOW_STATUS_REFRESHED_AT >= WORKFLOW_STATUS_CACHE_TTL_SECONDS
        if missing or stale:
            refreshed: Dict[str, Dict[str, Any]] = {}
            for offset in range(0, len(issue_keys), 50):
                batch = issue_keys[offset : offset + 50]
                jql = "issuekey in (" + ",".join(batch) + ")"
                path = (
                    "/rest/api/2/search?jql="
                    + urllib.parse.quote(jql)
                    + f"&maxResults={len(batch)}&fields=status"
                )
                payload = jira_connector._get_json(path)
                for jira_issue in payload.get("issues", []):
                    key = jira_issue.get("key")
                    if isinstance(key, str) and key in batch:
                        refreshed[key] = jira_connector.issue_workflow_status(jira_issue)
            _WORKFLOW_STATUS_CACHE = refreshed
            _WORKFLOW_STATUS_REFRESHED_AT = now
        cache = dict(_WORKFLOW_STATUS_CACHE)

    enriched = []
    for issue in issues:
        workflow = cache.get(issue.get("issue"))
        enriched.append({**issue, **workflow} if workflow else issue)
    return enriched


def _projects_view(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    view = []
    for key, project in sorted(config["projects"].items()):
        view.append(
            {
                "key": key,
                "name": project.get("name") or key,
                "enabled": bool(project.get("enabled")),
                "autoDispatch": bool(project.get("auto_dispatch")),
                "issueTypes": project.get("issue_types") or [],
                "repositories": [r["repository"] for r in project.get("repositories", [])],
                "maxDispatchPerPoll": int(project.get("max_dispatch_per_poll", 1)),
            }
        )
    return view


def _auto_scan_loop(interval: int) -> None:
    """Periodically poll Jira; honors each project's auto_dispatch config."""
    while True:
        try:
            with _AUTO_SCAN_LOCK:
                result = _coordinated_poll(dispatch=True)
            _AUTO_SCAN_STATE.update(
                {
                    "lastRunAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "lastResult": (
                        f"{len(result['issues'])} new"
                        if result.get("coordination", {}).get("acquired")
                        else "lease held by another instance"
                    ),
                    "lastError": None,
                }
            )
        except Exception as exc:  # noqa: BLE001 - the loop must never die
            _AUTO_SCAN_STATE.update(
                {
                    "lastRunAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "lastResult": None,
                    "lastError": str(exc),
                }
            )
        time.sleep(interval)


def _status_payload() -> Dict[str, Any]:
    try:
        config = jira_connector.load_routing_config(CONFIG_PATH)
    except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as exc:
        return {"status": "config_error", "detail": str(exc)}
    try:
        coordination = scan_coordination.status("JIRA")
        state = coordination.get("checkpoint")
    except (OSError, RuntimeError, ValueError):
        coordination = {"lastError": "scan coordination unavailable"}
        state = None
    if not isinstance(state, dict) or not isinstance(state.get("projects"), dict):
        state = {"projects": {}}
    history_error = None
    try:
        persisted_issues = _read_persisted_issues()
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        persisted_issues = []
        history_error = str(exc)
    issues = _merge_issues(persisted_issues, _read_shadow_issues())
    workflow_status_error = None
    try:
        issues = _enrich_workflow_statuses(issues)
    except (
        jira_connector.JiraAuthError,
        RuntimeError,
        ValueError,
        OSError,
    ) as exc:
        workflow_status_error = str(exc)
    watermarks = {
        key: value.get("watermark")
        for key, value in (state.get("projects") or {}).items()
        if isinstance(value, dict) and value.get("watermark")
    }
    counts: Dict[str, int] = {}
    for issue in issues:
        decision = issue.get("decision", "?")
        counts[decision] = counts.get(decision, 0) + 1
    return {
        "status": "ok",
        "issues": issues,
        "projects": _projects_view(config),
        "watermarks": watermarks,
        "counts": counts,
        "autoScan": dict(_AUTO_SCAN_STATE),
        "workflowStatusError": workflow_status_error,
        "taskHistoryError": history_error,
        "coordination": {
            "leaseExpiresAt": coordination.get("leaseExpiresAt"),
            "lastStartedAt": coordination.get("lastStartedAt"),
            "lastCompletedAt": coordination.get("lastCompletedAt"),
            "lastError": coordination.get("lastError"),
        },
        "servedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _scan() -> Dict[str, Any]:
    try:
        with _AUTO_SCAN_LOCK:
            result = _coordinated_poll(dispatch=False)
    except (RuntimeError, ValueError, OSError) as exc:
        return {"status": "error", "detail": str(exc)}
    payload = _status_payload()
    payload["lastScan"] = {
        "newIssues": len(result["issues"]),
        "decisions": [r for r in result["issues"]],
    }
    return payload


def _update_rules(body: Dict[str, Any]) -> Dict[str, Any]:
    project_key = body.get("project")
    if not isinstance(project_key, str) or not project_key:
        return {"status": "error", "detail": "project is required"}
    config = load_config(CONFIG_PATH)
    project = config["projects"].get(project_key)
    if project is None:
        return {"status": "error", "detail": f"unknown project {project_key}"}
    if "enabled" in body:
        project["enabled"] = bool(body["enabled"])
    if "autoDispatch" in body:
        project["autoDispatch"] = bool(body["autoDispatch"])
        project["auto_dispatch"] = bool(body["autoDispatch"])
        project.pop("autoDispatch", None)
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    raw["projects"][project_key] = project
    CONFIG_PATH.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    load_config(CONFIG_PATH)  # fail fast if the write broke the schema
    return _status_payload()


def _dispatch_one(body: Dict[str, Any]) -> Dict[str, Any]:
    issue_key = body.get("issue")
    if not isinstance(issue_key, str) or not issue_key:
        return {"result": "failed", "detail": "issue is required"}
    override = body.get("repository")
    if not isinstance(override, str):
        override = ""
    try:
        return dispatch_issue(issue_key, repository_override=override.strip())
    except (RuntimeError, ValueError, OSError) as exc:
        return {"result": "failed", "detail": str(exc)}


def _reload_jira_environment(path: Path = ENV_PATH) -> Dict[str, Any]:
    if not path.exists():
        raise RuntimeError(f"{path} does not exist")
    if path.is_symlink():
        raise RuntimeError(f"{path} must not be a symlink")
    if path.stat().st_size > 256_000:
        raise RuntimeError(f"{path} is too large")
    values: Dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if key not in JIRA_ENV_KEYS:
            continue
        value = raw_value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {'"', "'"}
        ):
            value = value[1:-1]
        values[key] = value
    for key in JIRA_ENV_KEYS:
        if key in values:
            os.environ[key] = values[key]
        else:
            os.environ.pop(key, None)
    return {
        "status": "reloaded",
        "baseUrlConfigured": bool(values.get("JIRA_BASE_URL")),
        "sessionConfigured": bool(values.get("JIRA_SESSION_COOKIE")),
        "credentialsConfigured": bool(
            values.get("JIRA_USERNAME") and values.get("JIRA_PASSWORD")
        ),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "JiraMonitor/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # quiet
        return

    def _send(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] == "/jira-monitor":
            self._send(200, _status_payload())
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/jira-monitor/scan":
            self._send(200, _scan())
            return
        if path == "/jira-monitor/session/reload":
            try:
                self._send(200, _reload_jira_environment())
            except (RuntimeError, OSError, UnicodeError) as exc:
                self._send(400, {"status": "error", "detail": str(exc)})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._send(413, {"error": "body too large"})
            return
        body: Dict[str, Any] = {}
        if length:
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send(400, {"error": "invalid JSON body"})
                return
        if path == "/jira-monitor/dispatch":
            self._send(200, _dispatch_one(body))
            return
        if path == "/jira-monitor/rules":
            self._send(200, _update_rules(body))
            return
        self._send(404, {"error": "not found"})


def main() -> int:
    interval = int(
        os.environ.get("JIRA_MONITOR_SCAN_INTERVAL", DEFAULT_SCAN_INTERVAL_SECONDS)
    )
    interval = max(interval, MIN_SCAN_INTERVAL_SECONDS)
    scanner = threading.Thread(target=_auto_scan_loop, args=(interval,), daemon=True)
    scanner.start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(
        f"jira monitor api on http://{HOST}:{PORT}/jira-monitor"
        f" (auto-scan every {interval}s)",
        flush=True,
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
