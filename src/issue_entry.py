"""Turn natural-language context and one log into a reviewed local Issue, then optionally publish it."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src import ai_issue_generator, kibana_sanitizer
from src.issue_draft import _atomic_write_json


MAX_DESCRIPTION_CHARS = 8_000
MAX_LOG_BYTES = 2_000_000
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _load_log(path: Path) -> Dict[str, Any]:
    if path.stat().st_size > MAX_LOG_BYTES:
        raise ValueError(f"log input must not exceed {MAX_LOG_BYTES} bytes")
    raw = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("_source"), dict):
        return payload
    document_id = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return {
        "_id": document_id,
        "_source": {
            "message": raw,
            "stream": "",
            "@timestamp": "",
        },
    }


def compose_evidence(description: str, log_path: Path, hmac_key: bytes) -> Dict[str, Any]:
    description = description.strip()
    if not description:
        raise ValueError("natural-language description is required")
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise ValueError(f"description must not exceed {MAX_DESCRIPTION_CHARS} characters")

    safe_description, description_findings = kibana_sanitizer.redact_free_text(
        description,
        "facts.reported_description",
    )
    if any(finding.action == "blocked" for finding in description_findings):
        raise ValueError("description contains unclassified high-entropy data")

    sanitized_log = kibana_sanitizer.sanitize_hit(_load_log(log_path), hmac_key)
    sanitization = sanitized_log["sanitization"]
    if not sanitization.get("ai_allowed", False):
        raise ValueError("log sanitization blocked AI processing")

    sensitive_categories = {
        "password",
        "token",
        "cookie",
        "authorization",
        "private_key",
        "connection_string",
        "api_key_candidate",
        "credential",
    }
    all_categories = {
        finding.category for finding in description_findings
    } | set(sanitization.get("removed_categories", []))
    security_review_required = bool(all_categories & sensitive_categories) or bool(
        sanitization.get("security_review_required", False)
    )
    event_ref = str(sanitized_log.get("source", {}).get("event_ref", ""))
    fallback_ref = hashlib.sha256(
        f"{safe_description}\n{log_path.name}".encode("utf-8")
    ).hexdigest()[:16]

    return {
        "schema_version": ai_issue_generator.EVIDENCE_SCHEMA_VERSION,
        "source": {
            "type": "natural_language_and_log",
            "reference": event_ref or f"local_ref:{fallback_ref}",
            "url": "",
        },
        "safety": {
            "status": "sanitized",
            "ai_allowed": True,
            "security_review_required": security_review_required,
            "redacted_categories": sorted(all_categories),
        },
        "facts": {
            "reported_description": safe_description,
        },
        "target": sanitized_log.get("target", {}),
        "event": sanitized_log.get("event", {}),
        "runtime": sanitized_log.get("runtime", {}),
    }


def _infer_repository() -> str:
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return ""
    remote = result.stdout.strip()
    match = re.search(r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?$", remote)
    return f"{match.group(1)}/{match.group(2)}" if match else ""


def publish_issue(
    result: Dict[str, Any],
    markdown_path: Path,
    repository: str,
    evidence: Dict[str, Any],
) -> str:
    if not REPOSITORY_PATTERN.fullmatch(repository):
        raise ValueError("repository must use owner/name format")
    if result.get("state") == "blocked" or not result.get("validation", {}).get("valid", False):
        raise ValueError("blocked or invalid AI output cannot be published")
    if evidence.get("safety", {}).get("security_review_required", False):
        raise ValueError("redacted credential evidence requires a separate security review before publication")

    try:
        subprocess.run(
            ["gh", "auth", "status"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        created = subprocess.run(
            [
                "gh",
                "issue",
                "create",
                "--repo",
                repository,
                "--title",
                str(result["draft"]["title"]),
                "--body-file",
                str(markdown_path),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        raise ValueError("GitHub CLI is required for --publish") from exc
    except subprocess.CalledProcessError as exc:
        raise ValueError("GitHub authentication or Issue creation failed") from exc
    issue_url = created.stdout.strip()
    if not issue_url.startswith("https://github.com/"):
        raise ValueError("GitHub CLI did not return an Issue URL")
    return issue_url


def _description(args: argparse.Namespace) -> str:
    if args.description_file:
        return args.description_file.read_text(encoding="utf-8")
    return args.description or ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a reviewed Issue from natural language and one log."
    )
    description = parser.add_mutually_exclusive_group(required=True)
    description.add_argument("--description", help="Short natural-language problem description.")
    description.add_argument("--description-file", type=Path, help="UTF-8 description file.")
    parser.add_argument("--log", type=Path, required=True, help="Kibana hit JSON or plain-text log.")
    parser.add_argument("--output-dir", type=Path, default=Path(".issue-entry-output"))
    parser.add_argument("--name", help="Output folder name; defaults to a UTC timestamp.")
    parser.add_argument("--publish", action="store_true", help="Create the reviewed Issue with gh.")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Confirm human review and publication; required with --publish.",
    )
    parser.add_argument("--repository", help="GitHub owner/name; defaults to the origin remote.")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.publish and not args.confirm:
        print("error: --publish requires --confirm", file=sys.stderr)
        return 2

    name = args.name or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = args.output_dir / name
    evidence_path = run_dir / "evidence.json"
    result_path = run_dir / "result.json"
    markdown_path = run_dir / "issue.md"
    if run_dir.exists():
        print(f"error: output already exists: {run_dir}", file=sys.stderr)
        return 2

    try:
        raw_key = os.environ.get(kibana_sanitizer.HMAC_KEY_ENV, "").encode("utf-8")
        if len(raw_key) < kibana_sanitizer.MIN_HMAC_KEY_BYTES:
            raise ValueError(
                f"{kibana_sanitizer.HMAC_KEY_ENV} must contain at least "
                f"{kibana_sanitizer.MIN_HMAC_KEY_BYTES} bytes"
            )
        evidence = compose_evidence(_description(args), args.log, raw_key)
        _atomic_write_json(evidence_path, evidence)

        config = ai_issue_generator.GatewayConfig.from_env()
        result = ai_issue_generator.generate_issue(
            evidence,
            ai_issue_generator.OpenAICompatibleChatProvider(config, config.model),
            ai_issue_generator.OpenAICompatibleChatProvider(config, config.review_model),
        )
        ai_issue_generator.write_result(result, result_path, markdown_path)

        issue_url = ""
        if args.publish:
            repository = args.repository or _infer_repository()
            if not repository:
                raise ValueError("--repository is required when the origin is not a GitHub repository")
            issue_url = publish_issue(result, markdown_path, repository, evidence)
    except (FileExistsError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(markdown_path)
    if issue_url:
        print(issue_url)
    return 4 if result["state"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
