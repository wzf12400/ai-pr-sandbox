"""Read-only log-platform monitor API for the local console frontend.

Serves one bounded, read-only scan endpoint on the loopback interface. It
reuses the existing OpenSearch Dashboards client, sanitizer, and deterministic
incident grouper. Raw hits stay in process memory; only sanitized aggregates
are returned. Passwords come from the runtime environment or macOS Keychain
and are never written to responses or logs.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import (
    kibana_incident_grouper,
    kibana_issue_connector,
    kibana_sanitizer,
    scan_coordination,
)
from src.log_task_ingestor import _load_local_source_settings
from src.terminal_control_center import _load_keychain_log_password

HOST = "127.0.0.1"
PORT = 8099
FETCH_SIZE = 100
MAX_BATCHES = 5
TARGET_ERROR_EVENTS = 50
CACHE_TTL_SECONDS = 300
COORDINATION_RETRY_TTL_SECONDS = 5
AUTO_SCAN_INTERVAL = 300
MAX_MESSAGE = 240
MAX_LOG_ROUTES = 50
MAX_ROUTE_RESPONSE_BYTES = 256_000
SUPPORTED_SELECTOR_FIELDS = {
    "kubernetes.container_name.keyword",
    "kubernetes.labels.app_kubernetes_io/name.keyword",
}
ROUTE_ID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
SELECTOR_VALUE_PATTERN = re.compile(
    r"[a-z0-9](?:[a-z0-9.-]{0,126}[a-z0-9])?"
)
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")

CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "http://127.0.0.1:8080")
RULES_PATH = Path(".issue-entry-state/log-monitor-rules.json")
AUTOMATION_STATE_PATH = Path(".issue-entry-state/log-monitor-automation.json")
AGGREGATE_DB_PATH = Path(".issue-entry-state/log-monitor-aggregates.sqlite3")
EVENT_REF_RETENTION_SECONDS = 7 * 24 * 60 * 60
DEFAULT_RULES = {
    "enabled": True,
    "minGroupEvents": 10,
    "maxTasksPerScan": 3,
    "note": "聚类事件数达到 minGroupEvents 时自动提交控制面任务，"
    "触发 Issue 生成与代码修改门禁流程",
}

_cache: Dict[str, Any] = {"at": 0.0, "payload": None}
_scan_lock = threading.Lock()


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


def _load_log_source_settings() -> Optional[Dict[str, str]]:
    discover_url = os.environ.get("OPENSEARCH_DISCOVER_URL", "").strip()
    username = os.environ.get("OPENSEARCH_USERNAME", "").strip()
    if discover_url and username:
        return {"discover_url": discover_url, "username": username}
    return _load_local_source_settings()


def _load_log_password(discover_url: str, username: str) -> str:
    return (
        os.environ.get("OPENSEARCH_PASSWORD", "").strip()
        or _load_keychain_log_password(discover_url, username)
    )


def _load_log_repository_routes() -> List[Dict[str, Any]]:
    base = _loopback_control_plane_url()
    if base is None:
        raise RuntimeError("control plane URL must use loopback")
    request = urllib.request.Request(
        f"{base}/api/log-repository-routes?enabledOnly=true",
        headers={"Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        raw = response.read(MAX_ROUTE_RESPONSE_BYTES + 1)
    if len(raw) > MAX_ROUTE_RESPONSE_BYTES:
        raise RuntimeError("log route response is too large")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, list) or len(payload) > MAX_LOG_ROUTES:
        raise RuntimeError("log route response is invalid")
    routes: List[Dict[str, Any]] = []
    selectors: set[tuple[str, str]] = set()
    for value in payload:
        if not isinstance(value, dict) or value.get("enabled") is not True:
            raise RuntimeError("log route response contains an invalid route")
        route_id = _text(value.get("id"), 36)
        selector_field = _text(value.get("selectorField"), 64)
        selector_value = _text(value.get("selectorValue"), 128)
        repository = _text(value.get("repository"), 255)
        version = value.get("version")
        selector = (selector_field, selector_value)
        if (
            not ROUTE_ID_PATTERN.fullmatch(route_id)
            or selector_field not in SUPPORTED_SELECTOR_FIELDS
            or not SELECTOR_VALUE_PATTERN.fullmatch(selector_value)
            or not REPOSITORY_PATTERN.fullmatch(repository)
            or not isinstance(version, int)
            or version < 0
            or selector in selectors
        ):
            raise RuntimeError("log route response contains an invalid route")
        selectors.add(selector)
        routes.append(
            {
                "id": route_id,
                "selectorField": selector_field,
                "selectorValue": selector_value,
                "repository": repository,
                "version": version,
            }
        )
    return routes


def _verify_log_selector(
    selector_field: str,
    selector_value: str,
) -> Dict[str, Any]:
    if (
        selector_field not in SUPPORTED_SELECTOR_FIELDS
        or not SELECTOR_VALUE_PATTERN.fullmatch(selector_value)
    ):
        raise ValueError("invalid exact log selector")
    settings = _load_log_source_settings()
    if not settings:
        return {"status": "not_configured", "found": False}
    password = _load_log_password(
        settings["discover_url"], settings["username"]
    )
    if not password:
        return {"status": "no_credentials", "found": False}
    target = kibana_issue_connector.parse_discover_url(settings["discover_url"])
    client = kibana_issue_connector.OpenSearchDashboardsClient(
        target,
        kibana_issue_connector.DashboardCredentials(
            username=settings["username"], password=password
        ),
    )
    index_pattern, time_field = client.resolve_index_pattern()
    found = client.exact_selector_exists(
        index_pattern,
        time_field,
        selector_field,
        selector_value,
    )
    return {
        "status": "ok",
        "found": found,
        "selectorField": selector_field,
        "selectorValue": selector_value,
        "window": {"from": target.time_from, "to": target.time_to},
    }


def _fetch_route_hits(
    client: Any,
    index_pattern: str,
    time_field: str,
    route: Dict[str, Any],
    time_from: Optional[str] = None,
    time_to: Optional[str] = None,
) -> List[Dict[str, Any]]:
    hits: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    page_time_to = time_to
    for _ in range(MAX_BATCHES):
        batch = client.fetch_latest_error_hits(
            index_pattern,
            time_field,
            FETCH_SIZE,
            time_from=time_from,
            time_to=page_time_to,
            exact_selector_field=route["selectorField"],
            exact_selector_value=route["selectorValue"],
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
            str((hit.get("_source") or {}).get("@timestamp") or "")
            for hit in fresh
            if isinstance(hit.get("_source"), dict)
        ]
        batch_ts = [timestamp for timestamp in batch_ts if timestamp]
        if not batch_ts:
            break
        page_time_to = min(batch_ts)
        if sum(1 for hit in hits if _quick_error_level(hit)) >= TARGET_ERROR_EVENTS:
            break
    return hits


def _route_scoped_incident_ref(
    route: Dict[str, Any],
    incident_ref: str,
) -> str:
    material = (
        f"{route['id']}\0{route['selectorField']}\0"
        f"{route['selectorValue']}\0{route['repository']}\0{incident_ref}".encode(
            "utf-8"
        )
    )
    return "incident_ref:" + hashlib.sha256(material).hexdigest()


def _route_watermark(
    checkpoint: Dict[str, Any],
    route_id: str,
) -> Optional[str]:
    routes = checkpoint.get("routes")
    if not isinstance(routes, dict):
        return None
    route_state = routes.get(route_id)
    if not isinstance(route_state, dict):
        return None
    value = route_state.get("watermark")
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


def _scan_payload(
    checkpoint: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not isinstance(checkpoint, dict):
        checkpoint = {}
    scan_cutoff = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    settings = _load_log_source_settings()
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
    password = _load_log_password(discover_url, username)
    if not password:
        return {
            "status": "no_credentials",
            "detail": "环境变量或 macOS Keychain 中没有日志平台只读密码",
        }

    target = kibana_issue_connector.parse_discover_url(discover_url)
    credentials = kibana_issue_connector.DashboardCredentials(
        username=username, password=password
    )
    client = kibana_issue_connector.OpenSearchDashboardsClient(target, credentials)
    index_pattern, time_field = client.resolve_index_pattern()
    routes = _load_log_repository_routes()
    if not routes:
        return {
            "status": "no_routes",
            "detail": "数据库中没有启用的日志仓库映射",
            "incidents": [],
        }

    namespaces: Dict[str, int] = {}
    services: Dict[str, int] = {}
    timestamps: List[str] = []
    sanitized_by_route: Dict[str, List[Dict[str, Any]]] = {
        route["id"]: [] for route in routes
    }
    blocked_count = 0
    non_error_count = 0
    total_hits = 0

    for route in routes:
        route_hits = _fetch_route_hits(
            client,
            index_pattern,
            time_field,
            route,
            time_from=_route_watermark(checkpoint, route["id"]),
            time_to=scan_cutoff,
        )
        total_hits += len(route_hits)
        for hit in route_hits:
            source = hit.get("_source") if isinstance(hit, dict) else None
            if not isinstance(source, dict):
                continue
            timestamp = _text(source.get("@timestamp"), 40)
            if timestamp:
                timestamps.append(timestamp)
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
            event_target = (
                event.get("target")
                if isinstance(event.get("target"), dict)
                else {}
            )
            namespace = _text(event_target.get("namespace"), 80)
            if namespace:
                namespaces[namespace] = namespaces.get(namespace, 0) + 1
            service = _text(event_target.get("service"), 80)
            if service:
                services[service] = services.get(service, 0) + 1
            sanitized_by_route[route["id"]].append(event)

    incident_count = 0
    incident_views: List[Dict[str, Any]] = []
    for route in routes:
        route_events = sanitized_by_route[route["id"]]
        incidents = (
            kibana_incident_grouper.group_sanitized_events(route_events)
            if route_events
            else []
        )
        incident_count += len(incidents)
        route_views = _incident_views(incidents)
        for view in _merge_views_by_issue_signature(incidents, route_views):
            original_ref = view["incidentRef"]
            view["incidentRef"] = _route_scoped_incident_ref(route, original_ref)
            view["routeId"] = route["id"]
            view["repository"] = route["repository"]
            view["selectorField"] = route["selectorField"]
            view["selectorValue"] = route["selectorValue"]
            incident_views.append(view)
    persisted_views = _persist_incident_views(incident_views)

    automation = _apply_automation_rules(persisted_views)
    retryable_outcomes = {"failed", "over_budget", "skipped"}
    can_advance = not any(
        item.get("result") in retryable_outcomes
        for item in automation.get("dispatched", [])
        if isinstance(item, dict)
    )
    next_checkpoint = (
        {
            "version": 1,
            "routes": {
                route["id"]: {"watermark": scan_cutoff}
                for route in routes
            },
        }
        if can_advance
        else checkpoint
    )
    retained_views, retained_count = _load_retained_incident_views(
        routes=routes,
    )
    route_by_id = {route["id"]: route for route in routes}
    log_errors = {route["selectorValue"]: 0 for route in routes}
    for view in retained_views:
        event_count = max(0, int(view.get("eventCount") or 0))
        route = route_by_id.get(_text(view.get("routeId"), 36))
        if route:
            log_errors[route["selectorValue"]] += event_count
    return {
        "status": "ok",
        "scannedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "indexPattern": index_pattern,
        "fetchSize": total_hits,
        "projectsScanned": len(routes),
        "routes": routes,
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
        "logErrors": [
            {"name": name, "errors": count}
            for name, count in log_errors.items()
        ],
        "errorEvents": sum(len(events) for events in sanitized_by_route.values()),
        "blockedEvents": blocked_count,
        "skippedNonError": non_error_count,
        "incidentGroups": incident_count,
        "retainedIncidentGroups": retained_count,
        "window": {
            "from": min(timestamps) if timestamps else None,
            "to": max(timestamps) if timestamps else None,
        },
        "incidents": retained_views,
        "automation": automation,
        "_checkpoint": next_checkpoint,
    }


def _incident_views(incidents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    incident_views: List[Dict[str, Any]] = []
    for incident in incidents[:50]:
        source = incident.get("source") or {}
        statistics = incident.get("statistics") or {}
        grouping = incident.get("grouping") or {}
        members = incident.get("members") or []
        member_services = sorted(
            {
                _text((member.get("target") or {}).get("service"), 80)
                for member in members
                if isinstance(member, dict)
                and isinstance(member.get("target"), dict)
            }
            - {""}
        )
        summaries = [
            _text((member.get("event") or {}).get("summary"), 140)
            for member in members
            if isinstance(member, dict)
            and isinstance(member.get("event"), dict)
        ]
        code_locations = sorted(
            {
                _code_location(member)
                for member in members
                if isinstance(member, dict)
            }
            - {""}
        )[:5]
        member_views = []
        for member in members[:20]:
            if not isinstance(member, dict):
                continue
            member_source = (
                member.get("source")
                if isinstance(member.get("source"), dict)
                else {}
            )
            member_event = (
                member.get("event")
                if isinstance(member.get("event"), dict)
                else {}
            )
            member_views.append(
                {
                    "timestamp": _text(member_source.get("timestamp"), 40),
                    "level": _text(member_event.get("level"), 16),
                    "summary": _text(member_event.get("summary"), 200),
                    "traceRef": _text(member_event.get("trace_ref"), 60),
                }
            )
        incident_views.append(
            {
                "incidentRef": _text(source.get("incident_ref"), 60),
                "eventCount": (incident.get("incident") or {}).get(
                    "event_count", 0
                ),
                "firstSeenAt": _text(source.get("first_seen_at"), 40),
                "lastSeenAt": _text(source.get("last_seen_at"), 40),
                "strategy": _text(grouping.get("strategy"), 40),
                "services": member_services[:5],
                "affectedEndpoints": list(
                    statistics.get("affected_endpoints") or []
                )[:8],
                "codeLocations": code_locations,
                "affectedUserCount": statistics.get("affected_user_count"),
                "summary": summaries[0] if summaries else "",
                "members": member_views,
                "_eventRefs": list(source.get("event_refs") or []),
                "_groupRefs": [_text(source.get("incident_ref"), 60)],
            }
        )
    return incident_views


def _code_location(member: Dict[str, Any]) -> str:
    """从脱敏事件提取代码位置：优先业务类.方法，退回 日志类:行号。"""
    target = member.get("target") if isinstance(member.get("target"), dict) else {}
    business_class = _text(target.get("business_class"), 120)
    business_method = _text(target.get("business_method"), 60)
    if business_class:
        simple = business_class.rsplit(".", 1)[-1]
        return f"{simple}.{business_method}" if business_method else simple
    logger_class = _text(target.get("logger_class"), 120)
    if logger_class:
        simple = logger_class.rsplit(".", 1)[-1]
        line = _text(str(target.get("logger_line") or ""), 10)
        return f"{simple}:{line}" if line else simple
    return ""


def _merge_views_by_issue_signature(
    incidents: List[Dict[str, Any]], incident_views: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """把"同一问题"（issue 指纹相同）的跨 trace 聚类在展示层合并。

    trace_ref 策略让每次请求各自成组；相同接口反复报相同错误时，
    面板会出现大量内容一样的 1 事件聚类。这里按
    kibana_incident_grouper.issue_signature 的指纹合并展示，
    底层 incident 的审计结构保持不变。
    """
    buckets: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for incident, view in zip(incidents[:50], incident_views):
        fingerprint = ""
        try:
            signature = kibana_incident_grouper.issue_signature(incident)
            if signature.get("eligible"):
                fingerprint = _text(signature.get("fingerprint"), 60)
        except ValueError:
            fingerprint = ""
        key = fingerprint or f"raw:{view['incidentRef']}"
        bucket = buckets.get(key)
        if bucket is None:
            bucket = dict(view)
            bucket["mergedIncidentRefs"] = [view["incidentRef"]]
            if fingerprint:
                # 控制面只认 incident_ref:/event_ref: 前缀；复用指纹的哈希部分，
                # 保证同一问题的 ref 跨扫描稳定（server 端据此去重复用任务）
                bucket["incidentRef"] = "incident_ref:" + fingerprint.removeprefix(
                    "issue_ref:"
                )
                bucket["strategy"] = "issue_signature"
            buckets[key] = bucket
            order.append(key)
            continue
        bucket["eventCount"] += view.get("eventCount", 0)
        first_seen = [v for v in (bucket.get("firstSeenAt"), view.get("firstSeenAt")) if v]
        last_seen = [v for v in (bucket.get("lastSeenAt"), view.get("lastSeenAt")) if v]
        if first_seen:
            bucket["firstSeenAt"] = min(first_seen)
        if last_seen:
            bucket["lastSeenAt"] = max(last_seen)
        bucket["services"] = sorted(
            set(bucket.get("services") or []) | set(view.get("services") or [])
        )[:5]
        bucket["affectedEndpoints"] = sorted(
            set(bucket.get("affectedEndpoints") or [])
            | set(view.get("affectedEndpoints") or [])
        )[:8]
        bucket["codeLocations"] = sorted(
            set(bucket.get("codeLocations") or [])
            | set(view.get("codeLocations") or [])
        )[:5]
        users = [
            u
            for u in (bucket.get("affectedUserCount"), view.get("affectedUserCount"))
            if isinstance(u, int)
        ]
        bucket["affectedUserCount"] = max(users) if users else None
        merged_members = (bucket.get("members") or []) + (view.get("members") or [])
        bucket["members"] = merged_members[:20]
        bucket["mergedIncidentRefs"].append(view["incidentRef"])
        bucket["_eventRefs"] = sorted(
            set(bucket.get("_eventRefs") or []) | set(view.get("_eventRefs") or [])
        )
        bucket["_groupRefs"] = sorted(
            set(bucket.get("_groupRefs") or []) | set(view.get("_groupRefs") or [])
        )
    return [buckets[key] for key in order]


def _json_list(value: Any) -> List[Any]:
    if not isinstance(value, str):
        return []
    try:
        decoded = json.loads(value)
    except ValueError:
        return []
    return decoded if isinstance(decoded, list) else []


def _open_aggregate_store(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.is_symlink():
        raise RuntimeError("log aggregate database must not be a symlink")
    connection = sqlite3.connect(path, timeout=10)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS incident_aggregates (
            fingerprint TEXT PRIMARY KEY,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            total_event_count INTEGER NOT NULL,
            incident_group_count INTEGER NOT NULL,
            services_json TEXT NOT NULL,
            endpoints_json TEXT NOT NULL,
            code_locations_json TEXT NOT NULL,
            affected_user_count INTEGER,
            summary TEXT NOT NULL,
            sample_json TEXT NOT NULL,
            repository TEXT NOT NULL DEFAULT '',
            route_id TEXT NOT NULL DEFAULT '',
            updated_at INTEGER NOT NULL
        )
        """
    )
    aggregate_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(incident_aggregates)")
    }
    if "repository" not in aggregate_columns:
        connection.execute(
            "ALTER TABLE incident_aggregates "
            "ADD COLUMN repository TEXT NOT NULL DEFAULT ''"
        )
    if "route_id" not in aggregate_columns:
        connection.execute(
            "ALTER TABLE incident_aggregates "
            "ADD COLUMN route_id TEXT NOT NULL DEFAULT ''"
        )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS incident_event_refs (
            event_ref TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            seen_at INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS incident_group_refs (
            incident_ref TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            seen_at INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS incident_event_seen_at "
        "ON incident_event_refs(seen_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS incident_group_seen_at "
        "ON incident_group_refs(seen_at)"
    )
    connection.commit()
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return connection


def _persist_incident_views(
    incident_views: List[Dict[str, Any]],
    database_path: Path = AGGREGATE_DB_PATH,
    observed_at: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Persist one bounded counter row per stable issue fingerprint."""
    now = int(time.time()) if observed_at is None else observed_at
    connection = _open_aggregate_store(database_path)
    persisted: List[Dict[str, Any]] = []
    try:
        cutoff = now - EVENT_REF_RETENTION_SECONDS
        connection.execute(
            "DELETE FROM incident_event_refs WHERE seen_at < ?", (cutoff,)
        )
        connection.execute(
            "DELETE FROM incident_group_refs WHERE seen_at < ?", (cutoff,)
        )
        for view in incident_views:
            fingerprint = _text(view.get("incidentRef"), 60)
            if not fingerprint:
                continue
            event_refs = sorted(
                {
                    _text(reference, 80)
                    for reference in view.get("_eventRefs") or []
                    if _text(reference, 80)
                }
            )
            group_refs = sorted(
                {
                    _text(reference, 80)
                    for reference in view.get("_groupRefs") or []
                    if _text(reference, 80)
                }
            )
            new_events = 0
            for event_ref in event_refs:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO incident_event_refs "
                    "(event_ref, fingerprint, seen_at) VALUES (?, ?, ?)",
                    (event_ref, fingerprint, now),
                )
                new_events += cursor.rowcount
            new_groups = 0
            for incident_ref in group_refs:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO incident_group_refs "
                    "(incident_ref, fingerprint, seen_at) VALUES (?, ?, ?)",
                    (incident_ref, fingerprint, now),
                )
                new_groups += cursor.rowcount

            existing = connection.execute(
                "SELECT first_seen_at, last_seen_at, total_event_count, "
                "incident_group_count, services_json, endpoints_json, "
                "code_locations_json, affected_user_count, summary, sample_json "
                ", repository, route_id "
                "FROM incident_aggregates WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            first_seen = _text(view.get("firstSeenAt"), 40)
            last_seen = _text(view.get("lastSeenAt"), 40)
            services = list(view.get("services") or [])
            endpoints = list(view.get("affectedEndpoints") or [])
            code_locations = list(view.get("codeLocations") or [])
            affected_user_count = view.get("affectedUserCount")
            summary = _text(view.get("summary"), 240)
            repository = _text(view.get("repository"), 255)
            route_id = _text(view.get("routeId"), 36)
            members = list(view.get("members") or [])
            sample = (
                max(
                    (member for member in members if isinstance(member, dict)),
                    key=lambda member: _text(member.get("timestamp"), 40),
                )
                if any(isinstance(member, dict) for member in members)
                else {}
            )
            if existing:
                first_seen = min(filter(None, (existing[0], first_seen)))
                last_seen = max(filter(None, (existing[1], last_seen)))
                total_event_count = int(existing[2]) + new_events
                incident_group_count = int(existing[3]) + new_groups
                services = sorted(set(_json_list(existing[4])) | set(services))[:5]
                endpoints = sorted(set(_json_list(existing[5])) | set(endpoints))[:8]
                code_locations = sorted(
                    set(_json_list(existing[6])) | set(code_locations)
                )[:5]
                prior_users = existing[7]
                if isinstance(prior_users, int) and isinstance(
                    affected_user_count, int
                ):
                    affected_user_count = max(prior_users, affected_user_count)
                elif isinstance(prior_users, int):
                    affected_user_count = prior_users
                if not summary:
                    summary = str(existing[8])
                if not sample:
                    sample_value = json.loads(existing[9])
                    sample = sample_value if isinstance(sample_value, dict) else {}
                if not repository:
                    repository = _text(existing[10], 255)
                if not route_id:
                    route_id = _text(existing[11], 36)
            else:
                total_event_count = (
                    new_events
                    if event_refs
                    else int(view.get("eventCount") or 0)
                )
                incident_group_count = (
                    new_groups if group_refs else 1
                )

            connection.execute(
                """
                INSERT INTO incident_aggregates (
                    fingerprint, first_seen_at, last_seen_at, total_event_count,
                    incident_group_count, services_json, endpoints_json,
                    code_locations_json, affected_user_count, summary, sample_json,
                    repository, route_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    first_seen_at = excluded.first_seen_at,
                    last_seen_at = excluded.last_seen_at,
                    total_event_count = excluded.total_event_count,
                    incident_group_count = excluded.incident_group_count,
                    services_json = excluded.services_json,
                    endpoints_json = excluded.endpoints_json,
                    code_locations_json = excluded.code_locations_json,
                    affected_user_count = excluded.affected_user_count,
                    summary = excluded.summary,
                    sample_json = excluded.sample_json,
                    repository = excluded.repository,
                    route_id = excluded.route_id,
                    updated_at = excluded.updated_at
                """,
                (
                    fingerprint,
                    first_seen,
                    last_seen,
                    total_event_count,
                    incident_group_count,
                    json.dumps(services, ensure_ascii=False),
                    json.dumps(endpoints, ensure_ascii=False),
                    json.dumps(code_locations, ensure_ascii=False),
                    affected_user_count,
                    summary,
                    json.dumps(sample, ensure_ascii=False),
                    repository,
                    route_id,
                    now,
                ),
            )
            persisted.append(
                {
                    **{
                        key: value
                        for key, value in view.items()
                        if not key.startswith("_")
                    },
                    "eventCount": total_event_count,
                    "currentScanEventCount": new_events,
                    "incidentGroupCount": incident_group_count,
                    "firstSeenAt": first_seen,
                    "lastSeenAt": last_seen,
                    "services": services,
                    "affectedEndpoints": endpoints,
                    "codeLocations": code_locations,
                    "affectedUserCount": affected_user_count,
                    "summary": summary,
                    "members": [sample] if sample else [],
                    "repository": repository,
                    "routeId": route_id,
                }
            )
        connection.commit()
    finally:
        connection.close()
    return persisted


def _load_retained_incident_views(
    database_path: Path = AGGREGATE_DB_PATH,
    observed_at: Optional[int] = None,
    limit: int = 50,
    routes: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """Load recent sanitized aggregates without replaying them through automation."""
    now = int(time.time()) if observed_at is None else observed_at
    cutoff = now - EVENT_REF_RETENTION_SECONDS
    bounded_limit = max(1, min(limit, 100))
    connection = _open_aggregate_store(database_path)
    try:
        route_by_service = {
            route["selectorValue"]: route
            for route in routes or []
        }
        for fingerprint, services_json in connection.execute(
            "SELECT fingerprint, services_json FROM incident_aggregates "
            "WHERE repository = ''"
        ):
            services = _json_list(services_json)
            if len(services) != 1:
                continue
            route = route_by_service.get(services[0])
            if route:
                connection.execute(
                    "UPDATE incident_aggregates SET repository = ?, route_id = ? "
                    "WHERE fingerprint = ? AND repository = ''",
                    (route["repository"], route["id"], fingerprint),
                )
        connection.commit()
        total = int(
            connection.execute(
                "SELECT COUNT(*) FROM incident_aggregates WHERE updated_at >= ?",
                (cutoff,),
            ).fetchone()[0]
        )
        rows = connection.execute(
            """
            SELECT fingerprint, first_seen_at, last_seen_at, total_event_count,
                   incident_group_count, services_json, endpoints_json,
                   code_locations_json, affected_user_count, summary, sample_json
                   , repository, route_id
            FROM incident_aggregates
            WHERE updated_at >= ?
            ORDER BY last_seen_at DESC, fingerprint ASC
            LIMIT ?
            """,
            (cutoff, bounded_limit),
        ).fetchall()
    finally:
        connection.close()

    views: List[Dict[str, Any]] = []
    for row in rows:
        try:
            sample = json.loads(row[10])
        except (TypeError, ValueError):
            sample = {}
        views.append(
            {
                "incidentRef": row[0],
                "eventCount": int(row[3]),
                "currentScanEventCount": 0,
                "incidentGroupCount": int(row[4]),
                "firstSeenAt": row[1],
                "lastSeenAt": row[2],
                "strategy": "retained_aggregate",
                "services": _json_list(row[5]),
                "affectedEndpoints": _json_list(row[6]),
                "codeLocations": _json_list(row[7]),
                "affectedUserCount": row[8],
                "summary": row[9],
                "members": [sample] if isinstance(sample, dict) and sample else [],
                "repository": row[11],
                "routeId": row[12],
            }
        )
    merged_views = _merge_retained_views_by_message_template(views)
    merged_total = max(
        len(merged_views),
        total - (len(views) - len(merged_views)),
    )
    return merged_views[:bounded_limit], merged_total


def _merge_retained_views_by_message_template(
    views: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    buckets: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for view in views:
        services = sorted(set(view.get("services") or []))
        locations = sorted(set(view.get("codeLocations") or []))
        endpoints = sorted(set(view.get("affectedEndpoints") or []))
        template = kibana_incident_grouper.stable_message_template(
            _text(view.get("summary"), 500)
        )
        if len(services) == 1 and len(locations) == 1 and template:
            key = json.dumps(
                {
                    "repository": _text(view.get("repository"), 255),
                    "routeId": _text(view.get("routeId"), 36),
                    "services": services,
                    "locations": locations,
                    "endpoints": endpoints,
                    "messageTemplate": template,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        else:
            key = f"raw:{view['incidentRef']}"
        bucket = buckets.get(key)
        if bucket is None:
            bucket = dict(view)
            buckets[key] = bucket
            order.append(key)
            continue
        bucket["eventCount"] += int(view.get("eventCount") or 0)
        bucket["incidentGroupCount"] += int(
            view.get("incidentGroupCount") or 0
        )
        first_seen = [
            value
            for value in (bucket.get("firstSeenAt"), view.get("firstSeenAt"))
            if value
        ]
        last_seen = [
            value
            for value in (bucket.get("lastSeenAt"), view.get("lastSeenAt"))
            if value
        ]
        if first_seen:
            bucket["firstSeenAt"] = min(first_seen)
        if last_seen:
            bucket["lastSeenAt"] = max(last_seen)
        bucket["affectedEndpoints"] = sorted(
            set(bucket.get("affectedEndpoints") or []) | set(endpoints)
        )[:8]
        bucket["codeLocations"] = sorted(
            set(bucket.get("codeLocations") or []) | set(locations)
        )[:5]
        bucket["members"] = (
            (bucket.get("members") or []) + (view.get("members") or [])
        )[:20]
        users = [
            value
            for value in (
                bucket.get("affectedUserCount"),
                view.get("affectedUserCount"),
            )
            if isinstance(value, int)
        ]
        bucket["affectedUserCount"] = max(users) if users else None
        bucket["strategy"] = "retained_message_template"
    return [buckets[key] for key in order]


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
    route_id = _text(incident_view.get("routeId"), 36)
    if not ROUTE_ID_PATTERN.fullmatch(route_id):
        return {"result": "skipped", "detail": "日志聚类缺少数据库路由"}
    payload = {
        "sourceType": "LOG",
        "input": (incident_view.get("summary") or "")[:400]
        or f"日志故障 {incident_view['incidentRef']}",
        "logIncident": {
            "dataSafetyStatus": "SANITIZED",
            "routeId": route_id,
            "sourceReference": incident_view["incidentRef"],
            "firstSeenAt": incident_view["firstSeenAt"],
            "lastSeenAt": incident_view["lastSeenAt"],
            "currentScanEventCount": max(
                1, incident_view.get("currentScanEventCount") or 0
            ),
            "historicalEventCount": incident_view["eventCount"],
            "incidentGroupCount": incident_view.get("incidentGroupCount") or 1,
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


def _fetch_task_status(base: str, task_id: str) -> Optional[str]:
    """Best-effort task status lookup; None on any failure (caller keeps skipping)."""
    if not task_id or len(task_id) > 40:
        return None
    if not all(ch in "0123456789abcdef-" for ch in task_id.lower()):
        return None
    request = urllib.request.Request(f"{base}/api/tasks/{task_id}", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            body = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - 查询失败按未失败处理，维持本地跳过
        return None
    if not isinstance(body, dict):
        return None
    task = body.get("task")
    status = task.get("status") if isinstance(task, dict) else body.get("status")
    return status if isinstance(status, str) else None


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
            task_id = dispatched[ref].get("taskId")
            base = _loopback_control_plane_url()
            status = (
                _fetch_task_status(base, task_id)
                if base and isinstance(task_id, str)
                else None
            )
            if status != "FAILED":
                automation["dispatched"].append(
                    {"incidentRef": ref, "result": "already_dispatched",
                     "taskId": task_id}
                )
                continue
            # 任务 FAILED：放行到下面的重派逻辑，由控制面按重试上限
            # 决定是否重新排队；本地不重复计数
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


def _run_scan() -> Dict[str, Any]:
    checkpoint: Dict[str, Any] = {}
    try:
        lease = scan_coordination.acquire("LOG")
        if not lease.get("acquired"):
            retained_views, retained_count = _load_retained_incident_views()
            payload = {
                "status": "ok",
                "detail": "后台正在同步，当前展示最近一次扫描结果",
                "retainedIncidentGroups": retained_count,
                "incidents": retained_views,
                "coordination": {
                    "acquired": False,
                    "leaseExpiresAt": lease.get("leaseExpiresAt"),
                },
            }
        else:
            token = lease.get("leaseToken")
            if not isinstance(token, str) or not token:
                raise RuntimeError("control plane returned an invalid log lease")
            value = lease.get("checkpoint")
            checkpoint = value if isinstance(value, dict) else {}
            try:
                payload = _scan_payload(checkpoint)
                completed_checkpoint = payload.pop("_checkpoint", checkpoint)
                completion_error = (
                    None
                    if payload.get("status") == "ok"
                    else _text(
                        payload.get("detail") or payload.get("status"),
                        500,
                    )
                )
                scan_coordination.complete(
                    "LOG",
                    token,
                    completed_checkpoint,
                    error=completion_error,
                )
                payload["coordination"] = {"acquired": True}
            except Exception as scan_error:
                try:
                    scan_coordination.complete(
                        "LOG",
                        token,
                        checkpoint,
                        error=str(scan_error)[:500],
                    )
                except Exception as release_error:
                    raise RuntimeError(
                        f"{type(scan_error).__name__}; "
                        "log scan lease release failed"
                    ) from release_error
                raise
    except Exception as exception:  # bounded: never leak internals
        payload = {
            "status": "error",
            "detail": f"扫描失败：{type(exception).__name__}",
        }
    _cache["at"] = time.time()
    _cache["payload"] = payload
    return payload


def _warming_payload() -> Dict[str, Any]:
    retained_views, retained_count = _load_retained_incident_views()
    return {
        "status": "ok",
        "detail": "首次扫描进行中，约 1 分钟后自动展示",
        "retainedIncidentGroups": retained_count,
        "incidents": retained_views,
    }


def _kick_background_scan() -> None:
    if not _scan_lock.acquire(blocking=False):
        return  # 已有扫描在跑

    def _bg() -> None:
        try:
            _run_scan()
        finally:
            _scan_lock.release()

    threading.Thread(target=_bg, daemon=True).start()


def _get_scan(force: bool = False) -> Dict[str, Any]:
    if force:
        with _scan_lock:
            return _run_scan()
    now = time.time()
    cached = _cache["payload"]
    coordination = cached.get("coordination") if isinstance(cached, dict) else None
    retrying_coordination = (
        isinstance(coordination, dict)
        and coordination.get("acquired") is False
    )
    cache_ttl = (
        COORDINATION_RETRY_TTL_SECONDS
        if retrying_coordination
        else CACHE_TTL_SECONDS
    )
    if cached is not None and now - _cache["at"] < cache_ttl:
        return cached
    # 过期或无缓存：后台补扫，本次请求立刻返回，绝不让前端干等
    _kick_background_scan()
    return cached if cached is not None else _warming_payload()


def _auto_scan_loop() -> None:
    while True:
        with _scan_lock:
            _run_scan()
        time.sleep(AUTO_SCAN_INTERVAL)


_ISSUE_PATH = re.compile(
    r"^/issue/(?P<owner>[A-Za-z0-9_.-]{1,100})/(?P<repo>[A-Za-z0-9_.-]{1,100})/(?P<number>[0-9]{1,9})$"
)
_PULL_PATH = re.compile(
    r"^/pull/(?P<owner>[A-Za-z0-9_.-]{1,100})/(?P<repo>[A-Za-z0-9_.-]{1,100})/(?P<number>[0-9]{1,9})$"
)
MAX_ISSUE_BODY_CHARS = 8000
MAX_PULL_PATCH_CHARS = 24_000


def _nonnegative_count(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


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


def _get_pull(owner: str, repo: str, number: str) -> Dict[str, Any]:
    """Read bounded PR metadata and patches through the authenticated gh CLI."""
    repository = f"{owner}/{repo}"
    try:
        metadata_result = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                number,
                "--repo",
                repository,
                "--json",
                (
                    "number,title,state,url,isDraft,additions,deletions,"
                    "changedFiles,baseRefName,headRefName"
                ),
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=20,
        )
        files_result = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{repository}/pulls/{number}/files?per_page=100",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "error", "detail": "gh 调用失败"}
    if metadata_result.returncode != 0 or files_result.returncode != 0:
        return {"status": "error", "detail": "无法读取该 Pull Request"}
    try:
        metadata = json.loads(metadata_result.stdout.decode("utf-8"))
        file_payload = json.loads(files_result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {"status": "error", "detail": "gh 响应无效"}
    if not isinstance(metadata, dict) or not isinstance(file_payload, list):
        return {"status": "error", "detail": "Pull Request 响应结构无效"}

    files: List[Dict[str, Any]] = []
    for item in file_payload[:100]:
        if not isinstance(item, dict):
            continue
        filename = item.get("filename")
        if (
            not isinstance(filename, str)
            or not filename
            or filename.startswith("/")
            or ".." in Path(filename).parts
        ):
            continue
        patch = item.get("patch")
        bounded_patch = patch[:MAX_PULL_PATCH_CHARS] if isinstance(patch, str) else ""
        files.append(
            {
                "path": filename[:500],
                "status": _text(item.get("status"), 20),
                "additions": _nonnegative_count(item.get("additions")),
                "deletions": _nonnegative_count(item.get("deletions")),
                "changes": _nonnegative_count(item.get("changes")),
                "patch": bounded_patch,
                "patchTruncated": isinstance(patch, str)
                and len(patch) > len(bounded_patch),
            }
        )
    return {
        "status": "ok",
        "number": metadata.get("number"),
        "title": _text(metadata.get("title"), 300),
        "state": _text(metadata.get("state"), 20),
        "url": _text(metadata.get("url"), 300),
        "draft": metadata.get("isDraft") is True,
        "additions": _nonnegative_count(metadata.get("additions")),
        "deletions": _nonnegative_count(metadata.get("deletions")),
        "changedFiles": _nonnegative_count(metadata.get("changedFiles")),
        "baseRef": _text(metadata.get("baseRefName"), 200),
        "headRef": _text(metadata.get("headRefName"), 200),
        "files": files,
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
            self._send(200, _get_scan(force=True))
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
            request_path = self.path.split("?", 1)[0]
            issue_match = _ISSUE_PATH.match(request_path)
            pull_match = _PULL_PATH.match(request_path)
            if issue_match:
                self._send(
                    200,
                    _get_issue(
                        issue_match.group("owner"),
                        issue_match.group("repo"),
                        issue_match.group("number"),
                    ),
                )
            elif pull_match:
                self._send(
                    200,
                    _get_pull(
                        pull_match.group("owner"),
                        pull_match.group("repo"),
                        pull_match.group("number"),
                    ),
                )
            else:
                self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path not in (
            "/log-monitor/rules",
            "/log-monitor/routes/verify",
        ):
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
            self._send(400, {"error": "invalid payload"})
            return
        if self.path == "/log-monitor/routes/verify":
            selector_field = payload.get("selectorField")
            selector_value = payload.get("selectorValue")
            if not isinstance(selector_field, str) or not isinstance(
                selector_value, str
            ):
                self._send(400, {"error": "invalid selector"})
                return
            try:
                result = _verify_log_selector(
                    selector_field.strip(), selector_value.strip()
                )
            except (OSError, RuntimeError, ValueError):
                self._send(502, {"status": "error", "found": False})
                return
            self._send(200, result)
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
    threading.Thread(target=_auto_scan_loop, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(
        f"log monitor api on http://{HOST}:{PORT}/log-monitor"
        f" (auto-scan every {AUTO_SCAN_INTERVAL}s)"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
