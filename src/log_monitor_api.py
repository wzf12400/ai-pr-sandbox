"""Read-only log-platform monitor API for the local console frontend.

Serves one bounded, read-only scan endpoint on the loopback interface. It
reuses the existing OpenSearch Dashboards client, sanitizer, and deterministic
incident grouper. Raw hits stay in process memory; only sanitized aggregates
are returned. Passwords come from macOS Keychain and are never written to the
response, disk, or logs.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import kibana_incident_grouper, kibana_issue_connector, kibana_sanitizer
from src.log_task_ingestor import _load_local_source_settings
from src.terminal_control_center import _load_keychain_log_password

HOST = "127.0.0.1"
PORT = 8099
FETCH_SIZE = 100
MAX_BATCHES = 5
TARGET_ERROR_EVENTS = 50
CACHE_TTL_SECONDS = 30
MAX_MESSAGE = 240

CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8080")
RULES_PATH = Path(".issue-entry-state/log-monitor-rules.json")
AUTOMATION_STATE_PATH = Path(".issue-entry-state/log-monitor-automation.json")
DEFAULT_RULES = {
    "enabled": True,
    "minGroupEvents": 10,
    "maxTasksPerScan": 3,
    "note": "聚类事件数达到 minGroupEvents 时自动提交控制面任务，"
    "触发 Issue 生成与代码修改门禁流程",
}

_cache: Dict[str, Any] = {"at": 0.0, "payload": None}


def _text(value: Any, limit: int = MAX_MESSAGE) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


_ERROR_LEVEL_PATTERN = re.compile(r"\bERROR\b")


def _quick_error_level(hit: Dict[str, Any]) -> bool:
    """Cheap pre-check used only for the pagination stop decision.

    The authoritative level filter still runs on the sanitized event; this
    only estimates whether a raw hit is likely ERROR level so the scan knows
    when to stop paging through older windows.
    """
    source = hit.get("_source")
    if not isinstance(source, dict):
        return False
    message = source.get("message")
    if not isinstance(message, str):
        return False
    return bool(_ERROR_LEVEL_PATTERN.search(message[:500]))


def _load_hmac_key() -> str:
    env_value = os.environ.get("LOG_SANITIZER_HMAC_KEY", "").strip()
    if env_value:
        return env_value
    key_file = Path(".issue-entry-state/log-sanitizer-hmac.key")
    try:
        if key_file.is_file() and not key_file.is_symlink():
            if key_file.stat().st_mode & 0o077 == 0:
                return key_file.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return ""


def _scan_payload() -> Dict[str, Any]:
    settings = _load_local_source_settings()
    if not settings:
        return {
            "status": "not_configured",
            "detail": "尚未配置日志平台连接",
            "configure": "./bin/log-platform-to-tasks --configure "
            "--discover-url 'FULL_DISCOVER_URL' --username 'READ_ONLY_USER'",
        }

    hmac_key = _load_hmac_key()
    if len(hmac_key.encode("utf-8")) < kibana_sanitizer.MIN_HMAC_KEY_BYTES:
        return {
            "status": "no_hmac_key",
            "detail": "缺少 LOG_SANITIZER_HMAC_KEY（至少 32 字节），无法脱敏扫描",
        }

    discover_url = settings["discover_url"]
    username = settings["username"]
    password = _load_keychain_log_password(discover_url, username)
    if not password:
        return {
            "status": "no_credentials",
            "detail": "macOS Keychain 中没有该日志平台的只读密码",
        }

    target = kibana_issue_connector.parse_discover_url(discover_url)
    credentials = kibana_issue_connector.DashboardCredentials(
        username=username, password=password
    )
    client = kibana_issue_connector.OpenSearchDashboardsClient(target, credentials)
    index_pattern, time_field = client.resolve_index_pattern()

    # 只保留 ERROR 级别：INFO 噪音（如 "error count:0"）会占满单批次配额，
    # 因此分页向更旧窗口拉取，直到凑够目标 ERROR 数或达到批次数上限。
    hits: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    error_kept = 0
    time_to: Optional[str] = None
    for _ in range(MAX_BATCHES):
        batch = client.fetch_latest_error_hits(
            index_pattern, time_field, FETCH_SIZE, time_to=time_to
        )
        fresh = []
        for hit in batch:
            doc_id = str(hit.get("_id") or "")
            if doc_id and doc_id in seen_ids:
                continue
            if doc_id:
                seen_ids.add(doc_id)
            fresh.append(hit)
        hits.extend(fresh)
        batch_ts = [
            str((h.get("_source") or {}).get("@timestamp") or "")
            for h in fresh
            if isinstance(h.get("_source"), dict)
        ]
        batch_ts = [t for t in batch_ts if t]
        if not batch_ts:
            break
        time_to = min(batch_ts)
        error_kept = sum(1 for h in hits if _quick_error_level(h))
        if error_kept >= TARGET_ERROR_EVENTS:
            break

    namespaces: Dict[str, int] = {}
    services: Dict[str, int] = {}
    timestamps: List[str] = []
    sanitized_events: List[Dict[str, Any]] = []
    blocked_count = 0
    non_error_count = 0

    for hit in hits:
        source = hit.get("_source") if isinstance(hit, dict) else None
        if not isinstance(source, dict):
            continue
        ts = _text(source.get("@timestamp"), 40)
        if ts:
            timestamps.append(ts)
        try:
            event = kibana_sanitizer.sanitize_hit(
                hit, hmac_key.encode("utf-8"), include_aggregation_refs=True
            )
        except Exception:
            blocked_count += 1
            continue
        sanitization = event.get("sanitization")
        if not isinstance(sanitization, dict) or not sanitization.get("ai_allowed"):
            blocked_count += 1
            continue
        event_section = event.get("event")
        level = (
            _text(event_section.get("level"), 16).upper()
            if isinstance(event_section, dict)
            else ""
        )
        if level != "ERROR":
            non_error_count += 1
            continue
        target = event.get("target") if isinstance(event.get("target"), dict) else {}
        namespace = _text(target.get("namespace"), 80)
        if namespace:
            namespaces[namespace] = namespaces.get(namespace, 0) + 1
        service = _text(target.get("service"), 80)
        if service:
            services[service] = services.get(service, 0) + 1
        sanitized_events.append(event)

    incidents = (
        kibana_incident_grouper.group_sanitized_events(sanitized_events)
        if sanitized_events
        else []
    )

    incident_views: List[Dict[str, Any]] = []
    for incident in incidents[:50]:
        source = incident.get("source") or {}
        statistics = incident.get("statistics") or {}
        grouping = incident.get("grouping") or {}
        members = incident.get("members") or []
        member_services = sorted(
            {
                _text((m.get("target") or {}).get("service"), 80)
                for m in members
                if isinstance(m, dict) and isinstance(m.get("target"), dict)
            }
            - {""}
        )
        summaries = [
            _text((m.get("event") or {}).get("summary"), 140)
            for m in members
            if isinstance(m, dict) and isinstance(m.get("event"), dict)
        ]
        member_views = []
        for m in members[:20]:
            if not isinstance(m, dict):
                continue
            m_source = m.get("source") if isinstance(m.get("source"), dict) else {}
            m_event = m.get("event") if isinstance(m.get("event"), dict) else {}
            member_views.append(
                {
                    "timestamp": _text(m_source.get("timestamp"), 40),
                    "level": _text(m_event.get("level"), 16),
                    "summary": _text(m_event.get("summary"), 200),
                    "traceRef": _text(m_event.get("trace_ref"), 60),
                }
            )
        incident_views.append(
            {
                "incidentRef": _text(source.get("incident_ref"), 60),
                "eventCount": (incident.get("incident") or {}).get("event_count", 0),
                "firstSeenAt": _text(source.get("first_seen_at"), 40),
                "lastSeenAt": _text(source.get("last_seen_at"), 40),
                "strategy": _text(grouping.get("strategy"), 40),
                "services": member_services[:5],
                "affectedEndpoints": list(statistics.get("affected_endpoints") or [])[:8],
                "affectedUserCount": statistics.get("affected_user_count"),
                "summary": summaries[0] if summaries else "",
                "members": member_views,
            }
        )

    return {
        "status": "ok",
        "scannedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "indexPattern": index_pattern,
        "fetchSize": len(hits),
        "projectsScanned": len(namespaces),
        "namespaces": [
            {"name": name, "errors": count}
            for name, count in sorted(
                namespaces.items(), key=lambda item: item[1], reverse=True
            )[:20]
        ],
        "services": [
            {"name": name, "errors": count}
            for name, count in sorted(
                services.items(), key=lambda item: item[1], reverse=True
            )[:20]
        ],
        "errorEvents": len(sanitized_events),
        "blockedEvents": blocked_count,
        "skippedNonError": non_error_count,
        "incidentGroups": len(incidents),
        "window": {
            "from": min(timestamps) if timestamps else None,
            "to": max(timestamps) if timestamps else None,
        },
        "incidents": incident_views,
        "automation": _apply_automation_rules(incident_views),
    }


def _load_rules() -> Dict[str, Any]:
    try:
        if RULES_PATH.is_file() and not RULES_PATH.is_symlink():
            if RULES_PATH.stat().st_size <= 16_384:
                payload = json.loads(RULES_PATH.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    merged = dict(DEFAULT_RULES)
                    merged.update(
                        {
                            key: payload[key]
                            for key in ("enabled", "minGroupEvents", "maxTasksPerScan")
                            if key in payload
                        }
                    )
                    merged["enabled"] = bool(merged["enabled"])
                    merged["minGroupEvents"] = max(
                        1, min(int(merged["minGroupEvents"]), 10_000)
                    )
                    merged["maxTasksPerScan"] = max(
                        1, min(int(merged["maxTasksPerScan"]), 10)
                    )
                    return merged
    except (OSError, ValueError, TypeError):
        pass
    return dict(DEFAULT_RULES)


def _load_automation_state() -> Dict[str, Any]:
    try:
        if AUTOMATION_STATE_PATH.is_file() and not AUTOMATION_STATE_PATH.is_symlink():
            if AUTOMATION_STATE_PATH.stat().st_size <= 1_000_000:
                payload = json.loads(AUTOMATION_STATE_PATH.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and isinstance(
                    payload.get("dispatched"), dict
                ):
                    return payload
    except (OSError, ValueError):
        pass
    return {"version": 1, "dispatched": {}}


def _save_automation_state(state: Dict[str, Any]) -> None:
    try:
        AUTOMATION_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = AUTOMATION_STATE_PATH.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.chmod(0o600)
        tmp.replace(AUTOMATION_STATE_PATH)
    except OSError:
        pass


def _loopback_control_plane_url() -> Optional[str]:
    base = CONTROL_PLANE_URL.rstrip("/")
    if not base.startswith(("http://127.0.0.1", "http://localhost")):
        return None
    return base


def _dispatch_incident_task(
    incident_view: Dict[str, Any], rules: Dict[str, Any]
) -> Dict[str, Any]:
    """Submit one over-threshold incident to the control plane (loopback only).

    The Java side applies its own gates: routing, MOCK execution, and the
    disabled-by-default Issue/Copilot/Draft-PR write policies. A repeated
    sourceReference reuses the existing task server-side.
    """
    base = _loopback_control_plane_url()
    if base is None:
        return {"result": "skipped", "detail": "控制面地址不是本机回环，已跳过"}
    payload = {
        "sourceType": "LOG",
        "input": (incident_view.get("summary") or "")[:400]
        or f"日志故障 {incident_view['incidentRef']}",
        "logIncident": {
            "dataSafetyStatus": "SANITIZED",
            "sourceReference": incident_view["incidentRef"],
            "firstSeenAt": incident_view["firstSeenAt"],
            "lastSeenAt": incident_view["lastSeenAt"],
            "currentScanEventCount": incident_view["eventCount"],
            "historicalEventCount": incident_view["eventCount"],
            "incidentGroupCount": 1,
            "affectedEndpoints": incident_view.get("affectedEndpoints") or [],
            "affectedUserCountMin": None,
            "affectedUserCountMax": None,
            "userIdentifierEventCount": 0,
            "historicalCountComplete": True,
            "aggregationBasis": (
                f"auto-rule: group_events>={rules['minGroupEvents']}; "
                f"services={','.join(incident_view.get('services') or [])[:120]}"
            )[:240],
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
        return {"result": "failed", "detail": "控制面响应无效"}
    return {
        "result": "created",
        "taskId": task_id,
        "taskStatus": body.get("status"),
        "matchedRepository": body.get("matchedRepository"),
    }


def _apply_automation_rules(
    incident_views: List[Dict[str, Any]],
) -> Dict[str, Any]:
    rules = _load_rules()
    over_threshold = [
        view
        for view in incident_views
        if view["eventCount"] >= rules["minGroupEvents"]
        and view["firstSeenAt"]
        and view["lastSeenAt"]
    ]
    automation: Dict[str, Any] = {
        "rules": {
            "enabled": rules["enabled"],
            "minGroupEvents": rules["minGroupEvents"],
            "maxTasksPerScan": rules["maxTasksPerScan"],
        },
        "overThreshold": len(over_threshold),
        "dispatched": [],
    }
    if not rules["enabled"] or not over_threshold:
        return automation

    state = _load_automation_state()
    dispatched = state["dispatched"]
    budget = rules["maxTasksPerScan"]
    for view in over_threshold:
        ref = view["incidentRef"]
        if ref in dispatched:
            automation["dispatched"].append(
                {"incidentRef": ref, "result": "already_dispatched",
                 "taskId": dispatched[ref].get("taskId")}
            )
            continue
        if budget <= 0:
            automation["dispatched"].append(
                {"incidentRef": ref, "result": "over_budget"}
            )
            continue
        budget -= 1
        outcome = _dispatch_incident_task(view, rules)
        outcome["incidentRef"] = ref
        automation["dispatched"].append(outcome)
        if outcome["result"] == "created":
            dispatched[ref] = {
                "taskId": outcome["taskId"],
                "dispatchedAt": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                ),
            }
    _save_automation_state(state)
    return automation


def _get_scan() -> Dict[str, Any]:
    now = time.time()
    if _cache["payload"] is not None and now - _cache["at"] < CACHE_TTL_SECONDS:
        return _cache["payload"]
    try:
        payload = _scan_payload()
    except Exception as exception:  # bounded: never leak internals
        payload = {
            "status": "error",
            "detail": f"扫描失败：{type(exception).__name__}",
        }
    _cache["at"] = now
    _cache["payload"] = payload
    return payload


_ISSUE_PATH = re.compile(
    r"^/issue/(?P<owner>[A-Za-z0-9_.-]{1,100})/(?P<repo>[A-Za-z0-9_.-]{1,100})/(?P<number>[0-9]{1,9})$"
)
MAX_ISSUE_BODY_CHARS = 8000


def _get_issue(owner: str, repo: str, number: str) -> Dict[str, Any]:
    """Read one GitHub Issue through the local user's authenticated gh CLI."""
    try:
        result = subprocess.run(
            [
                "gh",
                "issue",
                "view",
                number,
                "--repo",
                f"{owner}/{repo}",
                "--json",
                "number,title,state,url,labels,body",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "error", "detail": "gh 调用失败"}
    if result.returncode != 0:
        return {"status": "error", "detail": "无法读取该 Issue"}
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {"status": "error", "detail": "gh 响应无效"}
    labels = [
        str(label.get("name"))[:60]
        for label in payload.get("labels") or []
        if isinstance(label, dict) and label.get("name")
    ][:10]
    body = str(payload.get("body") or "")
    return {
        "status": "ok",
        "number": payload.get("number"),
        "title": str(payload.get("title") or "")[:300],
        "state": str(payload.get("state") or "")[:20],
        "url": str(payload.get("url") or "")[:300],
        "labels": labels,
        "body": body[:MAX_ISSUE_BODY_CHARS],
        "bodyTruncated": len(body) > MAX_ISSUE_BODY_CHARS,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "LogMonitor/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # quiet
        return

    def _send(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path in ("/log-monitor", "/log-monitor/scan"):
            self._send(200, _get_scan())
        elif self.path == "/log-monitor/refresh":
            _cache["at"] = 0.0
            self._send(200, _get_scan())
        elif self.path == "/log-monitor/rules":
            rules = _load_rules()
            self._send(
                200,
                {
                    "enabled": rules["enabled"],
                    "minGroupEvents": rules["minGroupEvents"],
                    "maxTasksPerScan": rules["maxTasksPerScan"],
                    "note": DEFAULT_RULES["note"],
                },
            )
        else:
            issue_match = _ISSUE_PATH.match(self.path.split("?", 1)[0])
            if issue_match:
                self._send(
                    200,
                    _get_issue(
                        issue_match.group("owner"),
                        issue_match.group("repo"),
                        issue_match.group("number"),
                    ),
                )
            else:
                self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/log-monitor/rules":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if not 0 < length <= 4096:
            self._send(400, {"error": "invalid body"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._send(400, {"error": "invalid json"})
            return
        if not isinstance(payload, dict):
            self._send(400, {"error": "invalid rules"})
            return
        current = _load_rules()
        if "enabled" in payload:
            current["enabled"] = bool(payload["enabled"])
        if "minGroupEvents" in payload:
            try:
                current["minGroupEvents"] = max(
                    1, min(int(payload["minGroupEvents"]), 10_000)
                )
            except (TypeError, ValueError):
                self._send(400, {"error": "invalid minGroupEvents"})
                return
        if "maxTasksPerScan" in payload:
            try:
                current["maxTasksPerScan"] = max(
                    1, min(int(payload["maxTasksPerScan"]), 10)
                )
            except (TypeError, ValueError):
                self._send(400, {"error": "invalid maxTasksPerScan"})
                return
        try:
            RULES_PATH.parent.mkdir(parents=True, exist_ok=True)
            RULES_PATH.write_text(
                json.dumps(
                    {
                        "enabled": current["enabled"],
                        "minGroupEvents": current["minGroupEvents"],
                        "maxTasksPerScan": current["maxTasksPerScan"],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            RULES_PATH.chmod(0o600)
        except OSError:
            self._send(500, {"error": "cannot persist rules"})
            return
        _cache["at"] = 0.0
        self._send(
            200,
            {
                "enabled": current["enabled"],
                "minGroupEvents": current["minGroupEvents"],
                "maxTasksPerScan": current["maxTasksPerScan"],
            },
        )


def main() -> int:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"log monitor api on http://{HOST}:{PORT}/log-monitor")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
