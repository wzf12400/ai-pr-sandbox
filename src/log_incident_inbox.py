"""Persistent, sanitized incident inbox for the terminal control center."""

from __future__ import annotations

import hashlib
import fcntl
import functools
import json
import os
import re
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from src import ai_issue_generator
from src.issue_entry import compose_evidence


INBOX_SCHEMA_VERSION = "local-log-incident-inbox/v1"
INCIDENT_ID_PATTERN = re.compile(r"INC-[0-9A-F]{12}")
ISSUE_FINGERPRINT_PATTERN = re.compile(r"issue_ref:[0-9a-f]{20}")
MAX_INBOX_BYTES = 8_000_000
MAX_ARTIFACT_BYTES = 1_000_000
MAX_SOURCE_REFERENCES_PER_INCIDENT = 5_000
ACTIVE_STATUSES = frozenset(
    {
        "pending",
        "snoozed",
        "preparing",
        "awaiting_approval",
        "executing",
        "blocked",
    }
)
KNOWN_STATUSES = ACTIVE_STATUSES | frozenset({"ignored", "completed"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("日志收件箱不能是符号链接。")
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


def _incident_id(reference: str) -> str:
    digest = hashlib.sha256(reference.encode("utf-8")).hexdigest()[:12].upper()
    return f"INC-{digest}"


def _safe_text(value: Any, *, limit: int = 200) -> str:
    return " ".join(str(value or "").split())[:limit]


def _nonnegative_int(value: Any, default: int = 0) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return default


def _time_boundary(left: Any, right: Any, *, latest: bool) -> str:
    values = [
        _safe_text(item, limit=64)
        for item in (left, right)
        if _safe_text(item, limit=64)
    ]
    if not values:
        return ""
    return max(values) if latest else min(values)


def _string_list(value: Any, *, limit: int = 50) -> List[str]:
    if not isinstance(value, list):
        return []
    return sorted(
        {
            _safe_text(item, limit=200)
            for item in value[:limit]
            if _safe_text(item, limit=200)
        }
    )


def _candidate_statistics(
    compact: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> Dict[str, Any]:
    event = compact.get("event")
    event = event if isinstance(event, dict) else {}
    statistics = event.get("statistics")
    statistics = statistics if isinstance(statistics, dict) else {}
    batch_event_count = _nonnegative_int(
        candidate.get("event_count"),
        _nonnegative_int(
            statistics.get("batch_event_count"),
            _nonnegative_int(event.get("event_count")),
        ),
    )
    affected_user_count = statistics.get("affected_user_count_min")
    if not isinstance(affected_user_count, int) or isinstance(
        affected_user_count,
        bool,
    ):
        affected_user_count = None
    signature = candidate.get("issue_signature")
    signature = signature if isinstance(signature, dict) else {}
    components = signature.get("fingerprint_components")
    if not isinstance(components, dict):
        components = signature.get("components")
    components = components if isinstance(components, dict) else {}
    return {
        "batch_event_count": batch_event_count,
        "affected_endpoints": _string_list(
            statistics.get("affected_endpoints")
        ),
        "affected_user_count": affected_user_count,
        "user_identifier_event_count": _nonnegative_int(
            statistics.get("user_identifier_event_count")
        ),
        "aggregation_components": {
            key: _string_list(components.get(key))
            for key in (
                "services",
                "paths",
                "exceptions",
                "systems",
                "top_frames",
            )
        },
    }


def _with_cumulative_statistics(
    evidence: Mapping[str, Any],
    *,
    total_event_count: int,
    latest_batch_event_count: int,
    candidate_count: int,
    first_seen_at: str,
    last_seen_at: str,
    affected_endpoints: List[str],
    affected_user_count_min: Optional[int],
    affected_user_count_max: Optional[int],
    user_identifier_event_count: int,
    aggregation_components: Mapping[str, Any],
    historical_count_complete: bool,
) -> Dict[str, Any]:
    updated = json.loads(json.dumps(evidence, ensure_ascii=False))
    event = updated.get("event")
    if not isinstance(event, dict):
        event = {}
        updated["event"] = event
    statistics = event.get("statistics")
    if not isinstance(statistics, dict):
        statistics = {}
    statistics.update(
        {
            "batch_event_count": latest_batch_event_count,
            "total_event_count": total_event_count,
            "candidate_count": candidate_count,
            "affected_endpoints": affected_endpoints,
            "affected_user_count_min": affected_user_count_min,
            "affected_user_count_max": affected_user_count_max,
            "user_identifier_event_count": user_identifier_event_count,
            "aggregation_components": dict(aggregation_components),
            "historical_count_complete": historical_count_complete,
        }
    )
    event["statistics"] = statistics
    runtime = updated.get("runtime")
    if not isinstance(runtime, dict):
        runtime = {}
        updated["runtime"] = runtime
    runtime["first_seen_at"] = first_seen_at
    runtime["last_seen_at"] = last_seen_at
    return updated


def _with_inbox_lock(exclusive: bool):
    def decorate(function):
        @functools.wraps(function)
        def wrapped(self, *args, **kwargs):
            with self._locked(exclusive=exclusive):
                return function(self, *args, **kwargs)

        return wrapped

    return decorate


class LogIncidentInbox:
    """Store only minimized, AI-eligible evidence and auditable local state."""

    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def _locked(self, *, exclusive: bool):
        lock_path = self.path.with_suffix(f"{self.path.suffix}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        if lock_path.is_symlink():
            raise ValueError("日志收件箱锁文件无效。")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise ValueError("日志收件箱锁文件不可用。") from exc
        try:
            os.chmod(lock_path, 0o600)
            operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(descriptor, operation)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _empty(self) -> Dict[str, Any]:
        return {
            "schema_version": INBOX_SCHEMA_VERSION,
            "updated_at": _utc_now(),
            "incidents": {},
        }

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        if self.path.is_symlink() or self.path.stat().st_size > MAX_INBOX_BYTES:
            raise ValueError("本地日志收件箱文件无效。")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("本地日志收件箱不可读。") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != INBOX_SCHEMA_VERSION
            or not isinstance(payload.get("incidents"), dict)
        ):
            raise ValueError("本地日志收件箱结构无效。")
        return payload

    def _save(self, payload: Dict[str, Any]) -> None:
        payload["updated_at"] = _utc_now()
        _atomic_write(self.path, payload)

    @_with_inbox_lock(True)
    def ingest_summary(self, summary_path: Path) -> Dict[str, int]:
        if (
            not summary_path.exists()
            or summary_path.is_symlink()
            or summary_path.stat().st_size > MAX_ARTIFACT_BYTES
        ):
            raise ValueError("日志轮询摘要无效。")
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("日志轮询摘要不可读。") from exc
        candidates = summary.get("candidates", []) if isinstance(summary, dict) else []
        if not isinstance(candidates, list):
            raise ValueError("日志轮询摘要候选结构无效。")

        state = self._load()
        incidents = state["incidents"]
        by_reference: Dict[str, str] = {}
        for incident_id, item in incidents.items():
            if not isinstance(item, dict):
                continue
            references = item.get("source_references")
            if not isinstance(references, list):
                references = [item.get("source_reference")]
            for reference in references:
                value = str(reference or "")
                if value:
                    by_reference[value] = incident_id
        by_fingerprint = {
            str(item.get("issue_fingerprint", "")): incident_id
            for incident_id, item in incidents.items()
            if isinstance(item, dict) and item.get("issue_fingerprint")
        }
        added = 0
        deduplicated = 0
        root = summary_path.parent.resolve()
        batch_ref = hashlib.sha256(
            str(summary_path.resolve()).encode("utf-8")
        ).hexdigest()[:20]

        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            artifact_value = str(candidate.get("artifact", "")).strip()
            artifact_path = Path(artifact_value)
            if not artifact_path.is_absolute():
                artifact_path = Path.cwd() / artifact_path
            if artifact_path.is_symlink():
                raise ValueError("日志候选证据路径无效。")
            artifact = artifact_path.resolve()
            if (
                not artifact.is_relative_to(root)
                or not artifact.exists()
                or artifact.stat().st_size > MAX_ARTIFACT_BYTES
            ):
                raise ValueError("日志候选证据路径无效。")
            evidence = json.loads(artifact.read_text(encoding="utf-8"))
            if (
                not isinstance(evidence, dict)
                or evidence.get("schema_version") != "sanitized-kibana-incident/v1"
            ):
                raise ValueError("日志候选不是已脱敏的聚合异常。")
            compact = ai_issue_generator.compact_evidence(evidence)
            reference = _safe_text(compact.get("source", {}).get("reference"), limit=120)
            if not reference:
                raise ValueError("日志候选缺少稳定事件引用。")
            signature = candidate.get("issue_signature", {})
            fingerprint = (
                _safe_text(signature.get("fingerprint"), limit=64)
                if isinstance(signature, dict)
                else ""
            )
            if fingerprint and not ISSUE_FINGERPRINT_PATTERN.fullmatch(fingerprint):
                raise ValueError("日志候选指纹无效。")
            reference_existing_id = by_reference.get(reference)
            fingerprint_existing_id = (
                by_fingerprint.get(fingerprint) if fingerprint else None
            )
            existing_id = reference_existing_id or fingerprint_existing_id
            now = _utc_now()
            if existing_id:
                record = incidents[existing_id]
                if reference_existing_id:
                    record["updated_at"] = now
                    deduplicated += 1
                    continue
                candidate_statistics = _candidate_statistics(compact, candidate)
                batch_event_count = candidate_statistics["batch_event_count"]
                previous_candidate_count = _nonnegative_int(
                    record.get("candidate_count"),
                    _nonnegative_int(record.get("occurrence_count"), 1),
                )
                previous_total = _nonnegative_int(
                    record.get("total_event_count"),
                    _nonnegative_int(record.get("event_count")),
                )
                historical_count_complete = record.get(
                    "historical_count_complete"
                )
                if not isinstance(historical_count_complete, bool):
                    historical_count_complete = (
                        "total_event_count" in record
                        or previous_candidate_count <= 1
                    )
                total_event_count = previous_total + batch_event_count
                candidate_count = previous_candidate_count + 1
                if record.get("latest_batch_ref") == batch_ref:
                    latest_batch_event_count = _nonnegative_int(
                        record.get("latest_batch_event_count")
                    ) + batch_event_count
                else:
                    latest_batch_event_count = batch_event_count
                first_seen_at = _time_boundary(
                    record.get("first_seen_at"),
                    candidate.get("first_seen_at"),
                    latest=False,
                )
                last_seen_at = _time_boundary(
                    record.get("last_seen_at"),
                    candidate.get("last_seen_at"),
                    latest=True,
                )
                affected_endpoints = sorted(
                    set(_string_list(record.get("affected_endpoints")))
                    | set(candidate_statistics["affected_endpoints"])
                )
                previous_user_min = record.get("affected_user_count_min")
                previous_user_max = record.get("affected_user_count_max")
                new_user_count = candidate_statistics["affected_user_count"]
                if not isinstance(previous_user_min, int) or isinstance(
                    previous_user_min,
                    bool,
                ):
                    previous_user_min = None
                if not isinstance(previous_user_max, int) or isinstance(
                    previous_user_max,
                    bool,
                ):
                    previous_user_max = None
                if new_user_count is None:
                    affected_user_min = previous_user_min
                    affected_user_max = previous_user_max
                elif previous_user_min is None or previous_user_max is None:
                    affected_user_min = new_user_count
                    affected_user_max = new_user_count
                else:
                    affected_user_min = max(previous_user_min, new_user_count)
                    affected_user_max = previous_user_max + new_user_count
                user_identifier_event_count = _nonnegative_int(
                    record.get("user_identifier_event_count")
                ) + candidate_statistics["user_identifier_event_count"]
                aggregation_components = candidate_statistics[
                    "aggregation_components"
                ]
                if not any(aggregation_components.values()):
                    existing_event = record.get("evidence", {}).get("event", {})
                    existing_statistics = (
                        existing_event.get("statistics", {})
                        if isinstance(existing_event, dict)
                        else {}
                    )
                    existing_components = (
                        existing_statistics.get("aggregation_components", {})
                        if isinstance(existing_statistics, dict)
                        else {}
                    )
                    if isinstance(existing_components, dict):
                        aggregation_components = existing_components
                raw_source_references = record.get("source_references")
                if not isinstance(raw_source_references, list):
                    raw_source_references = [record.get("source_reference")]
                source_references = _string_list(
                    raw_source_references,
                    limit=MAX_SOURCE_REFERENCES_PER_INCIDENT,
                )
                if (
                    reference not in source_references
                    and len(source_references) < MAX_SOURCE_REFERENCES_PER_INCIDENT
                ):
                    source_references.append(reference)
                    source_references.sort()
                evidence = _with_cumulative_statistics(
                    record.get("evidence", {}),
                    total_event_count=total_event_count,
                    latest_batch_event_count=latest_batch_event_count,
                    candidate_count=candidate_count,
                    first_seen_at=first_seen_at,
                    last_seen_at=last_seen_at,
                    affected_endpoints=affected_endpoints,
                    affected_user_count_min=affected_user_min,
                    affected_user_count_max=affected_user_max,
                    user_identifier_event_count=user_identifier_event_count,
                    aggregation_components=aggregation_components,
                    historical_count_complete=historical_count_complete,
                )
                record.update(
                    {
                        "updated_at": now,
                        "first_seen_at": first_seen_at,
                        "last_seen_at": last_seen_at,
                        "event_count": total_event_count,
                        "latest_batch_event_count": latest_batch_event_count,
                        "total_event_count": total_event_count,
                        "candidate_count": candidate_count,
                        "occurrence_count": total_event_count,
                        "latest_batch_ref": batch_ref,
                        "source_references": source_references,
                        "affected_endpoints": affected_endpoints,
                        "affected_user_count_min": affected_user_min,
                        "affected_user_count_max": affected_user_max,
                        "user_identifier_event_count": (
                            user_identifier_event_count
                        ),
                        "historical_count_complete": historical_count_complete,
                        "evidence": evidence,
                    }
                )
                deduplicated += 1
                by_reference[reference] = existing_id
                continue

            incident_id = _incident_id(reference)
            if incident_id in incidents:
                raise ValueError("日志候选标识发生冲突。")
            services = candidate.get("services", [])
            if not isinstance(services, list):
                services = []
            candidate_statistics = _candidate_statistics(compact, candidate)
            batch_event_count = candidate_statistics["batch_event_count"]
            affected_user_count = candidate_statistics["affected_user_count"]
            first_seen_at = _safe_text(
                candidate.get("first_seen_at"),
                limit=64,
            )
            last_seen_at = _safe_text(
                candidate.get("last_seen_at"),
                limit=64,
            )
            evidence = _with_cumulative_statistics(
                compact,
                total_event_count=batch_event_count,
                latest_batch_event_count=batch_event_count,
                candidate_count=1,
                first_seen_at=first_seen_at,
                last_seen_at=last_seen_at,
                affected_endpoints=candidate_statistics["affected_endpoints"],
                affected_user_count_min=affected_user_count,
                affected_user_count_max=affected_user_count,
                user_identifier_event_count=candidate_statistics[
                    "user_identifier_event_count"
                ],
                aggregation_components=candidate_statistics[
                    "aggregation_components"
                ],
                historical_count_complete=True,
            )
            record = {
                "incident_id": incident_id,
                "status": "pending",
                "source_reference": reference,
                "source_references": [reference],
                "issue_fingerprint": fingerprint,
                "services": [_safe_text(item, limit=120) for item in services[:10]],
                "event_count": batch_event_count,
                "latest_batch_event_count": batch_event_count,
                "total_event_count": batch_event_count,
                "candidate_count": 1,
                "latest_batch_ref": batch_ref,
                "first_seen_at": first_seen_at,
                "last_seen_at": last_seen_at,
                "grouping_strategy": _safe_text(
                    candidate.get("grouping_strategy"), limit=80
                ),
                "occurrence_count": batch_event_count,
                "affected_endpoints": candidate_statistics[
                    "affected_endpoints"
                ],
                "affected_user_count_min": affected_user_count,
                "affected_user_count_max": affected_user_count,
                "user_identifier_event_count": candidate_statistics[
                    "user_identifier_event_count"
                ],
                "historical_count_complete": True,
                "discovered_at": now,
                "updated_at": now,
                "snoozed_until": None,
                "workflow_run_id": None,
                "issue_url": None,
                "draft_pr_url": None,
                "approval": None,
                "failure": None,
                "evidence": evidence,
            }
            incidents[incident_id] = record
            by_reference[reference] = incident_id
            if fingerprint:
                by_fingerprint[fingerprint] = incident_id
            added += 1
        if added or deduplicated or not self.path.exists():
            self._save(state)
        return {
            "candidates": len(candidates),
            "added": added,
            "deduplicated": deduplicated,
        }

    @_with_inbox_lock(True)
    def list(self, *, include_closed: bool = False) -> List[Dict[str, Any]]:
        records = []
        now = datetime.now(timezone.utc)
        state = self._load()
        changed = False
        for item in state["incidents"].values():
            if not isinstance(item, dict):
                continue
            status = str(item.get("status", ""))
            if status not in KNOWN_STATUSES:
                raise ValueError("日志收件箱包含未知状态。")
            if status == "snoozed":
                value = str(item.get("snoozed_until") or "")
                try:
                    until = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    until = now
                if until <= now:
                    item["status"] = "pending"
                    item["snoozed_until"] = None
                    item["updated_at"] = _utc_now()
                    changed = True
                    status = "pending"
            if include_closed or status in ACTIVE_STATUSES:
                records.append(dict(item))
        if changed:
            self._save(state)
        records.sort(key=lambda item: str(item.get("updated_at", "")), reverse=True)
        records.sort(key=lambda item: str(item.get("status")) != "pending")
        return records

    @_with_inbox_lock(False)
    def get(self, incident_id: str) -> Dict[str, Any]:
        if not INCIDENT_ID_PATTERN.fullmatch(incident_id):
            raise ValueError("异常编号格式无效。")
        record = self._load()["incidents"].get(incident_id)
        if not isinstance(record, dict):
            raise ValueError("异常编号不存在。")
        return dict(record)

    @_with_inbox_lock(False)
    def find(
        self,
        *,
        source_reference: str = "",
        issue_fingerprint: str = "",
    ) -> Optional[Dict[str, Any]]:
        reference = _safe_text(source_reference, limit=120)
        fingerprint = _safe_text(issue_fingerprint, limit=64)
        if not reference and not fingerprint:
            raise ValueError("日志异常查询缺少稳定引用。")
        if fingerprint and not ISSUE_FINGERPRINT_PATTERN.fullmatch(fingerprint):
            raise ValueError("日志候选指纹无效。")
        for record in self._load()["incidents"].values():
            if not isinstance(record, dict):
                continue
            if reference and record.get("source_reference") == reference:
                return dict(record)
            if fingerprint and record.get("issue_fingerprint") == fingerprint:
                return dict(record)
        return None

    @_with_inbox_lock(True)
    def update(self, incident_id: str, **changes: Any) -> Dict[str, Any]:
        if not INCIDENT_ID_PATTERN.fullmatch(incident_id):
            raise ValueError("异常编号格式无效。")
        allowed = {
            "status",
            "snoozed_until",
            "workflow_run_id",
            "issue_url",
            "draft_pr_url",
            "approval",
            "failure",
            "evidence",
        }
        if set(changes) - allowed:
            raise ValueError("日志收件箱更新字段无效。")
        state = self._load()
        record = state["incidents"].get(incident_id)
        if not isinstance(record, dict):
            raise ValueError("异常编号不存在。")
        if "status" in changes and changes["status"] not in KNOWN_STATUSES:
            raise ValueError("日志收件箱状态无效。")
        if "evidence" in changes:
            changes["evidence"] = ai_issue_generator.compact_evidence(
                dict(changes["evidence"])
            )
        record.update(changes)
        record["updated_at"] = _utc_now()
        self._save(state)
        return dict(record)

    def snooze(self, incident_id: str, hours: int = 24) -> Dict[str, Any]:
        if not 1 <= hours <= 24 * 30:
            raise ValueError("稍后处理时长必须在 1 小时到 30 天之间。")
        until = datetime.now(timezone.utc) + timedelta(hours=hours)
        return self.update(
            incident_id,
            status="snoozed",
            snoozed_until=until.isoformat().replace("+00:00", "Z"),
        )

    def ignore(self, incident_id: str) -> Dict[str, Any]:
        return self.update(incident_id, status="ignored", snoozed_until=None)

    def add_context(self, incident_id: str, context: str) -> Dict[str, Any]:
        safe = compose_evidence(context)
        if safe.get("safety", {}).get("security_review_required"):
            raise ValueError("补充上下文包含敏感信息，请删除后重试。")
        record = self.get(incident_id)
        evidence = ai_issue_generator.compact_evidence(dict(record["evidence"]))
        evidence = dict(evidence)
        evidence["facts"] = dict(evidence.get("facts", {}))
        evidence["facts"]["human_context"] = safe["facts"]["reported_description"]
        return self.update(
            incident_id,
            status="pending",
            snoozed_until=None,
            workflow_run_id=None,
            approval=None,
            failure=None,
            evidence=evidence,
        )
