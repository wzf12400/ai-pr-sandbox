"""Verify exact Jira project names without reading issue content."""

from __future__ import annotations

import json
import sys
from typing import Any

from src import jira_connector


MAX_PROJECT_NAMES = 50
MAX_PROJECT_NAME_CHARS = 120


def verify_project_names(names: list[str]) -> list[dict[str, Any]]:
    if not 1 <= len(names) <= MAX_PROJECT_NAMES:
        raise ValueError("Jira project name count is invalid")
    normalized_names = []
    for name in names:
        normalized = name.strip()
        if (
            not normalized
            or len(normalized) > MAX_PROJECT_NAME_CHARS
            or any(ord(character) < 32 for character in normalized)
        ):
            raise ValueError("Jira project name is invalid")
        normalized_names.append(normalized)

    payload = jira_connector._get_json("/rest/api/2/project")
    if not isinstance(payload, list):
        raise RuntimeError("Jira project response is invalid")
    projects_by_name: dict[str, list[dict[str, str]]] = {}
    for value in payload:
        if not isinstance(value, dict):
            continue
        key = value.get("key")
        name = value.get("name")
        if (
            not isinstance(key, str)
            or not jira_connector.PROJECT_KEY_PATTERN.fullmatch(key)
            or not isinstance(name, str)
            or not name.strip()
        ):
            continue
        projects_by_name.setdefault(name.strip().casefold(), []).append(
            {"projectKey": key, "projectName": name.strip()}
        )

    results = []
    for requested_name in normalized_names:
        matches = projects_by_name.get(requested_name.casefold(), [])
        if len(matches) == 1:
            results.append(
                {
                    "requestedName": requested_name,
                    "found": True,
                    **matches[0],
                }
            )
        else:
            results.append(
                {
                    "requestedName": requested_name,
                    "found": False,
                    "reason": "ambiguous" if len(matches) > 1 else "not_found",
                }
            )
    return results


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, list) or not all(
            isinstance(value, str) for value in payload
        ):
            raise ValueError("input must be a string array")
        results = verify_project_names(payload)
    except (OSError, RuntimeError, ValueError) as exception:
        print(
            json.dumps(
                {"status": "error", "detail": type(exception).__name__},
                separators=(",", ":"),
            )
        )
        return 1
    print(json.dumps({"status": "ok", "results": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
