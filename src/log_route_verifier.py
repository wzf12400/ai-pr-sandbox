"""Verify repository-name log selectors without reading log document bodies."""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from src import kibana_issue_connector
from src.log_task_ingestor import _load_local_source_settings
from src.terminal_control_center import _load_keychain_log_password

MAX_SELECTORS = 50
SELECTOR_FIELD = "kubernetes.container_name.keyword"


def verify_selector_values(values: list[str]) -> list[dict[str, Any]]:
    if not 1 <= len(values) <= MAX_SELECTORS:
        raise ValueError("selector count is invalid")
    if any(
        not kibana_issue_connector.EXACT_SELECTOR_VALUE_PATTERN.fullmatch(value)
        for value in values
    ):
        raise ValueError("selector value is invalid")
    discover_url = os.environ.get("OPENSEARCH_DISCOVER_URL", "").strip()
    username = os.environ.get("OPENSEARCH_USERNAME", "").strip()
    settings = (
        {"discover_url": discover_url, "username": username}
        if discover_url and username
        else _load_local_source_settings()
    )
    if not settings:
        raise RuntimeError("log source is not configured")
    password = (
        os.environ.get("OPENSEARCH_PASSWORD", "").strip()
        or _load_keychain_log_password(
            settings["discover_url"], settings["username"]
        )
    )
    if not password:
        raise RuntimeError("log source credential is unavailable")
    target = kibana_issue_connector.parse_discover_url(settings["discover_url"])
    client = kibana_issue_connector.OpenSearchDashboardsClient(
        target,
        kibana_issue_connector.DashboardCredentials(
            username=settings["username"], password=password
        ),
    )
    index_pattern, time_field = client.resolve_index_pattern()
    return [
        {
            "selectorValue": value,
            "found": client.exact_selector_exists(
                index_pattern,
                time_field,
                SELECTOR_FIELD,
                value,
            ),
        }
        for value in values
    ]


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, list) or not all(
            isinstance(value, str) for value in payload
        ):
            raise ValueError("input must be a string array")
        result = verify_selector_values(payload)
    except (OSError, RuntimeError, ValueError) as exception:
        print(
            json.dumps(
                {"status": "error", "detail": type(exception).__name__},
                separators=(",", ":"),
            )
        )
        return 1
    print(json.dumps({"status": "ok", "results": result}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
