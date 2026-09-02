#!/usr/bin/env python3
"""Load one repository environment file without shell evaluation."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess


ENVIRONMENT_NAMES = frozenset({"local", "staging", "production"})


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def environment_file(root: Path, name: str) -> Path:
    if name not in ENVIRONMENT_NAMES:
        raise ValueError(
            "APP_ENV must be one of: local, staging, production"
        )
    selected = root / f".env.{name}"
    if selected.is_file() and not selected.is_symlink():
        return selected
    legacy = root / ".env"
    if name == "local" and legacy.is_file() and not legacy.is_symlink():
        return legacy
    raise FileNotFoundError(
        f"missing environment file: {selected}; copy .env.example and fill secrets"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--github-token-fallback", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required")

    root = Path(__file__).resolve().parent.parent
    environment_name = os.environ.get("APP_ENV", "local").strip().lower()
    selected_file = environment_file(root, environment_name)
    environment = {**load_env(selected_file), **os.environ}
    environment["APP_ENV"] = environment_name
    github_api_token = environment.get("GITHUB_ROUTING_TOKEN", "")
    if github_api_token:
        environment["GITHUB_COPILOT_TOKEN"] = github_api_token
    if args.github_token_fallback and not environment.get("GITHUB_ISSUE_TOKEN"):
        if github_api_token:
            environment["GITHUB_ISSUE_TOKEN"] = github_api_token
        else:
            result = subprocess.run(
                ["gh", "auth", "token"],
                check=True,
                capture_output=True,
                text=True,
            )
            environment["GITHUB_ISSUE_TOKEN"] = result.stdout.strip()
    os.execvpe(command[0], command, environment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
