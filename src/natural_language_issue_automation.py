"""Run natural-language Issue generation through repository resolution and policy approval."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from src import ai_issue_generator, kibana_sanitizer
from src.issue_draft import _atomic_write_json, _atomic_write_text
from src.issue_entry import _description, _gateway_config, compose_evidence
from src.repository_issue_automation import (
    GitHubCLIIssueClient,
    automate_repository_issue,
    load_auto_publish_policy,
    render_automated_issue_body,
)
from src.repository_resolver import (
    GitHubCLICodeSearchAdapter,
    GitHubCLIRepositoryTreeProbeAdapter,
    RepositorySearchAdapter,
    load_search_scope,
)


POLICY_DIGEST_ENV = "REPOSITORY_AUTO_POLICY_SHA256"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate, resolve, deduplicate, and policy-approve one GitHub Issue."
    )
    description = parser.add_mutually_exclusive_group(required=True)
    description.add_argument("--description")
    description.add_argument("--description-file", type=Path)
    parser.add_argument("--log", type=Path)
    parser.add_argument(
        "--scope",
        type=Path,
        default=Path(".issue-entry-state/repository-search-scope.json"),
    )
    parser.add_argument(
        "--auto-policy",
        type=Path,
        default=Path(".issue-entry-state/repository-auto-publish-policy.json"),
    )
    parser.add_argument("--confirmed-policy-sha256", default="")
    parser.add_argument(
        "--adapter",
        choices=("github-code-search", "github-tree-probe"),
        default="github-code-search",
    )
    parser.add_argument("--auto-publish", action="store_true")
    parser.add_argument("--prompt-api-key", action="store_true")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--output-dir", type=Path, default=Path(".issue-entry-output"))
    parser.add_argument("--name")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    name = args.name or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = args.output_dir / name
    if run_dir.exists():
        print(f"error: output already exists: {run_dir}", file=sys.stderr)
        return 2
    try:
        raw_key = os.environ.get(kibana_sanitizer.HMAC_KEY_ENV, "").encode("utf-8")
        evidence = compose_evidence(_description(args), args.log, raw_key)
        evidence_path = run_dir / "evidence.json"
        _atomic_write_json(evidence_path, evidence)

        gateway = _gateway_config(args.prompt_api_key)
        generation = ai_issue_generator.generate_issue(
            evidence,
            ai_issue_generator.OpenAICompatibleChatProvider(gateway, gateway.model),
            ai_issue_generator.OpenAICompatibleChatProvider(
                gateway, gateway.review_model
            ),
        )
        generation_path = run_dir / "generation.json"
        draft_path = run_dir / "issue-draft.md"
        ai_issue_generator.write_result(generation, generation_path, draft_path)

        scope = load_search_scope(args.scope)
        confirmed_digest = (
            args.confirmed_policy_sha256.strip()
            or os.environ.get(POLICY_DIGEST_ENV, "").strip()
        )
        policy = load_auto_publish_policy(
            args.auto_policy,
            confirmed_digest,
            scope,
            args.scope,
        )
        adapter: RepositorySearchAdapter
        if args.adapter == "github-tree-probe":
            adapter = GitHubCLIRepositoryTreeProbeAdapter(
                scope.enabled_repositories, args.timeout
            )
        else:
            adapter = GitHubCLICodeSearchAdapter(args.timeout)
        issue_client = GitHubCLIIssueClient(args.timeout)
        automation = automate_repository_issue(
            generation,
            evidence,
            scope,
            adapter,
            args.adapter,
            policy,
            issue_client,
            args.auto_publish,
        )
        automation_path = run_dir / "automation.json"
        _atomic_write_json(automation_path, automation)
        repository = automation["publication"].get("repository")
        if repository:
            publish_body, _ = render_automated_issue_body(generation, repository, policy)
            _atomic_write_text(run_dir / "publish-body.md", publish_body)
    except (FileExistsError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(automation_path)
    issue_url = automation["publication"].get("issue_url")
    if issue_url:
        print(issue_url)
    if automation["publication"]["status"] == "blocked":
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
