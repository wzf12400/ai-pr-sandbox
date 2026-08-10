"""Submit sanitized legacy log incidents to the local Java control plane."""

from __future__ import annotations

import argparse
import getpass
import io
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from src import ai_issue_generator, kibana_issue_connector
from src.log_incident_inbox import LogIncidentInbox
from src.local_control_center import _atomic_replace_json
from src.mock_task_worker import DEFAULT_CONTROL_PLANE_URL, require_loopback_url
from src.terminal_control_center import (
    DEFAULT_LOG_INBOX_PATH,
    DEFAULT_LOG_KEY_PATH,
    DEFAULT_LOG_OUTPUT_PATH,
    DEFAULT_LOG_SCAN_STATE_PATH,
    Terminal,
    _commit_log_scan_cursor,
    _load_keychain_log_password,
    _poll_log_candidates,
    _store_keychain_log_password,
)


DISCOVER_URL_ENV = "OPENSEARCH_DISCOVER_URL"
CONTROL_PLANE_URL_ENV = "CONTROL_PLANE_URL"
REQUEST_TIMEOUT_ENV = "LOG_INGEST_REQUEST_TIMEOUT_SECONDS"
MAX_SUMMARY_LENGTH = 3900
MAX_OBSERVATIONS_IN_SUMMARY = 3
SOURCE_SETTINGS_SCHEMA_VERSION = "local-log-platform-source/v1"
DEFAULT_SOURCE_SETTINGS_PATH = Path(".issue-entry-state/log-platform.json")


class LogTaskIngestionError(ValueError):
    """A sanitized log batch could not be handed to the control plane."""


@dataclass(frozen=True)
class LogTaskIngestionConfig:
    discover_url: str
    username: str
    password: str = field(repr=False)
    control_plane_url: str = DEFAULT_CONTROL_PLANE_URL
    output_path: Path = DEFAULT_LOG_OUTPUT_PATH
    key_path: Path = DEFAULT_LOG_KEY_PATH
    scan_state_path: Path = DEFAULT_LOG_SCAN_STATE_PATH
    inbox_path: Path = DEFAULT_LOG_INBOX_PATH
    max_scan_hits: int = kibana_issue_connector.DEFAULT_MAX_SCAN_HITS
    initial_scan_hits: int = kibana_issue_connector.DEFAULT_INITIAL_SCAN_HITS
    request_timeout_seconds: float = 5.0

    def validated(self) -> "LogTaskIngestionConfig":
        if not self.discover_url.strip():
            raise LogTaskIngestionError(
                f"{DISCOVER_URL_ENV} or --discover-url is required"
            )
        kibana_issue_connector.parse_discover_url(self.discover_url)
        if not self.username.strip() or not self.password:
            raise LogTaskIngestionError(
                "a read-only OpenSearch username and password are required"
            )
        try:
            control_plane_url = require_loopback_url(
                self.control_plane_url,
                {"http"},
                CONTROL_PLANE_URL_ENV,
            )
        except RuntimeError as exception:
            raise LogTaskIngestionError(str(exception)) from exception
        if not 1 <= self.max_scan_hits <= kibana_issue_connector.MAX_SCAN_HITS:
            raise LogTaskIngestionError("max scan hits is outside the connector limit")
        if not 1 <= self.initial_scan_hits <= kibana_issue_connector.MAX_INITIAL_SCAN_HITS:
            raise LogTaskIngestionError("initial scan hits is outside the connector limit")
        if not 0 < self.request_timeout_seconds <= 30:
            raise LogTaskIngestionError(
                f"{REQUEST_TIMEOUT_ENV} must be greater than 0 and at most 30"
            )
        return LogTaskIngestionConfig(
            discover_url=self.discover_url.strip(),
            username=self.username.strip(),
            password=self.password,
            control_plane_url=control_plane_url,
            output_path=self.output_path,
            key_path=self.key_path,
            scan_state_path=self.scan_state_path,
            inbox_path=self.inbox_path,
            max_scan_hits=self.max_scan_hits,
            initial_scan_hits=self.initial_scan_hits,
            request_timeout_seconds=self.request_timeout_seconds,
        )


class ControlPlaneLogClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        try:
            self._base_url = require_loopback_url(
                base_url,
                {"http"},
                CONTROL_PLANE_URL_ENV,
            )
        except RuntimeError as exception:
            raise LogTaskIngestionError(str(exception)) from exception
        self._timeout_seconds = timeout_seconds
        self._opener = opener

    def create_task(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        request = Request(
            f"{self._base_url}/api/tasks",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener(request, timeout=self._timeout_seconds) as response:
                raw = response.read()
        except HTTPError as exception:
            raise LogTaskIngestionError(
                "the local control plane rejected a sanitized log task"
            ) from exception
        except (OSError, TimeoutError, URLError) as exception:
            raise LogTaskIngestionError(
                "the local control plane is unavailable"
            ) from exception
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exception:
            raise LogTaskIngestionError(
                "the local control plane returned an invalid response"
            ) from exception
        if not isinstance(result, dict):
            raise LogTaskIngestionError(
                "the local control plane returned an invalid task"
            )
        log_incident = result.get("logIncident")
        expected_reference = payload["logIncident"]["sourceReference"]
        if (
            result.get("sourceType") != "LOG"
            or not isinstance(result.get("id"), str)
            or not result["id"]
            or not isinstance(log_incident, dict)
            or log_incident.get("sourceReference") != expected_reference
        ):
            raise LogTaskIngestionError(
                "the local control plane returned an inconsistent log task"
            )
        return result


def _load_local_source_settings(
    path: Path = DEFAULT_SOURCE_SETTINGS_PATH,
) -> dict[str, str]:
    if not path.exists():
        return {}
    metadata = path.stat()
    if (
        path.is_symlink()
        or metadata.st_size > 16_384
        or metadata.st_mode & 0o077
    ):
        raise LogTaskIngestionError("local log source configuration is invalid")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exception:
        raise LogTaskIngestionError(
            "local log source configuration is unreadable"
        ) from exception
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != SOURCE_SETTINGS_SCHEMA_VERSION
        or set(payload) != {"schema_version", "discover_url", "username"}
    ):
        raise LogTaskIngestionError("local log source configuration is invalid")
    discover_url = str(payload.get("discover_url", "")).strip()
    username = str(payload.get("username", "")).strip()
    kibana_issue_connector.parse_discover_url(discover_url)
    if not username or len(username) > 128 or any(
        character in username for character in "\r\n\0"
    ):
        raise LogTaskIngestionError("local log source username is invalid")
    return {"discover_url": discover_url, "username": username}


def configure_local_source(
    discover_url: str,
    username: str,
    *,
    path: Path = DEFAULT_SOURCE_SETTINGS_PATH,
    password_storer: Callable[[str, str], None] = _store_keychain_log_password,
) -> None:
    target_url = discover_url.strip()
    target_username = username.strip()
    kibana_issue_connector.parse_discover_url(target_url)
    if not target_username or len(target_username) > 128 or any(
        character in target_username for character in "\r\n\0"
    ):
        raise LogTaskIngestionError("local log source username is invalid")
    _atomic_replace_json(
        path,
        {
            "schema_version": SOURCE_SETTINGS_SCHEMA_VERSION,
            "discover_url": target_url,
            "username": target_username,
        },
    )
    password_storer(target_url, target_username)


def _safe_text(value: Any, *, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _safe_list(value: Any, *, limit: int = 50) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted(
        {
            _safe_text(item, limit=255)
            for item in value[:limit]
            if _safe_text(item, limit=255)
        }
    )


def _nonnegative_int(record: Mapping[str, Any], name: str) -> int:
    value = record.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise LogTaskIngestionError(f"sanitized inbox record has invalid {name}")
    return value


def _aggregation_components(record: Mapping[str, Any]) -> dict[str, list[str]]:
    evidence = record.get("evidence")
    if not isinstance(evidence, dict):
        raise LogTaskIngestionError("sanitized inbox record has no evidence")
    compact = ai_issue_generator.compact_evidence(evidence)
    event = compact.get("event")
    event = event if isinstance(event, dict) else {}
    statistics = event.get("statistics")
    statistics = statistics if isinstance(statistics, dict) else {}
    components = statistics.get("aggregation_components")
    components = components if isinstance(components, dict) else {}
    return {
        name: _safe_list(components.get(name))
        for name in ("services", "paths", "exceptions", "systems", "top_frames")
    }


def _task_summary(record: Mapping[str, Any]) -> str:
    evidence = record.get("evidence")
    if not isinstance(evidence, dict):
        raise LogTaskIngestionError("sanitized inbox record has no evidence")
    compact = ai_issue_generator.compact_evidence(evidence)
    target = compact.get("target")
    target = target if isinstance(target, dict) else {}
    event = compact.get("event")
    event = event if isinstance(event, dict) else {}
    components = _aggregation_components(record)
    services = _safe_list(target.get("services")) or _safe_list(record.get("services"))
    endpoints = _safe_list(record.get("affected_endpoints"))
    parts = ["日志平台自动采集到故障"]
    for label, values in (
        ("服务", services),
        ("接口", endpoints),
        ("异常", components["exceptions"]),
        ("相关系统", components["systems"]),
        ("调用位置", components["top_frames"]),
    ):
        if values:
            parts.append(f"{label}: {', '.join(values[:10])}")
    observations = event.get("observations")
    if isinstance(observations, list):
        for observation in observations[:MAX_OBSERVATIONS_IN_SUMMARY]:
            if not isinstance(observation, dict):
                continue
            observed = []
            observed_target = observation.get("target")
            if isinstance(observed_target, dict):
                for key in (
                    "business_class",
                    "business_method",
                    "logger_class",
                    "logger_line",
                ):
                    value = _safe_text(observed_target.get(key), limit=300)
                    if value:
                        observed.append(value)
            message = _safe_text(observation.get("summary"), limit=700)
            if message:
                observed.append(message)
            if observed:
                parts.append("观测: " + " | ".join(observed))
    summary = "; ".join(parts)
    if len(summary) > MAX_SUMMARY_LENGTH:
        summary = summary[:MAX_SUMMARY_LENGTH].rstrip()
    if summary == "日志平台自动采集到故障":
        raise LogTaskIngestionError(
            "sanitized incident has no repository-routing or error evidence"
        )
    return summary


def _aggregation_basis(record: Mapping[str, Any]) -> str:
    components = _aggregation_components(record)
    parts = []
    strategy = _safe_text(record.get("grouping_strategy"), limit=80)
    if strategy:
        parts.append(f"grouping={strategy}")
    for label, key in (
        ("service", "services"),
        ("path", "paths"),
        ("exception", "exceptions"),
        ("system", "systems"),
        ("top_frame", "top_frames"),
    ):
        values = components[key]
        if values:
            parts.append(f"{label}={','.join(values[:20])}")
    basis = "; ".join(parts)
    if not basis:
        basis = "grouping=deterministic-sanitized-incident"
    return basis[:1000]


def build_log_task_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    source_reference = _safe_text(record.get("source_reference"), limit=128)
    if not source_reference:
        raise LogTaskIngestionError("sanitized inbox record has no source reference")
    first_seen = _safe_text(record.get("first_seen_at"), limit=64)
    last_seen = _safe_text(record.get("last_seen_at"), limit=64)
    if not first_seen or not last_seen:
        raise LogTaskIngestionError("sanitized inbox record has no time boundary")
    current_count = _nonnegative_int(record, "latest_batch_event_count")
    historical_count = _nonnegative_int(record, "total_event_count")
    incident_count = _nonnegative_int(record, "candidate_count")
    identifier_count = _nonnegative_int(record, "user_identifier_event_count")
    if min(current_count, historical_count, incident_count) < 1:
        raise LogTaskIngestionError("sanitized inbox record has empty occurrence counts")
    user_min = record.get("affected_user_count_min")
    user_max = record.get("affected_user_count_max")
    if (user_min is None) != (user_max is None):
        raise LogTaskIngestionError("sanitized inbox record has an incomplete user range")
    if user_min is not None and (
        not isinstance(user_min, int)
        or isinstance(user_min, bool)
        or not isinstance(user_max, int)
        or isinstance(user_max, bool)
        or user_min < 0
        or user_min > user_max
    ):
        raise LogTaskIngestionError("sanitized inbox record has an invalid user range")
    historical_complete = record.get("historical_count_complete")
    if not isinstance(historical_complete, bool):
        raise LogTaskIngestionError(
            "sanitized inbox record has no historical completeness marker"
        )
    return {
        "sourceType": "LOG",
        "input": _task_summary(record),
        "logIncident": {
            "dataSafetyStatus": "SANITIZED",
            "sourceReference": source_reference,
            "firstSeenAt": first_seen,
            "lastSeenAt": last_seen,
            "currentScanEventCount": current_count,
            "historicalEventCount": historical_count,
            "incidentGroupCount": incident_count,
            "affectedEndpoints": _safe_list(record.get("affected_endpoints")),
            "affectedUserCountMin": user_min,
            "affectedUserCountMax": user_max,
            "userIdentifierEventCount": identifier_count,
            "historicalCountComplete": historical_complete,
            "aggregationBasis": _aggregation_basis(record),
        },
    }


def submit_summary_to_control_plane(
    summary_path: Path,
    summary: Mapping[str, Any],
    inbox: LogIncidentInbox,
    client: ControlPlaneLogClient,
) -> dict[str, Any]:
    ingestion = inbox.ingest_summary(summary_path)
    candidates = summary.get("candidates")
    if not isinstance(candidates, list):
        raise LogTaskIngestionError("log connector summary has invalid candidates")
    submitted: list[str] = []
    reused: list[str] = []
    handled_incidents: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise LogTaskIngestionError("log connector returned an invalid candidate")
        signature = candidate.get("issue_signature")
        signature = signature if isinstance(signature, dict) else {}
        record = inbox.find(
            source_reference=str(candidate.get("incident_ref", "")),
            issue_fingerprint=str(signature.get("fingerprint", "")),
        )
        if not isinstance(record, dict):
            raise LogTaskIngestionError(
                "sanitized log candidate was not acknowledged by the inbox"
            )
        incident_id = str(record.get("incident_id", ""))
        if incident_id in handled_incidents:
            continue
        handled_incidents.add(incident_id)
        workflow_run_id = str(record.get("workflow_run_id") or "")
        if workflow_run_id:
            reused.append(workflow_run_id)
            continue
        task = client.create_task(build_log_task_payload(record))
        task_id = str(task["id"])
        task_status = str(task.get("status", ""))
        inbox.update(
            incident_id,
            status="blocked" if task_status == "NEEDS_CONTEXT" else "executing",
            workflow_run_id=task_id,
            failure=(
                {"code": "repository_needs_context"}
                if task_status == "NEEDS_CONTEXT"
                else None
            ),
        )
        submitted.append(task_id)
    return {
        "candidates": len(candidates),
        "inbox_added": ingestion["added"],
        "inbox_deduplicated": ingestion["deduplicated"],
        "submitted_task_ids": submitted,
        "reused_task_ids": reused,
    }


def run_once(
    config: LogTaskIngestionConfig,
    *,
    client: Optional[ControlPlaneLogClient] = None,
    poller: Callable[..., tuple[Path, dict[str, Any]]] = _poll_log_candidates,
    cursor_committer: Callable[..., None] = _commit_log_scan_cursor,
) -> dict[str, Any]:
    config = config.validated()
    resolved_client = client or ControlPlaneLogClient(
        config.control_plane_url,
        config.request_timeout_seconds,
    )
    terminal = Terminal(stream=io.StringIO(), color=False)
    summary_path, summary = poller(
        root=Path.cwd(),
        terminal=terminal,
        discover_url=config.discover_url,
        username=config.username,
        password=config.password,
        output_path=config.output_path,
        key_path=config.key_path,
        scan_state_path=config.scan_state_path,
        history_state_path=Path(".issue-entry-state/log-history-cursor.json"),
        history_scan=False,
        max_scan_hits=config.max_scan_hits,
        initial_scan_hits=config.initial_scan_hits,
    )
    result = submit_summary_to_control_plane(
        summary_path,
        summary,
        LogIncidentInbox(config.inbox_path),
        resolved_client,
    )
    cursor_committer(
        discover_url=config.discover_url,
        scan_state_path=config.scan_state_path,
        summary_path=summary_path,
        summary=summary,
    )
    result["cursor_committed"] = True
    result["backlog_remaining"] = bool(
        summary.get("query", {}).get("backlog_remaining", False)
    )
    return result


def build_parser() -> argparse.ArgumentParser:
    local_source = _load_local_source_settings()
    parser = argparse.ArgumentParser(
        description=(
            "Fetch one bounded sanitized log batch and submit it to the local control plane."
        )
    )
    parser.add_argument(
        "--configure",
        action="store_true",
        help="Save the URL and username locally and prompt into macOS Keychain.",
    )
    parser.add_argument("--once", action="store_true", help="Process one bounded batch and exit.")
    parser.add_argument(
        "--discover-url",
        default=(
            os.getenv(DISCOVER_URL_ENV, "").strip()
            or local_source.get("discover_url", "")
        ),
    )
    parser.add_argument(
        "--username",
        default=(
            os.getenv(kibana_issue_connector.USERNAME_ENV, "").strip()
            or local_source.get("username", "")
        ),
    )
    parser.add_argument(
        "--prompt-password",
        action="store_true",
        help="Read the OpenSearch password without echoing or persisting it.",
    )
    parser.add_argument(
        "--control-plane-url",
        default=os.getenv(CONTROL_PLANE_URL_ENV, DEFAULT_CONTROL_PLANE_URL),
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_LOG_OUTPUT_PATH)
    parser.add_argument("--key-file", type=Path, default=DEFAULT_LOG_KEY_PATH)
    parser.add_argument("--scan-state-file", type=Path, default=DEFAULT_LOG_SCAN_STATE_PATH)
    parser.add_argument("--inbox-file", type=Path, default=DEFAULT_LOG_INBOX_PATH)
    parser.add_argument(
        "--max-scan-hits",
        type=int,
        default=kibana_issue_connector.DEFAULT_MAX_SCAN_HITS,
    )
    parser.add_argument(
        "--initial-scan-hits",
        type=int,
        default=kibana_issue_connector.DEFAULT_INITIAL_SCAN_HITS,
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.configure:
        if args.once:
            print("error: --configure and --once are mutually exclusive", file=sys.stderr)
            return 2
        try:
            configure_local_source(args.discover_url, args.username)
        except (OSError, ValueError) as exception:
            print(f"error: {exception}", file=sys.stderr)
            return 2
        print("log platform URL and username saved; password stored in macOS Keychain")
        return 0
    if not args.once:
        print(
            "error: use --configure for local setup or --once for one bounded scan; "
            "durable scheduling is not implemented",
            file=sys.stderr,
        )
        return 2
    password = (
        os.getenv(kibana_issue_connector.PASSWORD_ENV, "")
        or _load_keychain_log_password(args.discover_url, args.username)
    )
    if args.prompt_password and not password:
        password = getpass.getpass("OpenSearch password: ")
    try:
        timeout = float(os.getenv(REQUEST_TIMEOUT_ENV, "5"))
        result = run_once(
            LogTaskIngestionConfig(
                discover_url=args.discover_url,
                username=args.username,
                password=password,
                control_plane_url=args.control_plane_url,
                output_path=args.output_dir,
                key_path=args.key_file,
                scan_state_path=args.scan_state_file,
                inbox_path=args.inbox_file,
                max_scan_hits=args.max_scan_hits,
                initial_scan_hits=args.initial_scan_hits,
                request_timeout_seconds=timeout,
            )
        )
    except (OSError, ValueError) as exception:
        print(f"error: {exception}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
