"""Load an operator-approved, secret-free policy for automatic Issue routing."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


POLICY_SCHEMA_VERSION = "issue-auto-publish-policy/v1"
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
POLICY_ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,80}")
MAX_ROUTES = 100
MAX_AUTO_PUBLISH_PER_RUN = 3
ALLOWED_WORKFLOW_STATES = {"ready_for_human_review", "needs_human_context"}


def _mapping(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


@dataclass(frozen=True)
class PublicationRoute:
    route_id: str
    service: str
    provider: str
    repository: str


@dataclass(frozen=True)
class AutoPublishPolicy:
    policy_id: str
    policy_sha256: str
    max_issues_per_run: int
    allowed_states: frozenset[str]
    routes: tuple[PublicationRoute, ...]

    def resolve(self, incident: Dict[str, Any]) -> Optional[PublicationRoute]:
        members = incident.get("members")
        if not isinstance(members, list) or not members:
            return None
        services = {
            _text(_mapping(member.get("target")).get("service"))
            for member in members
        }
        services.discard("")
        if len(services) != 1:
            return None
        service = next(iter(services))
        return next((route for route in self.routes if route.service == service), None)


def _require_policy_id(value: Any, field: str) -> str:
    resolved = _text(value)
    if not POLICY_ID_PATTERN.fullmatch(resolved):
        raise ValueError(f"{field} must contain only letters, numbers, dots, underscores, or hyphens")
    return resolved


def _parse_routes(value: Any) -> tuple[PublicationRoute, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_ROUTES:
        raise ValueError(f"routes must contain between 1 and {MAX_ROUTES} entries")
    routes: List[PublicationRoute] = []
    seen_services = set()
    seen_ids = set()
    for item in value:
        route = _mapping(item)
        route_id = _require_policy_id(route.get("route_id"), "route_id")
        service = _text(_mapping(route.get("match")).get("service"))
        provider = _text(route.get("provider"))
        repository = _text(route.get("repository"))
        if not service or len(service) > 160:
            raise ValueError("route match.service must be a nonempty exact service name")
        if provider != "github_cli":
            raise ValueError("only the github_cli publication provider is currently implemented")
        if not REPOSITORY_PATTERN.fullmatch(repository):
            raise ValueError("route repository must use owner/name format")
        if service in seen_services:
            raise ValueError(f"duplicate service route: {service}")
        if route_id in seen_ids:
            raise ValueError(f"duplicate route_id: {route_id}")
        seen_services.add(service)
        seen_ids.add(route_id)
        routes.append(PublicationRoute(route_id, service, provider, repository))
    return tuple(routes)


def load_policy(path: Path, confirmed_sha256: str) -> AutoPublishPolicy:
    """Load a policy only when the operator-confirmed digest matches its bytes."""
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if not re.fullmatch(r"[0-9a-f]{64}", confirmed_sha256) or digest != confirmed_sha256:
        raise ValueError("automatic publication policy SHA-256 confirmation does not match")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("automatic publication policy is not valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise ValueError(f"automatic publication policy must use {POLICY_SCHEMA_VERSION}")
    policy_id = _require_policy_id(payload.get("policy_id"), "policy_id")
    limit = payload.get("max_issues_per_run")
    if not isinstance(limit, int) or not 1 <= limit <= MAX_AUTO_PUBLISH_PER_RUN:
        raise ValueError(
            f"max_issues_per_run must be between 1 and {MAX_AUTO_PUBLISH_PER_RUN}"
        )
    states = payload.get("allowed_states")
    if not isinstance(states, list) or not states:
        raise ValueError("allowed_states must be a nonempty list")
    normalized_states = frozenset(_text(state) for state in states)
    if "" in normalized_states or not normalized_states <= ALLOWED_WORKFLOW_STATES:
        raise ValueError("automatic publication policy contains an unsupported workflow state")
    return AutoPublishPolicy(
        policy_id=policy_id,
        policy_sha256=digest,
        max_issues_per_run=limit,
        allowed_states=normalized_states,
        routes=_parse_routes(payload.get("routes")),
    )


def policy_summary(policy: AutoPublishPolicy) -> Dict[str, Any]:
    """Return audit metadata without serializing the complete routing policy."""
    return {
        "policy_id": policy.policy_id,
        "policy_sha256": policy.policy_sha256,
        "max_issues_per_run": policy.max_issues_per_run,
        "allowed_states": sorted(policy.allowed_states),
        "route_count": len(policy.routes),
    }
