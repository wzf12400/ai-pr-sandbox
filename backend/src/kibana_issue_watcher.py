"""Run the bounded Kibana connector repeatedly under one approved publication policy."""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from typing import List, Optional

from src import kibana_issue_connector, kibana_sanitizer


MIN_INTERVAL_SECONDS = 60
MAX_INTERVAL_SECONDS = 3600
REQUIRED_ENVIRONMENT = ("OPENSEARCH_PASSWORD", "AI_API_KEY")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Poll the bounded OpenSearch connector in the foreground."
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=300,
        help=f"Polling interval, between {MIN_INTERVAL_SECONDS} and {MAX_INTERVAL_SECONDS} seconds.",
    )
    parser.add_argument(
        "--max-runs",
        type=int,
        default=0,
        help="Stop after this many runs; zero means run until interrupted.",
    )
    parser.add_argument(
        "connector_args",
        nargs=argparse.REMAINDER,
        help="Arguments for kibana-to-issues after --.",
    )
    return parser


def _validate_connector_args(arguments: List[str]) -> List[str]:
    resolved = arguments[1:] if arguments[:1] == ["--"] else arguments
    required = {"--generate", "--auto-publish-policy", "--confirm-policy-sha256"}
    missing = sorted(option for option in required if option not in resolved)
    if missing:
        raise ValueError(
            "watch mode requires connector arguments: " + ", ".join(missing)
        )
    forbidden = {"--prompt-password", "--prompt-api-key", "--publish", "--confirm"}
    present = sorted(option for option in forbidden if option in resolved)
    if present:
        raise ValueError(
            "watch mode does not accept interactive or manual publication flags: "
            + ", ".join(present)
        )
    if "--name" in resolved:
        raise ValueError("watch mode assigns a unique --name to each run")
    return resolved


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not MIN_INTERVAL_SECONDS <= args.interval_seconds <= MAX_INTERVAL_SECONDS:
        print(
            f"error: --interval-seconds must be between {MIN_INTERVAL_SECONDS} and {MAX_INTERVAL_SECONDS}",
            file=sys.stderr,
        )
        return 2
    if args.max_runs < 0:
        print("error: --max-runs cannot be negative", file=sys.stderr)
        return 2
    try:
        connector_args = _validate_connector_args(args.connector_args)
        missing_environment = [name for name in REQUIRED_ENVIRONMENT if not os.environ.get(name)]
        if missing_environment:
            raise ValueError(
                "watch mode requires in-memory environment values: "
                + ", ".join(missing_environment)
            )
        hmac_key = os.environ.get(kibana_sanitizer.HMAC_KEY_ENV, "").encode("utf-8")
        if len(hmac_key) < kibana_sanitizer.MIN_HMAC_KEY_BYTES:
            raise ValueError(
                f"{kibana_sanitizer.HMAC_KEY_ENV} must contain at least "
                f"{kibana_sanitizer.MIN_HMAC_KEY_BYTES} bytes and remain stable across restarts"
            )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    run_count = 0
    last_code = 0
    while args.max_runs == 0 or run_count < args.max_runs:
        run_count += 1
        run_name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        code = kibana_issue_connector.main([*connector_args, "--name", run_name])
        last_code = code
        if code != 0:
            print(f"watch run {run_count} stopped safely with exit code {code}", file=sys.stderr)
        if args.max_runs and run_count >= args.max_runs:
            break
        try:
            time.sleep(args.interval_seconds)
        except KeyboardInterrupt:
            break
    return last_code


if __name__ == "__main__":
    raise SystemExit(main())
