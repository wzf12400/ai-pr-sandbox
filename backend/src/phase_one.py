"""Unified CLI for the guarded phase-one intake and localization flow."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional

from src import ai_issue_generator, kibana_sanitizer, repo_locator, triage_issue


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one phase-one workflow step.")
    commands = parser.add_subparsers(dest="command", required=True)

    kibana = commands.add_parser("kibana", help="Run raw Kibana event through the full triage flow.")
    kibana.add_argument("input")
    kibana.add_argument("--sanitized-output", required=True)
    kibana.add_argument("--draft-output", required=True)

    triage = commands.add_parser("kibana-to-issue", help="Create a triage draft from a sanitized event.")
    triage.add_argument("input")
    triage.add_argument("--output", required=True)

    locate = commands.add_parser("locate-github-issue", help="Locate code for a GitHub Issue.")
    locate.add_argument("issue_json")
    locate.add_argument("--repo", required=True)
    locate.add_argument("--output", required=True)
    locate.add_argument("--top-k", type=int, default=10)

    ai_issue = commands.add_parser(
        "ai-issue",
        help="Generate and review a local Issue through the configured AI gateway.",
    )
    ai_issue.add_argument("input")
    ai_issue.add_argument("--output-json", required=True)
    ai_issue.add_argument("--output-md", required=True)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "kibana":
        raw_key = os.environ.get(kibana_sanitizer.HMAC_KEY_ENV, "").encode("utf-8")
        if len(raw_key) < kibana_sanitizer.MIN_HMAC_KEY_BYTES:
            print(
                f"error: {kibana_sanitizer.HMAC_KEY_ENV} must contain at least "
                f"{kibana_sanitizer.MIN_HMAC_KEY_BYTES} bytes",
                file=sys.stderr,
            )
            return 2
        sanitized_path = Path(args.sanitized_output)
        draft_path = Path(args.draft_output)
        if sanitized_path.exists() or draft_path.exists():
            print("error: output already exists", file=sys.stderr)
            return 2
        try:
            result = kibana_sanitizer.sanitize_hit(
                kibana_sanitizer.load_hit(Path(args.input)),
                raw_key,
            )
            decision = triage_issue.evaluate_event(result)
            draft = (
                triage_issue.render_triage_markdown(result)
                if decision.state not in {"blocked", "ignored_non_error"}
                else ""
            )
            kibana_sanitizer.write_sanitized_event(sanitized_path, result)
            if draft:
                triage_issue.write_triage_draft(draft_path, draft)
        except (FileExistsError, OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if decision.state == "blocked":
            print(f"blocked: {decision.reason}", file=sys.stderr)
            return 4
        if decision.state == "ignored_non_error":
            print(f"skipped: {decision.reason}")
            return 3
        print(draft_path)
        return 0
    if args.command == "kibana-to-issue":
        return triage_issue.main([args.input, "--output", args.output])
    if args.command == "ai-issue":
        return ai_issue_generator.main(
            [
                args.input,
                "--output-json",
                args.output_json,
                "--output-md",
                args.output_md,
            ]
        )
    return repo_locator.main(
        [args.issue_json, "--repo", args.repo, "--output", args.output, "--top-k", str(args.top_k)]
    )


if __name__ == "__main__":
    raise SystemExit(main())
