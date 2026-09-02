"""Loopback client for database-backed monitor scan leases."""

from __future__ import annotations

import json
import os
import re
import socket
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, Optional

MAX_RESPONSE_BYTES = 1_100_000
DEFAULT_LEASE_SECONDS = 600
INSTANCE_ID = re.sub(
    r"[^A-Za-z0-9._:]",
    "_",
    f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}",
)


def control_plane_base() -> str:
    raw = os.environ.get(
        "CONTROL_PLANE_URL", "http://127.0.0.1:8080"
    ).rstrip("/")
    parsed = urllib.parse.urlparse(raw)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("control plane URL must use loopback HTTP")
    return raw


def _scanner_key(value: str) -> str:
    normalized = value.strip().upper()
    if normalized not in {"JIRA", "LOG"}:
        raise ValueError("scanner key must be JIRA or LOG")
    return normalized


def _request_json(
    path: str,
    *,
    method: str = "GET",
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        control_plane_base() + path,
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RuntimeError("scan coordination response is too large")
    result = json.loads(raw.decode("utf-8"))
    if not isinstance(result, dict):
        raise RuntimeError("scan coordination response is invalid")
    return result


def acquire(
    scanner_key: str,
    *,
    owner_id: str = INSTANCE_ID,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> Dict[str, Any]:
    key = _scanner_key(scanner_key)
    return _request_json(
        f"/api/internal/scan-coordination/{key}/acquire",
        method="POST",
        payload={"ownerId": owner_id, "leaseSeconds": lease_seconds},
    )


def complete(
    scanner_key: str,
    lease_token: str,
    checkpoint: Dict[str, Any],
    *,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    key = _scanner_key(scanner_key)
    if not isinstance(checkpoint, dict):
        raise ValueError("scan checkpoint must be an object")
    return _request_json(
        f"/api/internal/scan-coordination/{key}/complete",
        method="POST",
        payload={
            "leaseToken": lease_token,
            "checkpoint": checkpoint,
            "error": error,
        },
    )


def status(scanner_key: str) -> Dict[str, Any]:
    key = _scanner_key(scanner_key)
    return _request_json(f"/api/internal/scan-coordination/{key}")
