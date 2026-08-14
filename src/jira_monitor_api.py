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

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

from src import jira_connector
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
MAX_BODY_BYTES = 4096
DEFAULT_SCAN_INTERVAL_SECONDS = 300
MIN_SCAN_INTERVAL_SECONDS = 60

_AUTO_SCAN_LOCK = threading.Lock()
_AUTO_SCAN_STATE: Dict[str, Any] = {"lastRunAt": None, "lastResult": None, "lastError": None}


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


def _projects_view(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    view = []
    for key, project in sorted(config["projects"].items()):
        view.append(
            {
                "key": key,
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
                result = poll(dispatch=True)
            _AUTO_SCAN_STATE.update(
                {
                    "lastRunAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "lastResult": f"{len(result['issues'])} new",
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
        config = load_config(CONFIG_PATH)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        return {"status": "config_error", "detail": str(exc)}
    state: Dict[str, Any] = {"projects": {}}
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    issues = _read_shadow_issues()
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
        "servedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _scan() -> Dict[str, Any]:
    try:
        result = poll(dispatch=False)
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
