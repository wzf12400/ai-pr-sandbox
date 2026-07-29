"""Fetch bounded OpenSearch Dashboards error candidates and turn them into guarded Issues."""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src import (
    ai_issue_generator,
    issue_publication_policy,
    kibana_incident_grouper,
    kibana_sanitizer,
)
from src.issue_draft import _atomic_write_json
from src.issue_entry import _gateway_config, _infer_repository, publish_issue
from src.issue_intake import find_sensitive_data


USERNAME_ENV = "OPENSEARCH_USERNAME"
PASSWORD_ENV = "OPENSEARCH_PASSWORD"
TENANT_ENV = "OPENSEARCH_TENANT"
MAX_CANDIDATES = 5000
MAX_GENERATE_CANDIDATES = 20
MAX_PUBLISH_CANDIDATES = 3
MAX_FETCH_SIZE = 100
DEFAULT_MAX_SCAN_HITS = 1000
MAX_SCAN_HITS = 5000
DEFAULT_INITIAL_SCAN_HITS = 30
MAX_INITIAL_SCAN_HITS = 100
DEFAULT_SCAN_OVERLAP_SECONDS = 300
MAX_SCAN_OVERLAP_SECONDS = 3600
DEFAULT_MAX_CATCHUP_WINDOW_SECONDS = 0
MAX_CATCHUP_WINDOW_SECONDS = 3600
DEFAULT_SCAN_DELAY_SECONDS = 0
MAX_SCAN_DELAY_SECONDS = 86_400
SCROLL_KEEP_ALIVE = "2m"
SCAN_CURSOR_SCHEMA_VERSION = "kibana-scan-cursor/v2"
LEGACY_SCAN_CURSOR_SCHEMA_VERSION = "kibana-scan-cursor/v1"
SCAN_QUERY_VERSION = "opensearch-error-scan/v3"
LEGACY_SCAN_QUERY_VERSION = "opensearch-error-scan/v2"
HISTORY_CURSOR_SCHEMA_VERSION = "kibana-history-cursor/v1"
HISTORY_QUERY_VERSION = "opensearch-error-history/v1"
MAX_SCAN_CURSOR_BYTES = 16_384
MAX_BLOCKED_ERROR_PREVIEWS = 10
MAX_TIMEOUT_SECONDS = 120
ERROR_QUERY = 'message:(ERROR OR FATAL OR Exception OR "Caused by")'
ERROR_WINDOW_INTERVAL_SECONDS = 300
MAX_BLOCKED_CONTEXTS = 3
BLOCKED_CONTEXT_RADIUS = 160
HIGH_ENTROPY_REDACTION = "[REDACTED:unclassified_high_entropy]"
DATA_VIEW_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
INDEX_PATTERN = re.compile(r"^[A-Za-z0-9._*,-]{1,500}$")
RELATIVE_TIME_PATTERN = re.compile(r"^now(?:-\d+[mhdw])?$|^now$")


@dataclass(frozen=True)
class DiscoverTarget:
    base_url: str
    data_view_id: str
    time_from: str
    time_to: str


@dataclass(frozen=True)
class DashboardCredentials:
    username: str
    password: str = field(repr=False)
    tenant: str = ""


@dataclass(frozen=True)
class ErrorHitBatch:
    hits: List[Dict[str, Any]]
    completed_through: str
    backlog_remaining: bool


def parse_discover_url(url: str) -> DiscoverTarget:
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Discover URL must use HTTPS")
    marker = "/_dashboards/app/discover"
    if marker not in parsed.path:
        raise ValueError("URL must point to OpenSearch Dashboards Discover")
    base_path = parsed.path.split("/app/discover", 1)[0]
    fragment = urllib.parse.unquote(parsed.fragment)
    data_view = re.search(r"(?:^|[,(])index:([^,)]+)", fragment)
    time_range = re.search(r"time:\(from:([^,)]+),to:([^,)]+)\)", fragment)
    if not data_view or not time_range:
        raise ValueError("Discover URL must include a data-view ID and time range")
    data_view_id = data_view.group(1).strip("'\"")
    time_from = time_range.group(1).strip("'\"")
    time_to = time_range.group(2).strip("'\"")
    if not DATA_VIEW_PATTERN.fullmatch(data_view_id):
        raise ValueError("Discover data-view ID contains unsupported characters")
    if not RELATIVE_TIME_PATTERN.fullmatch(time_from) or not RELATIVE_TIME_PATTERN.fullmatch(time_to):
        raise ValueError("only bounded relative Discover time ranges are supported")
    return DiscoverTarget(
        base_url=urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, base_path, "", "")),
        data_view_id=data_view_id,
        time_from=time_from,
        time_to=time_to,
    )


def _safe_http_detail(exc: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(exc.read(4096).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    values: List[Any] = []
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            values.append(error.get("reason"))
        values.extend([payload.get("message"), payload.get("statusCode")])
    detail = next((str(value).strip() for value in values if value not in (None, "")), "")
    detail = " ".join(detail.split())[:300]
    sanitized, findings = kibana_sanitizer.redact_free_text(detail, "gateway_error")
    return "" if any(item.action == "blocked" for item in findings) else sanitized


def _transport_timed_out(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, BaseException):
            return _transport_timed_out(reason)
    return isinstance(exc, ssl.SSLError) and "timed out" in str(exc).casefold()


class OpenSearchDashboardsClient:
    def __init__(
        self,
        target: DiscoverTarget,
        credentials: DashboardCredentials,
        timeout_seconds: float = 30,
        opener: Any = urllib.request.urlopen,
    ):
        self.target = target
        self.credentials = credentials
        self.timeout_seconds = timeout_seconds
        self._opener = opener

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not path.startswith("/api/"):
            raise ValueError("Dashboards request path is not allowed")
        token = base64.b64encode(
            f"{self.credentials.username}:{self.credentials.password}".encode("utf-8")
        ).decode("ascii")
        headers = {
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
            "osd-xsrf": "ai-pr-issue-connector",
        }
        if self.credentials.tenant:
            headers["securitytenant"] = self.credentials.tenant
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.target.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
                final_url = response.geturl()
        except urllib.error.HTTPError as exc:
            detail = _safe_http_detail(exc)
            suffix = f": {detail}" if detail else ""
            raise ValueError(f"OpenSearch Dashboards returned HTTP {exc.code}{suffix}") from exc
        except TimeoutError as exc:
            raise ValueError(
                f"OpenSearch Dashboards read timed out after {self.timeout_seconds:g} seconds"
            ) from exc
        except urllib.error.URLError as exc:
            if _transport_timed_out(exc):
                raise ValueError(
                    "OpenSearch Dashboards read timed out after "
                    f"{self.timeout_seconds:g} seconds"
                ) from exc
            raise ValueError("OpenSearch Dashboards request failed") from exc
        except OSError as exc:
            if _transport_timed_out(exc):
                raise ValueError(
                    "OpenSearch Dashboards read timed out after "
                    f"{self.timeout_seconds:g} seconds"
                ) from exc
            raise ValueError("OpenSearch Dashboards request failed") from exc
        if "/app/login" in final_url:
            raise ValueError("OpenSearch Dashboards credentials were not accepted")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("OpenSearch Dashboards returned a non-JSON response") from exc
        if not isinstance(result, dict):
            raise ValueError("OpenSearch Dashboards response must be a JSON object")
        return result

    def resolve_index_pattern(self) -> Tuple[str, str]:
        data_view_id = urllib.parse.quote(self.target.data_view_id, safe="")
        payload = self._request_json("GET", f"/api/saved_objects/index-pattern/{data_view_id}")
        attributes = payload.get("attributes")
        if not isinstance(attributes, dict):
            raise ValueError("data view response has no attributes")
        title = str(attributes.get("title", "")).strip()
        time_field = str(attributes.get("timeFieldName", "@timestamp")).strip() or "@timestamp"
        if not INDEX_PATTERN.fullmatch(title):
            raise ValueError("resolved index pattern contains unsupported characters")
        if time_field != "@timestamp":
            raise ValueError("only @timestamp data views are supported in phase one")
        return title, time_field

    @staticmethod
    def _hits(response: Dict[str, Any]) -> List[Dict[str, Any]]:
        container = response.get("hits")
        if not isinstance(container, dict):
            raise ValueError("OpenSearch search response has no hits object")
        hits = container.get("hits")
        if not isinstance(hits, list):
            raise ValueError("OpenSearch search response has invalid hits")
        if any(
            not isinstance(hit, dict) or not isinstance(hit.get("_source"), dict)
            for hit in hits
        ):
            raise ValueError("OpenSearch search response contains an invalid hit")
        return hits

    @staticmethod
    def _scroll_id(response: Dict[str, Any]) -> str:
        value = response.get("_scroll_id", "")
        return value.strip() if isinstance(value, str) else ""

    def _console_request(
        self,
        method: str,
        path: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        search_path = urllib.parse.urlencode({"path": path, "method": method})
        return self._request_json(
            "POST",
            f"/api/console/proxy?{search_path}",
            payload,
        )

    def _find_error_window(
        self,
        index_pattern: str,
        time_field: str,
        *,
        time_from: str,
        time_to: str,
        order: str,
        interval_seconds: int = ERROR_WINDOW_INTERVAL_SECONDS,
    ) -> Optional[Tuple[str, str]]:
        if interval_seconds != ERROR_WINDOW_INTERVAL_SECONDS:
            raise ValueError("only five-minute error windows are supported")
        if order not in {"asc", "desc"}:
            raise ValueError("error window order is invalid")
        lower = _parse_utc_timestamp(time_from)
        upper = _parse_utc_timestamp(time_to)
        if upper <= lower:
            raise ValueError("error window discovery range is invalid")
        response = self._console_request(
            "POST",
            f"{index_pattern}/_search",
            {
                "size": 0,
                "track_total_hits": False,
                "query": {
                    "bool": {
                        "filter": [
                            {
                                "range": {
                                    time_field: {
                                        "gte": time_from,
                                        "lt": time_to,
                                    }
                                }
                            },
                            {
                                "query_string": {
                                    "query": ERROR_QUERY,
                                    "analyze_wildcard": True,
                                }
                            },
                        ]
                    }
                },
                "aggs": {
                    "error_windows": {
                        "date_histogram": {
                            "field": time_field,
                            "fixed_interval": "5m",
                            "min_doc_count": 1,
                            "order": {"_key": order},
                        }
                    }
                },
            },
        )
        aggregations = response.get("aggregations")
        if not isinstance(aggregations, dict):
            raise ValueError("OpenSearch aggregation response is invalid")
        error_windows = aggregations.get("error_windows")
        if not isinstance(error_windows, dict):
            raise ValueError("OpenSearch error-window aggregation is missing")
        buckets = error_windows.get("buckets")
        if not isinstance(buckets, list):
            raise ValueError("OpenSearch error-window buckets are invalid")
        for bucket in buckets:
            if not isinstance(bucket, dict) or int(bucket.get("doc_count", 0)) <= 0:
                continue
            key = bucket.get("key")
            if not isinstance(key, (int, float)):
                raise ValueError("OpenSearch error-window key is invalid")
            bucket_start = datetime.fromtimestamp(key / 1000, tz=timezone.utc)
            bucket_end = min(
                upper,
                bucket_start + timedelta(seconds=interval_seconds),
            )
            selected_from = max(lower, bucket_start)
            if bucket_end > selected_from:
                return (
                    _format_utc_timestamp(selected_from),
                    _format_utc_timestamp(bucket_end),
                )
        return None

    def find_next_error_window(
        self,
        index_pattern: str,
        time_field: str,
        *,
        time_from: str,
        time_to: str,
        interval_seconds: int = ERROR_WINDOW_INTERVAL_SECONDS,
    ) -> Optional[Tuple[str, str]]:
        return self._find_error_window(
            index_pattern,
            time_field,
            time_from=time_from,
            time_to=time_to,
            order="asc",
            interval_seconds=interval_seconds,
        )

    def find_previous_error_window(
        self,
        index_pattern: str,
        time_field: str,
        *,
        time_from: str,
        time_to: str,
        interval_seconds: int = ERROR_WINDOW_INTERVAL_SECONDS,
    ) -> Optional[Tuple[str, str]]:
        return self._find_error_window(
            index_pattern,
            time_field,
            time_from=time_from,
            time_to=time_to,
            order="desc",
            interval_seconds=interval_seconds,
        )

    def fetch_error_hits(
        self,
        index_pattern: str,
        time_field: str,
        fetch_size: int,
        *,
        time_from: Optional[str] = None,
        time_to: Optional[str] = None,
        max_scan_hits: int = DEFAULT_MAX_SCAN_HITS,
    ) -> ErrorHitBatch:
        if not 1 <= fetch_size <= MAX_FETCH_SIZE:
            raise ValueError(f"fetch size must be between 1 and {MAX_FETCH_SIZE}")
        if not 1 <= max_scan_hits <= MAX_SCAN_HITS:
            raise ValueError(f"max scan hits must be between 1 and {MAX_SCAN_HITS}")
        payload = {
            "size": fetch_size,
            "_source": [
                "@timestamp",
                "stream",
                "logtag",
                "message",
                "kubernetes.namespace_name",
                "kubernetes.container_name",
                "kubernetes.container_image",
                "kubernetes.labels.app_kubernetes_io/name",
                "kubernetes.labels.topology_kubernetes_io/region",
                "kubernetes.labels.topology_kubernetes_io/zone",
            ],
            "query": {
                "bool": {
                    "filter": [
                        {
                            "range": {
                                time_field: {
                                    "gte": time_from or self.target.time_from,
                                    "lt": time_to or self.target.time_to,
                                }
                            }
                        },
                        {
                            "query_string": {
                                "query": ERROR_QUERY,
                                "analyze_wildcard": True,
                            }
                        },
                    ]
                }
            },
            "sort": [
                {
                    time_field: {
                        "order": "asc",
                        "unmapped_type": "date",
                    }
                },
                "_doc",
            ],
        }
        response = self._console_request(
            "POST",
            f"{index_pattern}/_search?scroll={SCROLL_KEEP_ALIVE}",
            payload,
        )
        scroll_id = self._scroll_id(response)
        collected: List[Dict[str, Any]] = []
        try:
            while True:
                page = self._hits(response)
                if len(collected) + len(page) > max_scan_hits:
                    overflow = collected + page
                    boundary = _parse_utc_timestamp(
                        str(overflow[max_scan_hits]["_source"].get(time_field, ""))
                    )
                    safe_hits = [
                        hit
                        for hit in overflow[:max_scan_hits]
                        if _parse_utc_timestamp(
                            str(hit["_source"].get(time_field, ""))
                        )
                        < boundary
                    ]
                    if not safe_hits:
                        raise ValueError(
                            f"more than {max_scan_hits} errors share one timestamp; "
                            "scan cursor was not advanced"
                        )
                    return ErrorHitBatch(
                        hits=safe_hits,
                        completed_through=_format_utc_timestamp(boundary),
                        backlog_remaining=True,
                    )
                collected.extend(page)
                if len(page) < fetch_size:
                    break
                if not scroll_id:
                    raise ValueError(
                        "OpenSearch did not return a scroll cursor for the next page"
                    )
                response = self._console_request(
                    "POST",
                    "_search/scroll",
                    {
                        "scroll": SCROLL_KEEP_ALIVE,
                        "scroll_id": scroll_id,
                    },
                )
                scroll_id = self._scroll_id(response) or scroll_id
        finally:
            if scroll_id:
                try:
                    self._console_request(
                        "DELETE",
                        "_search/scroll",
                        {"scroll_id": [scroll_id]},
                    )
                except (OSError, ValueError):
                    pass
        return ErrorHitBatch(
            hits=collected,
            completed_through=time_to or self.target.time_to,
            backlog_remaining=False,
        )

    def fetch_latest_error_hits(
        self,
        index_pattern: str,
        time_field: str,
        fetch_size: int,
        *,
        time_from: Optional[str] = None,
        time_to: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if not 1 <= fetch_size <= MAX_INITIAL_SCAN_HITS:
            raise ValueError(
                f"initial scan size must be between 1 and {MAX_INITIAL_SCAN_HITS}"
            )
        payload = {
            "size": fetch_size,
            "_source": [
                "@timestamp",
                "stream",
                "logtag",
                "message",
                "kubernetes.namespace_name",
                "kubernetes.container_name",
                "kubernetes.container_image",
                "kubernetes.labels.app_kubernetes_io/name",
                "kubernetes.labels.topology_kubernetes_io/region",
                "kubernetes.labels.topology_kubernetes_io/zone",
            ],
            "query": {
                "bool": {
                    "filter": [
                        {
                            "range": {
                                time_field: {
                                    "gte": time_from or self.target.time_from,
                                    "lte": time_to or self.target.time_to,
                                }
                            }
                        },
                        {
                            "query_string": {
                                "query": ERROR_QUERY,
                                "analyze_wildcard": True,
                            }
                        },
                    ]
                }
            },
            "sort": [
                {
                    time_field: {
                        "order": "desc",
                        "unmapped_type": "date",
                    }
                }
            ],
        }
        response = self._console_request(
            "POST",
            f"{index_pattern}/_search",
            payload,
        )
        return self._hits(response)


def _scan_source_sha256(
    target: DiscoverTarget,
    query_version: str = SCAN_QUERY_VERSION,
) -> str:
    encoded = json.dumps(
        {
            "query_version": query_version,
            "base_url": target.base_url,
            "data_view_id": target.data_view_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _history_source_sha256(target: DiscoverTarget) -> str:
    encoded = json.dumps(
        {
            "query_version": HISTORY_QUERY_VERSION,
            "base_url": target.base_url,
            "data_view_id": target.data_view_id,
            "time_from": target.time_from,
            "time_to": target.time_to,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parse_utc_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("scan cursor timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("scan cursor timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _format_utc_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _load_scan_cursor(
    path: Optional[Path],
    target: DiscoverTarget,
) -> Optional[Dict[str, Any]]:
    if path is None or not path.exists():
        return None
    if path.is_symlink() or path.stat().st_size > MAX_SCAN_CURSOR_BYTES:
        raise ValueError("scan cursor file is invalid")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("scan cursor file is unreadable") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version")
        not in {SCAN_CURSOR_SCHEMA_VERSION, LEGACY_SCAN_CURSOR_SCHEMA_VERSION}
        or payload.get("source_sha256")
        not in {
            _scan_source_sha256(target),
            _scan_source_sha256(target, LEGACY_SCAN_QUERY_VERSION),
        }
    ):
        raise ValueError("scan cursor does not match the configured log source")
    completed = str(payload.get("completed_through", "")).strip()
    completed_at = _parse_utc_timestamp(completed)
    backlog_pending = payload.get("backlog_pending", False)
    if not isinstance(backlog_pending, bool):
        raise ValueError("scan cursor backlog state is invalid")
    if backlog_pending:
        target_through = str(payload.get("backlog_target_through", "")).strip()
        target_at = _parse_utc_timestamp(target_through)
        if target_at <= completed_at:
            raise ValueError("scan cursor backlog boundary is invalid")
    return payload


def _scan_window(
    target: DiscoverTarget,
    cursor: Optional[Dict[str, Any]],
    overlap_seconds: int,
    max_catchup_window_seconds: int = DEFAULT_MAX_CATCHUP_WINDOW_SECONDS,
    scan_delay_seconds: int = DEFAULT_SCAN_DELAY_SECONDS,
    *,
    now: Optional[datetime] = None,
) -> Tuple[str, str, datetime]:
    if target.time_to != "now":
        raise ValueError("scan cursor requires a Discover window ending at now")
    cutoff = (now or datetime.now(timezone.utc)).astimezone(
        timezone.utc
    ) - timedelta(seconds=scan_delay_seconds)
    if cursor is None:
        time_from = target.time_from
    elif cursor.get("backlog_pending", False):
        completed = _parse_utc_timestamp(str(cursor["completed_through"]))
        pending_cutoff = _parse_utc_timestamp(
            str(cursor["backlog_target_through"])
        )
        cutoff = (
            min(cutoff, pending_cutoff)
            if scan_delay_seconds
            else pending_cutoff
        )
        if max_catchup_window_seconds:
            cutoff = min(
                cutoff,
                completed + timedelta(seconds=max_catchup_window_seconds),
            )
        cutoff = max(cutoff, completed)
        time_from = _format_utc_timestamp(completed)
    else:
        completed = _parse_utc_timestamp(str(cursor["completed_through"]))
        if max_catchup_window_seconds:
            cutoff = min(
                cutoff,
                completed + timedelta(seconds=max_catchup_window_seconds),
            )
        cutoff = max(cutoff, completed)
        time_from = _format_utc_timestamp(
            completed - timedelta(seconds=overlap_seconds)
        )
    return time_from, _format_utc_timestamp(cutoff), cutoff


def _save_scan_cursor(
    path: Path,
    target: DiscoverTarget,
    completed_through: datetime,
    summary_path: Path,
    *,
    backlog_pending: bool = False,
    backlog_target_through: Optional[datetime] = None,
) -> None:
    if backlog_pending and (
        backlog_target_through is None
        or backlog_target_through <= completed_through
    ):
        raise ValueError("scan cursor backlog boundary is invalid")
    _atomic_write_json(
        path,
        {
            "schema_version": SCAN_CURSOR_SCHEMA_VERSION,
            "source_sha256": _scan_source_sha256(target),
            "completed_through": _format_utc_timestamp(completed_through),
            "backlog_pending": backlog_pending,
            "backlog_target_through": (
                _format_utc_timestamp(backlog_target_through)
                if backlog_target_through is not None
                else ""
            ),
            "last_summary": str(summary_path),
            "updated_at": _format_utc_timestamp(datetime.now(timezone.utc)),
        },
    )


def _resolve_relative_time(value: str, reference: datetime) -> datetime:
    normalized = value.strip()
    if normalized == "now":
        return reference.astimezone(timezone.utc)
    match = re.fullmatch(r"now-(\d+)([mhdw])", normalized)
    if not match:
        return _parse_utc_timestamp(normalized)
    count = int(match.group(1))
    unit_seconds = {
        "m": 60,
        "h": 3600,
        "d": 86_400,
        "w": 604_800,
    }
    return reference.astimezone(timezone.utc) - timedelta(
        seconds=count * unit_seconds[match.group(2)]
    )


def _load_history_cursor(
    path: Path,
    target: DiscoverTarget,
) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_size > MAX_SCAN_CURSOR_BYTES:
        raise ValueError("history cursor file is invalid")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("history cursor file is unreadable") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != HISTORY_CURSOR_SCHEMA_VERSION
        or payload.get("source_sha256") != _history_source_sha256(target)
    ):
        raise ValueError("history cursor does not match the configured log source")
    range_from = _parse_utc_timestamp(str(payload.get("range_from", "")))
    next_before = _parse_utc_timestamp(str(payload.get("next_before", "")))
    if next_before < range_from:
        raise ValueError("history cursor boundary is invalid")
    pending_from = str(payload.get("pending_from", "")).strip()
    pending_to = str(payload.get("pending_to", "")).strip()
    if bool(pending_from) != bool(pending_to):
        raise ValueError("history cursor pending window is invalid")
    if pending_from:
        pending_start = _parse_utc_timestamp(pending_from)
        pending_end = _parse_utc_timestamp(pending_to)
        if not range_from <= next_before <= pending_start < pending_end:
            raise ValueError("history cursor pending window is invalid")
    return payload


def _save_history_cursor(
    path: Path,
    target: DiscoverTarget,
    *,
    range_from: datetime,
    next_before: datetime,
    summary_path: Path,
    pending_from: Optional[datetime] = None,
    pending_to: Optional[datetime] = None,
) -> None:
    if next_before < range_from:
        raise ValueError("history cursor boundary is invalid")
    if (pending_from is None) != (pending_to is None):
        raise ValueError("history cursor pending window is invalid")
    if pending_from is not None and pending_to is not None and not (
        range_from <= next_before <= pending_from < pending_to
    ):
        raise ValueError("history cursor pending window is invalid")
    _atomic_write_json(
        path,
        {
            "schema_version": HISTORY_CURSOR_SCHEMA_VERSION,
            "source_sha256": _history_source_sha256(target),
            "range_from": _format_utc_timestamp(range_from),
            "next_before": _format_utc_timestamp(next_before),
            "pending_from": (
                _format_utc_timestamp(pending_from)
                if pending_from is not None
                else ""
            ),
            "pending_to": (
                _format_utc_timestamp(pending_to)
                if pending_to is not None
                else ""
            ),
            "last_summary": str(summary_path),
            "updated_at": _format_utc_timestamp(datetime.now(timezone.utc)),
        },
    )


def _credentials(prompt_password: bool, username: str) -> DashboardCredentials:
    resolved_username = username.strip() or os.environ.get(USERNAME_ENV, "").strip()
    password = os.environ.get(PASSWORD_ENV, "")
    if prompt_password and not resolved_username:
        resolved_username = input("OpenSearch username: ").strip()
    if prompt_password and not password:
        password = getpass.getpass("OpenSearch password: ")
    if not resolved_username or not password:
        raise ValueError(
            f"{USERNAME_ENV} and {PASSWORD_ENV} are required, or use --username with --prompt-password"
        )
    return DashboardCredentials(
        username=resolved_username,
        password=password,
        tenant=os.environ.get(TENANT_ENV, "").strip(),
    )


def _load_published(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"version": 1, "published": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid connector state file: {path}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("published", {}), dict):
        raise ValueError(f"invalid connector state file: {path}")
    return payload


def _blocked_error_preview(sanitized: Dict[str, Any]) -> Dict[str, Any]:
    event = sanitized.get("event", {})
    target = sanitized.get("target", {})
    full_summary = str(event.get("summary", ""))
    summary = full_summary[:1000]
    if find_sensitive_data({"summary": summary}):
        summary = "[REDACTED:sensitive_preview]"
    blocked_contexts: List[str] = []
    search_from = 0
    while len(blocked_contexts) < MAX_BLOCKED_CONTEXTS:
        position = full_summary.find(HIGH_ENTROPY_REDACTION, search_from)
        if position < 0:
            break
        start = max(0, position - BLOCKED_CONTEXT_RADIUS)
        end = min(
            len(full_summary),
            position + len(HIGH_ENTROPY_REDACTION) + BLOCKED_CONTEXT_RADIUS,
        )
        context, _ = kibana_sanitizer.redact_free_text(
            full_summary[start:end], "blocked_preview.context"
        )
        context = " ".join(context.split())
        if find_sensitive_data({"context": context}):
            context = "[REDACTED:sensitive_preview]"
        blocked_contexts.append(context)
        search_from = position + len(HIGH_ENTROPY_REDACTION)
    findings = sanitized.get("sanitization", {}).get("findings", [])
    blocked_categories = sorted(
        {
            str(item.get("category", "unknown"))
            for item in findings
            if isinstance(item, dict) and item.get("action") == "blocked"
        }
    )
    return {
        "event_ref": sanitized.get("source", {}).get("event_ref", ""),
        "timestamp": sanitized.get("source", {}).get("timestamp", ""),
        "service": target.get("service", ""),
        "level": event.get("level", "UNKNOWN"),
        "logger_class": target.get("logger_class", ""),
        "logger_line": target.get("logger_line"),
        "business_class": target.get("business_class", ""),
        "business_method": target.get("business_method", ""),
        "blocked_categories": blocked_categories,
        "sanitized_summary": summary,
        "blocked_contexts": blocked_contexts,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch bounded OpenSearch error candidates and optionally create reviewed Issues."
    )
    parser.add_argument("--discover-url", required=True, help="OpenSearch Dashboards Discover URL.")
    parser.add_argument("--username", default="", help=f"Read-only username; defaults to {USERNAME_ENV}.")
    parser.add_argument("--prompt-password", action="store_true", help="Read the password without echoing it.")
    parser.add_argument("--max-candidates", type=int, default=5, help=f"Candidate limit, maximum {MAX_CANDIDATES}.")
    parser.add_argument("--fetch-size", type=int, default=50, help=f"Remote hit limit, maximum {MAX_FETCH_SIZE}.")
    parser.add_argument(
        "--max-scan-hits",
        type=int,
        default=DEFAULT_MAX_SCAN_HITS,
        help=f"Complete-scan safety limit, maximum {MAX_SCAN_HITS}.",
    )
    parser.add_argument(
        "--initial-scan-hits",
        type=int,
        default=DEFAULT_INITIAL_SCAN_HITS,
        help=(
            "Latest errors retained when initializing a new scan cursor; "
            f"maximum {MAX_INITIAL_SCAN_HITS}."
        ),
    )
    parser.add_argument(
        "--scan-state-file",
        type=Path,
        help="Optional local non-secret cursor advanced only after a complete scan.",
    )
    parser.add_argument(
        "--history-state-file",
        type=Path,
        help=(
            "Optional separate cursor for manually scanning older non-empty "
            "five-minute windows without changing the forward scan cursor."
        ),
    )
    parser.add_argument(
        "--defer-cursor-commit",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--scan-overlap-seconds",
        type=int,
        default=DEFAULT_SCAN_OVERLAP_SECONDS,
        help=(
            "Overlap before the completed cursor to include delayed events; "
            f"maximum {MAX_SCAN_OVERLAP_SECONDS} seconds."
        ),
    )
    parser.add_argument(
        "--max-catchup-window-seconds",
        type=int,
        default=DEFAULT_MAX_CATCHUP_WINDOW_SECONDS,
        help=(
            "Maximum cursor time slice per run; "
            f"maximum {MAX_CATCHUP_WINDOW_SECONDS} seconds, "
            "zero scans through the current cutoff."
        ),
    )
    parser.add_argument(
        "--find-next-error-window",
        action="store_true",
        help=(
            "Use a count-only five-minute histogram to skip empty cursor "
            "ranges before fetching the next error documents."
        ),
    )
    parser.add_argument(
        "--scan-delay-seconds",
        type=int,
        default=DEFAULT_SCAN_DELAY_SECONDS,
        help=(
            "Keep the cursor behind current time to allow delayed ingestion; "
            f"maximum {MAX_SCAN_DELAY_SECONDS} seconds."
        ),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=30,
        help=f"Per-request OpenSearch timeout, maximum {MAX_TIMEOUT_SECONDS} seconds.",
    )
    parser.add_argument("--generate", action="store_true", help="Generate locally reviewed AI Issue drafts.")
    parser.add_argument("--publish", action="store_true", help="Publish valid generated drafts with gh.")
    parser.add_argument("--confirm", action="store_true", help="Confirm human-approved GitHub publication.")
    parser.add_argument("--repository", help="GitHub owner/name; defaults to origin.")
    parser.add_argument(
        "--auto-publish-policy",
        type=Path,
        help="Operator-approved JSON policy that routes sanitized services to GitHub repositories.",
    )
    parser.add_argument(
        "--confirm-policy-sha256",
        default="",
        help="Exact SHA-256 of --auto-publish-policy; binds unattended publication to reviewed policy bytes.",
    )
    parser.add_argument("--prompt-api-key", action="store_true", help="Read AI_API_KEY without echoing it.")
    parser.add_argument("--output-dir", type=Path, default=Path(".kibana-issue-output"))
    parser.add_argument("--state-file", type=Path, default=Path(".issue-entry-state/kibana.json"))
    parser.add_argument("--name", help="Output folder; defaults to a UTC timestamp.")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    auto_publish = args.auto_publish_policy is not None
    if not 1 <= args.max_candidates <= MAX_CANDIDATES:
        print(f"error: --max-candidates must be between 1 and {MAX_CANDIDATES}", file=sys.stderr)
        return 2
    if not 1 <= args.max_scan_hits <= MAX_SCAN_HITS:
        print(
            f"error: --max-scan-hits must be between 1 and {MAX_SCAN_HITS}",
            file=sys.stderr,
        )
        return 2
    if not 1 <= args.initial_scan_hits <= MAX_INITIAL_SCAN_HITS:
        print(
            f"error: --initial-scan-hits must be between 1 and "
            f"{MAX_INITIAL_SCAN_HITS}",
            file=sys.stderr,
        )
        return 2
    if not 0 <= args.scan_overlap_seconds <= MAX_SCAN_OVERLAP_SECONDS:
        print(
            "error: --scan-overlap-seconds must be between 0 and "
            f"{MAX_SCAN_OVERLAP_SECONDS}",
            file=sys.stderr,
        )
        return 2
    if not 0 <= args.max_catchup_window_seconds <= MAX_CATCHUP_WINDOW_SECONDS:
        print(
            "error: --max-catchup-window-seconds must be between 0 and "
            f"{MAX_CATCHUP_WINDOW_SECONDS}",
            file=sys.stderr,
        )
        return 2
    if args.find_next_error_window and args.scan_state_file is None:
        print(
            "error: --find-next-error-window requires --scan-state-file",
            file=sys.stderr,
        )
        return 2
    if args.scan_state_file is not None and args.history_state_file is not None:
        print(
            "error: --scan-state-file and --history-state-file are mutually exclusive",
            file=sys.stderr,
        )
        return 2
    if args.find_next_error_window and args.history_state_file is not None:
        print(
            "error: --find-next-error-window cannot be used with --history-state-file",
            file=sys.stderr,
        )
        return 2
    if not 0 <= args.scan_delay_seconds <= MAX_SCAN_DELAY_SECONDS:
        print(
            "error: --scan-delay-seconds must be between 0 and "
            f"{MAX_SCAN_DELAY_SECONDS}",
            file=sys.stderr,
        )
        return 2
    if args.generate and args.max_candidates > MAX_GENERATE_CANDIDATES:
        print(
            f"error: generation is limited to {MAX_GENERATE_CANDIDATES} candidates per run",
            file=sys.stderr,
        )
        return 2
    if not 1 <= args.timeout_seconds <= MAX_TIMEOUT_SECONDS:
        print(
            f"error: --timeout-seconds must be between 1 and {MAX_TIMEOUT_SECONDS}",
            file=sys.stderr,
        )
        return 2
    if args.publish and (not args.generate or not args.confirm):
        print("error: --publish requires --generate and --confirm", file=sys.stderr)
        return 2
    if args.publish and args.max_candidates > MAX_PUBLISH_CANDIDATES:
        print(
            f"error: publication is limited to {MAX_PUBLISH_CANDIDATES} candidates per run",
            file=sys.stderr,
        )
        return 2
    if auto_publish and not args.generate:
        print("error: --auto-publish-policy requires --generate", file=sys.stderr)
        return 2
    if auto_publish and (args.publish or args.confirm or args.repository):
        print(
            "error: automatic publication policy cannot be combined with --publish, --confirm, or --repository",
            file=sys.stderr,
        )
        return 2
    if args.confirm_policy_sha256 and not auto_publish:
        print("error: --confirm-policy-sha256 requires --auto-publish-policy", file=sys.stderr)
        return 2

    run_name = args.name or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = args.output_dir / run_name
    if run_dir.exists():
        print(f"error: output already exists: {run_dir}", file=sys.stderr)
        return 2

    try:
        auto_policy = (
            issue_publication_policy.load_policy(
                args.auto_publish_policy, args.confirm_policy_sha256
            )
            if auto_publish
            else None
        )
        target = parse_discover_url(args.discover_url)
        scan_reference = datetime.now(timezone.utc)
        scan_cutoff = scan_reference
        initializing_cursor = False
        cursor: Optional[Dict[str, Any]] = None
        history_mode = args.history_state_file is not None
        history_cursor: Optional[Dict[str, Any]] = None
        history_range_from: Optional[datetime] = None
        history_next_before: Optional[datetime] = None
        history_pending_from: Optional[datetime] = None
        history_pending_to: Optional[datetime] = None
        if history_mode:
            if target.time_to != "now":
                raise ValueError("history scan requires a Discover window ending at now")
            history_cursor = _load_history_cursor(args.history_state_file, target)
            if history_cursor is None:
                history_range_from = _resolve_relative_time(
                    target.time_from,
                    scan_reference,
                )
                history_next_before = scan_reference - timedelta(
                    seconds=args.scan_delay_seconds
                )
                history_next_before = max(
                    history_range_from,
                    history_next_before,
                )
            else:
                history_range_from = _parse_utc_timestamp(
                    str(history_cursor["range_from"])
                )
                history_next_before = _parse_utc_timestamp(
                    str(history_cursor["next_before"])
                )
                if history_cursor.get("pending_from"):
                    history_pending_from = _parse_utc_timestamp(
                        str(history_cursor["pending_from"])
                    )
                    history_pending_to = _parse_utc_timestamp(
                        str(history_cursor["pending_to"])
                    )
            effective_time_from = _format_utc_timestamp(history_range_from)
            effective_time_to = _format_utc_timestamp(history_next_before)
            scan_cutoff = history_next_before
        elif args.scan_state_file is not None:
            cursor = _load_scan_cursor(args.scan_state_file, target)
            initializing_cursor = cursor is None
            effective_time_from, effective_time_to, scan_cutoff = _scan_window(
                target,
                cursor,
                args.scan_overlap_seconds,
                args.max_catchup_window_seconds,
                args.scan_delay_seconds,
                now=scan_cutoff,
            )
        else:
            effective_time_from = target.time_from
            effective_time_to = target.time_to
        planned_time_from = effective_time_from
        planned_time_to = effective_time_to
        if args.find_next_error_window and cursor is not None:
            planned_time_from = _format_utc_timestamp(
                _parse_utc_timestamp(str(cursor["completed_through"]))
            )
        raw_key = os.environ.get(kibana_sanitizer.HMAC_KEY_ENV, "").encode("utf-8")
        if len(raw_key) < kibana_sanitizer.MIN_HMAC_KEY_BYTES:
            raise ValueError(
                f"{kibana_sanitizer.HMAC_KEY_ENV} must contain at least "
                f"{kibana_sanitizer.MIN_HMAC_KEY_BYTES} bytes"
            )
        client = OpenSearchDashboardsClient(
            target,
            _credentials(args.prompt_password, args.username),
            timeout_seconds=args.timeout_seconds,
        )
        index_pattern, time_field = client.resolve_index_pattern()
        backlog_remaining = False
        completed_through = scan_cutoff
        error_window_discovery_used = False
        empty_error_range_skipped = False
        cursor_already_at_safe_cutoff = False
        history_exhausted = False
        if history_mode:
            error_window_discovery_used = True
            if history_pending_from is not None and history_pending_to is not None:
                discovered_window = (
                    _format_utc_timestamp(history_pending_from),
                    _format_utc_timestamp(history_pending_to),
                )
            elif history_next_before <= history_range_from:
                discovered_window = None
            else:
                discovered_window = client.find_previous_error_window(
                    index_pattern,
                    time_field,
                    time_from=_format_utc_timestamp(history_range_from),
                    time_to=_format_utc_timestamp(history_next_before),
                )
            if discovered_window is None:
                hits = []
                batch = None
                history_exhausted = True
                empty_error_range_skipped = True
                history_next_before = history_range_from
                effective_time_from = _format_utc_timestamp(history_range_from)
                effective_time_to = effective_time_from
            else:
                effective_time_from, effective_time_to = discovered_window
                batch = client.fetch_error_hits(
                    index_pattern,
                    time_field,
                    args.fetch_size,
                    time_from=effective_time_from,
                    time_to=effective_time_to,
                    max_scan_hits=args.max_scan_hits,
                )
                if isinstance(batch, ErrorHitBatch):
                    hits = batch.hits
                    completed_through = _parse_utc_timestamp(
                        batch.completed_through
                    )
                    backlog_remaining = batch.backlog_remaining
                    history_next_before = min(
                        history_next_before,
                        _parse_utc_timestamp(effective_time_from),
                    )
                    if backlog_remaining:
                        history_pending_from = completed_through
                        history_pending_to = _parse_utc_timestamp(
                            effective_time_to
                        )
                    else:
                        history_pending_from = None
                        history_pending_to = None
                else:
                    # Preserve compatibility with bounded injected adapters.
                    hits = batch
                    history_next_before = min(
                        history_next_before,
                        _parse_utc_timestamp(effective_time_from),
                    )
                    history_pending_from = None
                    history_pending_to = None
        elif initializing_cursor:
            hits = client.fetch_latest_error_hits(
                index_pattern,
                time_field,
                args.initial_scan_hits,
                time_from=effective_time_from,
                time_to=effective_time_to,
            )
        else:
            if (
                args.find_next_error_window
                and _parse_utc_timestamp(planned_time_to)
                <= _parse_utc_timestamp(planned_time_from)
            ):
                hits = []
                batch = None
                cursor_already_at_safe_cutoff = True
            elif args.find_next_error_window:
                error_window_discovery_used = True
                discovered_window = client.find_next_error_window(
                    index_pattern,
                    time_field,
                    time_from=planned_time_from,
                    time_to=planned_time_to,
                )
                if discovered_window is None:
                    hits = []
                    batch = None
                    empty_error_range_skipped = True
                else:
                    effective_time_from, effective_time_to = discovered_window
                    scan_cutoff = _parse_utc_timestamp(effective_time_to)
                    completed_through = scan_cutoff
                    batch = client.fetch_error_hits(
                        index_pattern,
                        time_field,
                        args.fetch_size,
                        time_from=effective_time_from,
                        time_to=effective_time_to,
                        max_scan_hits=args.max_scan_hits,
                    )
            else:
                batch = client.fetch_error_hits(
                    index_pattern,
                    time_field,
                    args.fetch_size,
                    time_from=effective_time_from,
                    time_to=effective_time_to,
                    max_scan_hits=args.max_scan_hits,
                )
            if isinstance(batch, ErrorHitBatch):
                hits = batch.hits
                completed_through = _parse_utc_timestamp(batch.completed_through)
                backlog_remaining = batch.backlog_remaining
                if backlog_remaining and args.scan_state_file is None:
                    raise ValueError(
                        "backlog batching requires --scan-state-file; "
                        "no partial result was written"
                    )
            elif batch is not None:
                # Preserve compatibility with bounded injected adapters.
                hits = batch
        published_state = _load_published(args.state_file)
        seen = published_state.setdefault("published", {})

        eligible_events: List[Dict[str, Any]] = []
        candidate_refs = set()
        selection: Dict[str, Any] = {
            "scanned_hits": 0,
            "parsed_levels": {},
            "sanitization_statuses": {},
            "accepted": 0,
            "accepted_events": 0,
            "eligible_events": 0,
            "grouped_incidents": 0,
            "rejected_not_error": 0,
            "rejected_blocked": 0,
            "rejected_already_published": 0,
            "rejected_already_published_issue_signature": 0,
            "rejected_duplicate_in_run": 0,
            "rejected_duplicate_issue_signature": 0,
            "rejected_missing_event_ref": 0,
            "rejected_candidate_limit": 0,
            "publication_blocked": 0,
            "publication_failed": 0,
            "published": 0,
            "blocked_error_previews": [],
            "issue_signature_duplicates": [],
        }
        for hit in hits:
            sanitized = kibana_sanitizer.sanitize_hit(
                hit,
                raw_key,
                include_aggregation_refs=True,
            )
            selection["scanned_hits"] += 1
            level = str(sanitized.get("event", {}).get("level", "UNKNOWN"))
            status = str(sanitized.get("sanitization", {}).get("status", "unknown"))
            selection["parsed_levels"][level] = selection["parsed_levels"].get(level, 0) + 1
            selection["sanitization_statuses"][status] = (
                selection["sanitization_statuses"].get(status, 0) + 1
            )
            event_ref = str(sanitized.get("source", {}).get("event_ref", ""))
            if not sanitized.get("sanitization", {}).get("ai_allowed", False):
                selection["rejected_blocked"] += 1
                if level in {"ERROR", "FATAL"} and len(
                    selection["blocked_error_previews"]
                ) < MAX_BLOCKED_ERROR_PREVIEWS:
                    selection["blocked_error_previews"].append(
                        _blocked_error_preview(sanitized)
                    )
                continue
            if not sanitized.get("event", {}).get("is_error", False):
                selection["rejected_not_error"] += 1
                continue
            if not event_ref:
                selection["rejected_missing_event_ref"] += 1
                continue
            if event_ref in candidate_refs:
                selection["rejected_duplicate_in_run"] += 1
                continue
            eligible_events.append(sanitized)
            candidate_refs.add(event_ref)
            selection["eligible_events"] += 1

        grouped_incidents = kibana_incident_grouper.group_sanitized_events(eligible_events)
        selection["grouped_incidents"] = len(grouped_incidents)
        unpublished_incidents: List[Dict[str, Any]] = []
        signature_by_incident_ref: Dict[str, Dict[str, Any]] = {}
        current_signatures: Dict[str, str] = {}
        for incident in grouped_incidents:
            source = incident["source"]
            incident_ref = source["incident_ref"]
            signature = kibana_incident_grouper.issue_signature(incident)
            signature_by_incident_ref[incident_ref] = signature
            fingerprint = signature["fingerprint"]
            event_deduplication_refs = [incident_ref, *source["event_refs"]]
            if any(reference in seen for reference in event_deduplication_refs):
                selection["rejected_already_published"] += 1
                continue
            if fingerprint and fingerprint in seen:
                selection["rejected_already_published_issue_signature"] += 1
                continue
            if fingerprint and fingerprint in current_signatures:
                selection["rejected_duplicate_issue_signature"] += 1
                selection["issue_signature_duplicates"].append(
                    {
                        "incident_ref": incident_ref,
                        "duplicate_of_incident_ref": current_signatures[fingerprint],
                        "issue_fingerprint": fingerprint,
                        "components": signature["components"],
                    }
                )
                continue
            if fingerprint:
                current_signatures[fingerprint] = incident_ref
            unpublished_incidents.append(incident)
        candidates = unpublished_incidents[: args.max_candidates]
        selection["accepted"] = len(candidates)
        selection["accepted_events"] = sum(
            incident["incident"]["event_count"] for incident in candidates
        )
        selection["rejected_candidate_limit"] = max(
            0, len(unpublished_incidents) - len(candidates)
        )
        if (
            args.scan_state_file is not None
            or args.history_state_file is not None
        ) and selection["rejected_candidate_limit"]:
            raise ValueError(
                "incident backlog exceeds the candidate limit; "
                "log cursor was not advanced"
            )

        config = _gateway_config(args.prompt_api_key) if args.generate else None
        repository = args.repository or _infer_repository() if args.publish else ""
        if args.publish and not repository:
            raise ValueError("--repository is required when origin is not a GitHub repository")

        summary: Dict[str, Any] = {
            "schema_version": "kibana-issue-connector/v2",
            "source": {
                "base_url": target.base_url,
                "data_view_id": target.data_view_id,
                "time_from": target.time_from,
                "time_to": target.time_to,
            },
            "query": {
                "resolved_index_pattern": index_pattern,
                "fetch_size": args.fetch_size,
                "max_scan_hits": args.max_scan_hits,
                "initial_scan_hits": args.initial_scan_hits,
                "scan_mode": (
                    "history_backfill_batch"
                    if history_mode
                    else "initial_latest"
                    if initializing_cursor
                    else "incremental_backlog_batch"
                    if backlog_remaining
                    else "incremental_cursor"
                    if args.scan_state_file is not None
                    else "bounded_window"
                ),
                "effective_time_from": effective_time_from,
                "effective_time_to": effective_time_to,
                "planned_time_from": planned_time_from,
                "planned_time_to": planned_time_to,
                "error_window_discovery_used": error_window_discovery_used,
                "empty_error_range_skipped": empty_error_range_skipped,
                "cursor_already_at_safe_cutoff": cursor_already_at_safe_cutoff,
                "cursor_overlap_seconds": args.scan_overlap_seconds,
                "catchup_window_seconds": args.max_catchup_window_seconds,
                "scan_delay_seconds": args.scan_delay_seconds,
                "cursor_time_field": time_field,
                "cursor_enabled": args.scan_state_file is not None,
                "cursor_commit_deferred": bool(
                    args.scan_state_file is not None
                    and args.defer_cursor_commit
                ),
                "history_cursor_enabled": history_mode,
                "history_cursor_commit_deferred": bool(
                    history_mode and args.defer_cursor_commit
                ),
                "history_range_from": (
                    _format_utc_timestamp(history_range_from)
                    if history_range_from is not None
                    else ""
                ),
                "history_next_before": (
                    _format_utc_timestamp(history_next_before)
                    if history_next_before is not None
                    else ""
                ),
                "history_pending_from": (
                    _format_utc_timestamp(history_pending_from)
                    if history_pending_from is not None
                    else ""
                ),
                "history_pending_to": (
                    _format_utc_timestamp(history_pending_to)
                    if history_pending_to is not None
                    else ""
                ),
                "history_exhausted": history_exhausted,
                "timeout_seconds": args.timeout_seconds,
                "returned_hits": len(hits),
                "batch_completed_through": _format_utc_timestamp(
                    completed_through
                ),
                "backlog_remaining": backlog_remaining,
                "incident_candidate_limit": args.max_candidates,
            },
            "mode": (
                "auto_publish"
                if auto_publish
                else "publish"
                if args.publish
                else "generate"
                if args.generate
                else "dry_run"
            ),
            "publication": {
                "requested": bool(args.publish or auto_publish),
                "provider": "github_cli" if args.publish or auto_publish else "",
                "repository": repository,
                "automatic_policy": (
                    issue_publication_policy.policy_summary(auto_policy)
                    if auto_policy is not None
                    else None
                ),
            },
            "selection": selection,
            "candidates": [],
        }
        for position, incident in enumerate(candidates, start=1):
            candidate_dir = run_dir / f"candidate-{position:02d}"
            sanitized_path = candidate_dir / "sanitized-incident.json"
            _atomic_write_json(sanitized_path, incident)
            incident_source = incident["source"]
            members = incident["members"]
            services = sorted(
                {
                    str(member.get("target", {}).get("service", ""))
                    for member in members
                    if member.get("target", {}).get("service")
                }
            )
            item: Dict[str, Any] = {
                "incident_ref": incident_source["incident_ref"],
                "event_refs": incident_source["event_refs"],
                "event_count": incident["incident"]["event_count"],
                "first_seen_at": incident_source["first_seen_at"],
                "last_seen_at": incident_source["last_seen_at"],
                "services": services,
                "grouping_strategy": incident["grouping"]["strategy"],
                "issue_signature": signature_by_incident_ref[
                    incident_source["incident_ref"]
                ],
                "status": "sanitized",
                "artifact": str(sanitized_path),
            }
            if args.generate and config is not None:
                result = ai_issue_generator.generate_issue(
                    incident,
                    ai_issue_generator.OpenAICompatibleChatProvider(config, config.model),
                    ai_issue_generator.OpenAICompatibleChatProvider(config, config.review_model),
                )
                result_path = candidate_dir / "result.json"
                markdown_path = candidate_dir / "issue.md"
                ai_issue_generator.write_result(result, result_path, markdown_path)
                item.update(
                    {
                        "status": result["state"],
                        "issue_draft": str(markdown_path),
                        "validation_valid": result["validation"]["valid"],
                    }
                )
                if args.publish or auto_policy is not None:
                    route = auto_policy.resolve(incident) if auto_policy is not None else None
                    target_repository = route.repository if route is not None else repository
                    publication = {
                        "status": "pending",
                        "provider": route.provider if route is not None else "github_cli",
                        "repository": target_repository,
                        "route_id": route.route_id if route is not None else "manual",
                        "authorization": (
                            {
                                "mode": "operator_approved_policy",
                                "policy_id": auto_policy.policy_id,
                                "policy_sha256": auto_policy.policy_sha256,
                            }
                            if auto_policy is not None
                            else {"mode": "explicit_cli_confirmation"}
                        ),
                    }
                    block_reason = ""
                    if auto_policy is not None and route is None:
                        block_reason = "no_approved_repository_route"
                    elif auto_policy is not None and result.get("state") not in auto_policy.allowed_states:
                        block_reason = "workflow_state_not_allowed_by_policy"
                    elif result.get("state") == "blocked" or not result.get(
                        "validation", {}
                    ).get("valid", False):
                        block_reason = "blocked_or_invalid_ai_output"
                    elif (
                        auto_policy is not None
                        and selection["published"] >= auto_policy.max_issues_per_run
                    ):
                        block_reason = "automatic_publication_run_limit_reached"
                    elif not incident.get("sanitization", {}).get(
                        "github_issue_allowed", False
                    ):
                        block_reason = "security_review_required"

                    if block_reason:
                        publication.update({"status": "blocked", "reason": block_reason})
                        selection["publication_blocked"] += 1
                    else:
                        try:
                            issue_url = publish_issue(
                                result, markdown_path, target_repository, incident
                            )
                        except ValueError as exc:
                            publication.update(
                                {"status": "failed", "reason": str(exc)}
                            )
                            selection["publication_failed"] += 1
                        else:
                            publication.update({"status": "published", "issue_url": issue_url})
                            item["status"] = "published"
                            item["issue_url"] = issue_url
                            selection["published"] += 1
                            publication_record = {
                                "issue_url": issue_url,
                                "published_at": datetime.now(timezone.utc).isoformat(),
                                "incident_ref": item["incident_ref"],
                                "event_refs": item["event_refs"],
                                "issue_fingerprint": item["issue_signature"]["fingerprint"],
                                "repository": target_repository,
                                "provider": publication["provider"],
                                "authorization": publication["authorization"],
                            }
                            references = [item["incident_ref"], *item["event_refs"]]
                            fingerprint = item["issue_signature"]["fingerprint"]
                            if fingerprint:
                                references.append(fingerprint)
                            for reference in references:
                                seen[reference] = publication_record
                            _atomic_write_json(args.state_file, published_state)
                    item["publication"] = publication
            summary["candidates"].append(item)
        summary_path = run_dir / "summary.json"
        _atomic_write_json(summary_path, summary)
        if (
            args.scan_state_file is not None
            and not args.defer_cursor_commit
        ):
            _save_scan_cursor(
                args.scan_state_file,
                target,
                completed_through,
                summary_path,
                backlog_pending=backlog_remaining,
                backlog_target_through=(
                    scan_cutoff if backlog_remaining else None
                ),
            )
        if (
            args.history_state_file is not None
            and not args.defer_cursor_commit
        ):
            _save_history_cursor(
                args.history_state_file,
                target,
                range_from=history_range_from,
                next_before=history_next_before,
                summary_path=summary_path,
                pending_from=history_pending_from,
                pending_to=history_pending_to,
            )
    except (FileExistsError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(run_dir / "summary.json")
    for item in summary["candidates"]:
        if item.get("issue_url"):
            print(item["issue_url"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
