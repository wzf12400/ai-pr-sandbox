"""Deterministically group sanitized Kibana events into auditable incidents."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple


INCIDENT_SCHEMA_VERSION = "sanitized-kibana-incident/v1"
GROUPING_POLICY_VERSION = "kibana-incident-grouping/v1"
ISSUE_SIGNATURE_POLICY_VERSION = "kibana-issue-signature/v1"
FALLBACK_WINDOW_SECONDS = 5.0

EXCEPTION_PATTERN = re.compile(
    r"\b(?:[A-Za-z_$][\w$]*\.)*(?P<name>[A-Za-z_$][\w$]*(?:Exception|Error))\b"
)
METHOD_FRAME_PATTERN = re.compile(
    r"\b(?P<class>(?:[A-Za-z_$][\w$]*\.)*[A-Z][A-Za-z0-9_$]*)"
    r"\.(?P<method>[a-z_$][\w$]*):\d+\b"
)
LINE_FRAME_PATTERN = re.compile(
    r"\b(?P<class>(?:[A-Za-z_$][\w$]*\.)*[A-Z][A-Za-z0-9_$]*):\d+\b"
)
JAVA_STACK_FRAME_PATTERN = re.compile(
    r"\bat\s+(?P<class>(?:[A-Za-z_$][\w$]*\.)*[A-Z][A-Za-z0-9_$]*)"
    r"\.(?P<method>[A-Za-z_$][\w$]*)\([^\r\n)]*\)"
)
REQUEST_PATH_PATTERN = re.compile(r"\brequest_path\s*=\s*(?P<path>/[^\s?;,|]+)")
SYSTEM_ANCHORS = {
    "s3": re.compile(r"(?i)\b(?:amazon\s+)?s3\b"),
    "dynamodb": re.compile(r"(?i)\bdynamodb\b"),
    "elasticsearch": re.compile(r"(?i)\belasticsearch\b"),
    "kafka": re.compile(r"(?i)\bkafka\b"),
    "mongodb": re.compile(r"(?i)\bmongodb\b"),
    "mysql": re.compile(r"(?i)\bmysql\b"),
    "opensearch": re.compile(r"(?i)\bopensearch\b"),
    "postgresql": re.compile(r"(?i)\bpostgres(?:ql)?\b"),
    "rabbitmq": re.compile(r"(?i)\brabbitmq\b"),
    "redis": re.compile(r"(?i)\bredis\b"),
}


def _mapping(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _timestamp(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _event_ref(event: Dict[str, Any]) -> str:
    return _text(_mapping(event.get("source")).get("event_ref"))


def _trace_ref(event: Dict[str, Any]) -> str:
    return _text(_mapping(event.get("event")).get("trace_ref"))


def _service(event: Dict[str, Any]) -> str:
    return _text(_mapping(event.get("target")).get("service"))


def _event_time(event: Dict[str, Any]) -> Optional[datetime]:
    return _timestamp(_text(_mapping(event.get("source")).get("timestamp")))


def _simple_name(value: str) -> str:
    return value.rsplit(".", 1)[-1].lower()


def event_signatures(event: Dict[str, Any]) -> Set[str]:
    """Extract only fixed, software-semantic signatures from sanitized fields."""
    target = _mapping(event.get("target"))
    details = _mapping(event.get("event"))
    summary = _text(details.get("summary") or details.get("safe_summary"))
    signatures: Set[str] = set()

    for class_field, method_field in (
        ("logger_class", ""),
        ("business_class", "business_method"),
    ):
        class_name = _text(target.get(class_field))
        method_name = _text(target.get(method_field)) if method_field else ""
        if class_name:
            frame = _simple_name(class_name)
            signatures.add(f"frame:{frame}")
            if method_name:
                signatures.add(f"frame:{frame}.{method_name.lower()}")

    for match in EXCEPTION_PATTERN.finditer(summary):
        signatures.add(f"exception:{_simple_name(match.group('name'))}")
    for match in METHOD_FRAME_PATTERN.finditer(summary):
        class_name = _simple_name(match.group("class"))
        method_name = _text(match.group("method"))
        signatures.add(f"frame:{class_name}")
        signatures.add(f"frame:{class_name}.{method_name.lower()}")
    for match in LINE_FRAME_PATTERN.finditer(summary):
        signatures.add(f"frame:{_simple_name(match.group('class'))}")
    for match in JAVA_STACK_FRAME_PATTERN.finditer(summary):
        class_name = _simple_name(match.group("class"))
        method_name = _text(match.group("method"))
        signatures.add(f"frame:{class_name}")
        signatures.add(f"frame:{class_name}.{method_name.lower()}")
    for name, pattern in SYSTEM_ANCHORS.items():
        if pattern.search(summary):
            signatures.add(f"system:{name}")
    return signatures


def issue_signature(incident: Dict[str, Any]) -> Dict[str, Any]:
    """Build a conservative, auditable cross-trace Issue deduplication key."""
    if incident.get("schema_version") != INCIDENT_SCHEMA_VERSION:
        raise ValueError("Issue signature requires a sanitized Kibana incident")
    members = incident.get("members")
    if not isinstance(members, list) or not members:
        raise ValueError("Issue signature requires incident members")

    services = sorted({_service(member) for member in members if _service(member)})
    paths: Set[str] = set()
    exceptions: Set[str] = set()
    systems: Set[str] = set()
    top_frames: Set[str] = set()
    for member in members:
        summary = _text(_mapping(member.get("event")).get("summary"))
        paths.update(match.group("path") for match in REQUEST_PATH_PATTERN.finditer(summary))
        signatures = event_signatures(member)
        exceptions.update(
            item.removeprefix("exception:")
            for item in signatures
            if item.startswith("exception:")
        )
        systems.update(
            item.removeprefix("system:")
            for item in signatures
            if item.startswith("system:")
        )
        first_frame = JAVA_STACK_FRAME_PATTERN.search(summary)
        if first_frame:
            top_frames.add(
                f"{_simple_name(first_frame.group('class'))}."
                f"{_text(first_frame.group('method')).lower()}"
            )

    semantic_dimensions = sum(
        bool(values) for values in (paths, exceptions, systems, top_frames)
    )
    eligible = len(services) == 1 and semantic_dimensions >= 2
    components = {
        "services": services,
        "paths": sorted(paths),
        "exceptions": sorted(exceptions),
        "systems": sorted(systems),
        "top_frames": sorted(top_frames),
    }
    fingerprint = ""
    if eligible:
        encoded = json.dumps(
            {
                "policy_version": ISSUE_SIGNATURE_POLICY_VERSION,
                **components,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        fingerprint = "issue_ref:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]
    return {
        "policy_version": ISSUE_SIGNATURE_POLICY_VERSION,
        "eligible": eligible,
        "fingerprint": fingerprint,
        "criteria": [
            "one_nonempty_service",
            "at_least_two_semantic_dimensions",
            "exact_signature_equality",
        ],
        "components": components,
    }


def _fallback_match(
    left: Dict[str, Any], right: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    if _trace_ref(left) or _trace_ref(right):
        return None
    left_service = _service(left)
    if not left_service or left_service != _service(right):
        return None
    left_time = _event_time(left)
    right_time = _event_time(right)
    if left_time is None or right_time is None:
        return None
    delta_seconds = abs((left_time - right_time).total_seconds())
    if delta_seconds > FALLBACK_WINDOW_SECONDS:
        return None
    delta_ms = round(delta_seconds * 1000)

    shared = event_signatures(left) & event_signatures(right)
    shared_exceptions = sorted(item for item in shared if item.startswith("exception:"))
    shared_frames = sorted(item for item in shared if item.startswith("frame:"))
    shared_systems = sorted(item for item in shared if item.startswith("system:"))
    if shared_exceptions and shared_frames:
        rule = "same_service_time_exception_and_frame"
        matched = shared_exceptions + shared_frames
    elif shared_frames and shared_systems:
        rule = "same_service_time_frame_and_system"
        matched = shared_frames + shared_systems
    elif left_time == right_time and (shared_exceptions or shared_systems):
        rule = "same_service_exact_timestamp_and_signature"
        matched = shared_exceptions + shared_systems
    else:
        return None
    return {
        "left_event_ref": _event_ref(left),
        "right_event_ref": _event_ref(right),
        "rule": rule,
        "time_delta_ms": delta_ms,
        "shared_signatures": matched,
    }


def _sort_key(event: Dict[str, Any]) -> Tuple[datetime, str]:
    timestamp = _event_time(event)
    return timestamp or datetime.min.replace(tzinfo=timezone.utc), _event_ref(event)


def _incident_ref(strategy: str, members: Sequence[Dict[str, Any]]) -> str:
    if strategy == "trace_ref":
        identity = f"trace:{_trace_ref(members[0])}"
    else:
        identity = "events:" + ",".join(sorted(_event_ref(member) for member in members))
    digest = hashlib.sha256(
        f"{GROUPING_POLICY_VERSION}:{identity}".encode("utf-8")
    ).hexdigest()
    return f"incident_ref:{digest[:20]}"


def _build_incident(
    strategy: str,
    members: Sequence[Dict[str, Any]],
    links: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    ordered = sorted(members, key=_sort_key)
    timestamps = [
        _text(_mapping(member.get("source")).get("timestamp")) for member in ordered
    ]
    sanitizations = [_mapping(member.get("sanitization")) for member in ordered]
    incident_ref = _incident_ref(strategy, ordered)
    criteria = {
        "trace_ref": ["equal_nonempty_trace_ref"],
        "fallback_similarity": [
            "same_nonempty_service",
            "bounded_timestamp",
            "shared_software_signature",
            "complete_link",
        ],
        "single_event": ["no_deterministic_match"],
    }[strategy]
    return {
        "schema_version": INCIDENT_SCHEMA_VERSION,
        "source": {
            "type": "kibana",
            "incident_ref": incident_ref,
            "event_refs": [_event_ref(member) for member in ordered],
            "first_seen_at": timestamps[0] if timestamps else "",
            "last_seen_at": timestamps[-1] if timestamps else "",
        },
        "incident": {
            "event_count": len(ordered),
            "trace_ref": _trace_ref(ordered[0]) if strategy == "trace_ref" else "",
        },
        "grouping": {
            "policy_version": GROUPING_POLICY_VERSION,
            "strategy": strategy,
            "criteria": criteria,
            "fallback_window_seconds": FALLBACK_WINDOW_SECONDS,
            "links": list(links),
        },
        "members": ordered,
        "sanitization": {
            "status": "passed"
            if all(item.get("status") == "passed" for item in sanitizations)
            else "passed_with_redactions",
            "ai_allowed": all(item.get("ai_allowed", False) for item in sanitizations),
            "github_issue_allowed": all(
                item.get("github_issue_allowed", False) for item in sanitizations
            ),
            "security_review_required": any(
                item.get("security_review_required", False) for item in sanitizations
            ),
        },
    }


def group_sanitized_events(events: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group eligible events without model judgment or transitive bridge matches."""
    for event in events:
        if event.get("schema_version") != "sanitized-kibana-event/v1":
            raise ValueError("incident grouping requires sanitized Kibana events")
        if not _event_ref(event):
            raise ValueError("incident grouping requires an event_ref")
        if not _mapping(event.get("sanitization")).get("ai_allowed", False):
            raise ValueError("blocked events cannot enter incident grouping")

    ordered = sorted(events, key=_sort_key)
    trace_groups: Dict[str, List[Dict[str, Any]]] = {}
    without_trace: List[Dict[str, Any]] = []
    for event in ordered:
        trace_ref = _trace_ref(event)
        if trace_ref:
            trace_groups.setdefault(trace_ref, []).append(event)
        else:
            without_trace.append(event)

    incidents = [
        _build_incident("trace_ref", members, [])
        for _, members in sorted(trace_groups.items())
    ]

    fallback_groups: List[List[Dict[str, Any]]] = []
    fallback_links: List[List[Dict[str, Any]]] = []
    for event in without_trace:
        matches: List[Tuple[int, List[Dict[str, Any]]]] = []
        for index, members in enumerate(fallback_groups):
            links = [_fallback_match(member, event) for member in members]
            if all(link is not None for link in links):
                matches.append((index, [link for link in links if link is not None]))
        if matches:
            index, links = min(matches, key=lambda item: _event_ref(fallback_groups[item[0]][0]))
            fallback_groups[index].append(event)
            fallback_links[index].extend(links)
        else:
            fallback_groups.append([event])
            fallback_links.append([])

    for members, links in zip(fallback_groups, fallback_links):
        strategy = "fallback_similarity" if len(members) > 1 else "single_event"
        incidents.append(_build_incident(strategy, members, links))

    return sorted(
        incidents,
        key=lambda incident: (
            _text(_mapping(incident.get("source")).get("last_seen_at")),
            _text(_mapping(incident.get("source")).get("incident_ref")),
        ),
        reverse=True,
    )
