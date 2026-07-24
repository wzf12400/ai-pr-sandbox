"""Dispatch one approved GitHub Issue into the guarded code-modification preflight."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Sequence

from src.copilot_code_modifier import (
    ApprovedIssue,
    CodeModifier,
    CopilotCLICodeModifier,
    GitHubCLIDraftPRPublisher,
    GitHubCLIIssueSnapshotClient,
    IssueClient,
    IssueCodePolicy,
    _atomic_write,
    _run_process,
    evaluate_issue_approval,
    execute_issue_code_workflow,
    issue_work_branch_name,
    load_issue_code_policy,
    validate_repository,
)


REPORT_SCHEMA_VERSION = "issue-code-dispatch/v1"
MAX_CANDIDATES = 20
COMMIT_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")


class CandidateClient(Protocol):
    def list_open_issues(
        self, repository: str, required_labels: Sequence[str], limit: int
    ) -> Sequence[str]:
        ...


class DispatchStateInspector(Protocol):
    def inspect(
        self,
        repo: Path,
        repository: str,
        work_branch: str,
        claim_branch: str,
    ) -> Mapping[str, Any]:
        ...


class DispatchClaimer(Protocol):
    def claim(
        self, repo: Path, base_commit: str, claim_branch: str
    ) -> Mapping[str, Any]:
        ...


WorkflowRunner = Callable[
    [str, Path, Path, IssueClient, CodeModifier, bool], Mapping[str, Any]
]


def _validate_target_issue_url(issue_url: str, repository: str) -> None:
    prefix = f"https://github.com/{repository}/issues/"
    number = issue_url.removeprefix(prefix)
    if (
        not issue_url.startswith(prefix)
        or not number.isdigit()
        or int(number) < 1
        or str(int(number)) != number
    ):
        raise ValueError("target Issue URL is not canonical for the policy repository")


class GitHubCLIApprovedIssueClient:
    """List only bounded open Issues; the dispatcher revalidates every snapshot."""

    def __init__(self, timeout_seconds: float = 30.0):
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("GitHub timeout must be between 1 and 120 seconds")
        self.timeout_seconds = timeout_seconds

    def list_open_issues(
        self, repository: str, required_labels: Sequence[str], limit: int
    ) -> Sequence[str]:
        args = [
            "gh",
            "issue",
            "list",
            "--repo",
            repository,
            "--state",
            "open",
            "--limit",
            str(limit),
            "--search",
            "sort:created-asc",
            "--json",
            "number,url",
        ]
        for label in required_labels:
            args.extend(["--label", label])
        result = _run_process(args, Path.cwd(), self.timeout_seconds)
        if result.returncode != 0:
            raise ValueError("approved Issue listing failed closed")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("approved Issue listing returned invalid JSON") from exc
        if not isinstance(payload, list):
            raise ValueError("approved Issue listing returned an invalid array")
        prefix = f"https://github.com/{repository}/issues/"
        candidates: List[tuple[int, str]] = []
        for item in payload:
            if not isinstance(item, dict):
                raise ValueError("approved Issue listing returned an invalid item")
            number = item.get("number")
            url = item.get("url")
            if (
                not isinstance(number, int)
                or isinstance(number, bool)
                or number < 1
                or not isinstance(url, str)
                or url != f"{prefix}{number}"
            ):
                raise ValueError("approved Issue listing returned a noncanonical Issue")
            candidates.append((number, url))
        candidates.sort()
        return [url for _, url in candidates]


class GitHubCLIDispatchStateInspector:
    """Read local and GitHub state without creating a claim or branch."""

    def __init__(self, timeout_seconds: float = 30.0):
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("GitHub timeout must be between 1 and 120 seconds")
        self.timeout_seconds = timeout_seconds

    def inspect(
        self,
        repo: Path,
        repository: str,
        work_branch: str,
        claim_branch: str,
    ) -> Mapping[str, Any]:
        local = _run_process(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{work_branch}"],
            repo,
            30,
        )
        if local.returncode not in {0, 1}:
            raise ValueError("local dispatch state check failed closed")

        remote_work_sha = _remote_branch_sha(
            repo, work_branch, self.timeout_seconds
        )
        remote_claim_sha = _remote_branch_sha(
            repo, claim_branch, self.timeout_seconds
        )

        prs = _run_process(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repository,
                "--state",
                "all",
                "--head",
                work_branch,
                "--limit",
                "2",
                "--json",
                "number,url,state,isDraft",
            ],
            repo,
            self.timeout_seconds,
        )
        if prs.returncode != 0:
            raise ValueError("Draft PR idempotency check failed closed")
        try:
            pr_payload = json.loads(prs.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("Draft PR idempotency check returned invalid JSON") from exc
        if not isinstance(pr_payload, list) or len(pr_payload) > 1:
            raise ValueError("Draft PR idempotency state is ambiguous")
        existing_pr_url = None
        if pr_payload:
            item = pr_payload[0]
            if not isinstance(item, dict):
                raise ValueError("Draft PR idempotency state is invalid")
            candidate_url = item.get("url")
            expected_prefix = f"https://github.com/{repository}/pull/"
            pr_number = (
                candidate_url.removeprefix(expected_prefix)
                if isinstance(candidate_url, str)
                else ""
            )
            if (
                not isinstance(candidate_url, str)
                or not candidate_url.startswith(expected_prefix)
                or not pr_number.isdigit()
                or int(pr_number) < 1
            ):
                raise ValueError("Draft PR idempotency state has an invalid URL")
            existing_pr_url = candidate_url
        claimed = (
            local.returncode == 0
            or remote_work_sha is not None
            or remote_claim_sha is not None
            or bool(existing_pr_url)
        )
        return {
            "claimed": claimed,
            "local_work_branch_exists": local.returncode == 0,
            "remote_work_branch_exists": remote_work_sha is not None,
            "remote_claim_branch_exists": remote_claim_sha is not None,
            "remote_claim_commit": remote_claim_sha,
            "existing_pr_url": existing_pr_url,
        }


class SnapshotBoundIssueClient:
    """Require every downstream fetch to return the initially approved snapshot."""

    def __init__(
        self, delegate: IssueClient, issue_url: str, expected_sha256: str
    ):
        self.delegate = delegate
        self.issue_url = issue_url
        self.expected_sha256 = expected_sha256

    def fetch(self, issue_url: str) -> ApprovedIssue:
        if issue_url != self.issue_url:
            raise ValueError("dispatcher requested an unexpected Issue URL")
        issue = self.delegate.fetch(issue_url)
        if issue.sha256 != self.expected_sha256:
            raise ValueError("approved Issue snapshot changed before dispatch")
        return issue


def issue_claim_branch_name(
    policy: IssueCodePolicy, issue: ApprovedIssue
) -> str:
    return (
        f"{policy.branch_prefix}/claims/"
        f"issue-{issue.number}-{issue.sha256[:8]}"
    )


def _remote_branch_sha(
    repo: Path, branch: str, timeout_seconds: float
) -> Optional[str]:
    result = _run_process(
        [
            "git",
            "ls-remote",
            "--exit-code",
            "--heads",
            "origin",
            f"refs/heads/{branch}",
        ],
        repo,
        timeout_seconds,
    )
    if result.returncode == 2:
        return None
    if result.returncode != 0:
        raise ValueError("remote dispatch state check failed closed")
    lines = [line for line in result.stdout.splitlines() if line]
    expected_ref = f"refs/heads/{branch}"
    if len(lines) != 1:
        raise ValueError("remote dispatch state is ambiguous")
    parts = lines[0].split("\t")
    if (
        len(parts) != 2
        or not COMMIT_SHA_PATTERN.fullmatch(parts[0])
        or parts[1] != expected_ref
    ):
        raise ValueError("remote dispatch state is invalid")
    return parts[0]


class GitRemoteBranchClaimer:
    """Atomically compete for one Issue snapshot through a unique commit."""

    def __init__(self, timeout_seconds: float = 120.0):
        if not 1 <= timeout_seconds <= 300:
            raise ValueError("claim timeout must be between 1 and 300 seconds")
        self.timeout_seconds = timeout_seconds

    def _claim_commit(self, repo: Path, base_commit: str) -> str:
        if not COMMIT_SHA_PATTERN.fullmatch(base_commit):
            raise ValueError("claim base commit is invalid")
        tree = _run_process(
            ["git", "rev-parse", f"{base_commit}^{{tree}}"], repo, 30
        )
        tree_sha = tree.stdout.strip()
        if tree.returncode != 0 or not COMMIT_SHA_PATTERN.fullmatch(tree_sha):
            raise ValueError("claim tree could not be resolved")
        claim_id = secrets.token_hex(16)
        inherited = {"LANG", "LC_ALL", "LC_CTYPE", "PATH", "TMPDIR"}
        environment = {
            key: value for key, value in os.environ.items() if key in inherited
        }
        environment.update(
            {
                "GIT_AUTHOR_NAME": "AI Issue Dispatcher",
                "GIT_AUTHOR_EMAIL": "dispatcher@users.noreply.github.com",
                "GIT_COMMITTER_NAME": "AI Issue Dispatcher",
                "GIT_COMMITTER_EMAIL": "dispatcher@users.noreply.github.com",
            }
        )
        commit = _run_process(
            ["git", "commit-tree", tree_sha, "-p", base_commit],
            repo,
            30,
            input_text=f"Claim approved Issue snapshot {claim_id}\n",
            env=environment,
        )
        claim_commit = commit.stdout.strip()
        if commit.returncode != 0 or not COMMIT_SHA_PATTERN.fullmatch(claim_commit):
            raise ValueError("claim commit could not be created")
        return claim_commit

    def claim(
        self, repo: Path, base_commit: str, claim_branch: str
    ) -> Mapping[str, Any]:
        claim_commit = self._claim_commit(repo, base_commit)
        push = _run_process(
            [
                "git",
                "push",
                "origin",
                f"{claim_commit}:refs/heads/{claim_branch}",
            ],
            repo,
            self.timeout_seconds,
        )
        remote_sha = _remote_branch_sha(
            repo, claim_branch, self.timeout_seconds
        )
        won = push.returncode == 0 and remote_sha == claim_commit
        return {
            "claimed": won,
            "claim_branch": claim_branch,
            "claim_commit": claim_commit if won else None,
            "remote_commit": remote_sha,
            "conflict": not won and remote_sha is not None,
            "retained_on_failure": won,
        }


def _run_modifier(
    issue_url: str,
    repo: Path,
    policy_path: Path,
    issue_client: IssueClient,
    modifier: CodeModifier,
    execute: bool,
    *,
    publish_pr: bool = False,
    model: str = "",
) -> Mapping[str, Any]:
    return execute_issue_code_workflow(
        issue_url,
        repo,
        policy_path,
        issue_client,
        modifier,
        execute=execute,
        publish_pr=publish_pr,
        model=model,
        publisher=GitHubCLIDraftPRPublisher() if publish_pr else None,
    )


def _safe_modifier_failure_reason(exc: BaseException, execute: bool) -> str:
    message = str(exc)
    if message == "Issue text contains unclassified high-entropy data":
        return "modifier_localization_safety_blocked"
    if message in {
        "Copilot CLI is not installed",
        "Copilot CLI version check failed",
    }:
        return "copilot_cli_preflight_failed"
    return "modifier_execution_failed" if execute else "modifier_preflight_failed"


def _candidate_record(
    issue: ApprovedIssue,
    approval: Mapping[str, Any],
    work_branch: str,
    claim_branch: str,
    idempotency: Mapping[str, Any],
) -> Dict[str, Any]:
    return {
        "number": issue.number,
        "url": issue.url,
        "snapshot_sha256": issue.sha256,
        "approved": bool(approval.get("approved")),
        "approval_rules": dict(approval.get("rules", {})),
        "sensitive_categories": list(approval.get("sensitive_categories", [])),
        "work_branch": work_branch,
        "claim_branch": claim_branch,
        "idempotency": dict(idempotency),
        "raw_issue_persisted": False,
    }


def dispatch_once(
    repo: Path,
    policy_path: Path,
    candidate_client: CandidateClient,
    issue_client: IssueClient,
    state_inspector: DispatchStateInspector,
    modifier: CodeModifier,
    *,
    max_candidates: int = 10,
    execute: bool = False,
    publish_pr: bool = False,
    model: str = "",
    target_issue_url: str = "",
    retained_claim_commit: str = "",
    expected_issue_snapshot_sha256: str = "",
    claimer: Optional[DispatchClaimer] = None,
    workflow_runner: Optional[WorkflowRunner] = None,
) -> Dict[str, Any]:
    if not 1 <= max_candidates <= MAX_CANDIDATES:
        raise ValueError(f"max_candidates must be between 1 and {MAX_CANDIDATES}")
    if publish_pr and not execute:
        raise ValueError("Draft PR publication requires execute mode")
    if retained_claim_commit and (
        not execute
        or not target_issue_url
        or not COMMIT_SHA_PATTERN.fullmatch(retained_claim_commit)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_issue_snapshot_sha256)
    ):
        raise ValueError("retained claim resume parameters are invalid")
    if execute and claimer is None and not retained_claim_commit:
        raise ValueError("execute mode requires an atomic dispatcher claimer")
    policy = load_issue_code_policy(policy_path)
    repository = validate_repository(repo, policy_path, policy)
    if target_issue_url:
        _validate_target_issue_url(target_issue_url, policy.repository)
        urls = [target_issue_url]
        candidate_source = "explicit_target"
    else:
        urls = list(
            candidate_client.list_open_issues(
                policy.repository, policy.required_labels, max_candidates
            )
        )
        if len(urls) > max_candidates or len(urls) != len(set(urls)):
            raise ValueError("approved Issue listing exceeded its deterministic bounds")
        candidate_source = "approved_issue_poll"
    report: Dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": "no_candidate",
        "mode": (
            "once_publish_pr"
            if publish_pr
            else ("once_execute" if execute else "once_dry_run")
        ),
        "repository": {
            "name": policy.repository,
            "path": repository["path"],
            "base_branch": repository["base_branch"],
            "base_commit": repository["base_commit"],
        },
        "policy": {
            "schema_version": "issue-code-policy/v1",
            "policy_id": policy.policy_id,
            "sha256": policy.sha256,
            "required_labels": list(policy.required_labels),
        },
        "poll": {
            "candidate_limit": max_candidates,
            "candidate_count": len(urls),
            "single_dispatch_limit": 1,
            "candidate_source": candidate_source,
        },
        "candidates": [],
        "dispatch": {
            "requested": False,
            "issue_url": None,
            "modifier_mode": (
                "publish_pr"
                if publish_pr
                else ("execute" if execute else "dry_run")
            ),
            "modifier_report": None,
            "claim": {
                "requested": False,
                "claimed": False,
                "retained_on_failure": False,
            },
        },
        "capabilities": {
            "copilot_execution": execute,
            "github_claim_write": execute,
            "draft_pr_publication": publish_pr,
            "daemon_polling": False,
        },
    }
    for issue_url in urls:
        issue = issue_client.fetch(issue_url)
        approval = evaluate_issue_approval(issue, policy)
        approval["rules"]["requested_url_matches_snapshot"] = issue.url == issue_url
        if expected_issue_snapshot_sha256:
            approval["rules"]["retained_snapshot_matches"] = (
                issue.sha256 == expected_issue_snapshot_sha256
            )
        approval["approved"] = all(approval["rules"].values())
        work_branch = issue_work_branch_name(policy, issue)
        claim_branch = issue_claim_branch_name(policy, issue)
        idempotency = state_inspector.inspect(
            repo, policy.repository, work_branch, claim_branch
        )
        retained_claim_matches = bool(
            retained_claim_commit
            and idempotency.get("remote_claim_branch_exists")
            and idempotency.get("remote_claim_commit") == retained_claim_commit
            and not idempotency.get("local_work_branch_exists")
            and not idempotency.get("remote_work_branch_exists")
            and not idempotency.get("existing_pr_url")
        )
        idempotency = {
            **dict(idempotency),
            "retained_claim_resume": bool(retained_claim_commit),
            "retained_claim_matches": retained_claim_matches,
        }
        candidate = _candidate_record(
            issue, approval, work_branch, claim_branch, idempotency
        )
        report["candidates"].append(candidate)
        if not approval["approved"] or (
            idempotency.get("claimed") and not retained_claim_matches
        ):
            continue

        report["dispatch"].update({"requested": True, "issue_url": issue.url})
        bound_issue_client = SnapshotBoundIssueClient(
            issue_client, issue.url, issue.sha256
        )
        if execute:
            try:
                bound_issue_client.fetch(issue.url)
                if retained_claim_commit:
                    claim = {
                        "claimed": True,
                        "claim_branch": claim_branch,
                        "claim_commit": retained_claim_commit,
                        "remote_commit": retained_claim_commit,
                        "conflict": False,
                        "retained_on_failure": True,
                        "resumed": True,
                    }
                else:
                    claim = claimer.claim(
                        repo, repository["base_commit"], claim_branch
                    )
            except (OSError, ValueError):
                report["status"] = "blocked"
                report["dispatch"]["failure_reason"] = "claim_failed_closed"
                return report
            report["dispatch"]["claim"] = {
                "requested": True,
                **dict(claim),
            }
            if not claim.get("claimed"):
                report["status"] = "blocked"
                report["dispatch"]["failure_reason"] = "claim_conflict_or_failure"
                return report
            try:
                bound_issue_client.fetch(issue.url)
            except (OSError, ValueError):
                report["status"] = "blocked"
                report["dispatch"]["failure_reason"] = "issue_changed_after_claim"
                return report
        try:
            if workflow_runner is None:
                modifier_report = _run_modifier(
                    issue.url,
                    repo,
                    policy_path,
                    bound_issue_client,
                    modifier,
                    execute,
                    publish_pr=publish_pr,
                    model=model,
                )
            else:
                modifier_report = workflow_runner(
                    issue.url,
                    repo,
                    policy_path,
                    bound_issue_client,
                    modifier,
                    execute,
                )
        except (OSError, ValueError) as exc:
            report["status"] = "blocked"
            report["dispatch"]["failure_reason"] = _safe_modifier_failure_reason(
                exc,
                execute,
            )
            return report
        report["dispatch"]["modifier_report"] = dict(modifier_report)
        returned_source = modifier_report.get("source", {})
        same_snapshot = (
            isinstance(returned_source, Mapping)
            and returned_source.get("snapshot_sha256") == issue.sha256
        )
        report["dispatch"]["issue_unchanged_before_dispatch"] = same_snapshot
        if not same_snapshot:
            report["status"] = "blocked"
            report["dispatch"]["failure_reason"] = "issue_changed_before_dispatch"
        elif modifier_report.get("status") != (
            "draft_pr_created" if publish_pr else ("tested" if execute else "ready")
        ):
            report["status"] = "blocked"
            report["dispatch"]["failure_reason"] = (
                "modifier_execution_blocked"
                if execute
                else "modifier_preflight_blocked"
            )
        else:
            report["status"] = (
                "draft_pr_created"
                if publish_pr
                else ("tested" if execute else "ready")
            )
        return report
    if urls:
        report["status"] = "blocked"
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Poll approved GitHub Issues once and dispatch one into the guarded "
            "code-modification workflow."
        )
    )
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument(
        "--policy", type=Path, default=Path(".github/issue-code-policy.json")
    )
    parser.add_argument("--once", action="store_true")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--publish-pr", action="store_true")
    parser.add_argument("--model", default="")
    parser.add_argument("--max-candidates", type=int, default=10)
    parser.add_argument("--github-timeout", type=float, default=30.0)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.once:
        print(
            "error: the dispatcher currently requires --once",
            file=sys.stderr,
        )
        return 2
    policy_path = args.policy if args.policy.is_absolute() else args.repo / args.policy
    execute = args.execute or args.publish_pr
    try:
        report = dispatch_once(
            args.repo,
            policy_path,
            GitHubCLIApprovedIssueClient(args.github_timeout),
            GitHubCLIIssueSnapshotClient(args.github_timeout),
            GitHubCLIDispatchStateInspector(args.github_timeout),
            CopilotCLICodeModifier(),
            max_candidates=args.max_candidates,
            execute=execute,
            publish_pr=args.publish_pr,
            model=args.model,
            claimer=(
                GitRemoteBranchClaimer(max(args.github_timeout, 120.0))
                if execute
                else None
            ),
        )
        _atomic_write(args.output, report)
    except (FileExistsError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(args.output)
    if report["status"] == "blocked":
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
