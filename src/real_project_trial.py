"""Evaluate repository localization against a real GitHub fixing PR."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.repo_locator import load_github_issue, locate_issue


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON at {path}:{exc.lineno}") from exc


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evaluate_trial(
    issue_path: Path,
    pr_path: Path,
    pr_files_path: Path,
    repo: Path,
    top_k: int = 10,
) -> Dict[str, Any]:
    issue = load_github_issue(issue_path)
    pr = _load_json(pr_path)
    if not isinstance(pr, dict):
        raise ValueError("PR input must be one GitHub Pull Request API object")
    base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
    base_repo = base.get("repo") if isinstance(base.get("repo"), dict) else {}
    expected_repo = str(base_repo.get("full_name", ""))
    expected_commit = str(base.get("sha", ""))
    fix_pr_url = str(pr.get("html_url", ""))
    if not expected_repo or not expected_commit or not fix_pr_url:
        raise ValueError("PR input is missing base repository, base SHA, or URL")
    repository_url = str(issue.get("repository_url", ""))
    if not repository_url.endswith(f"/repos/{expected_repo}"):
        raise ValueError(f"Issue repository does not match {expected_repo}")

    actual_commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    if actual_commit != expected_commit:
        raise ValueError(f"repository commit is {actual_commit}, expected {expected_commit}")

    pr_files = _load_json(pr_files_path)
    if not isinstance(pr_files, list):
        raise ValueError("PR files input must be a GitHub API list")
    gold_files = sorted(
        str(item.get("filename"))
        for item in pr_files
        if isinstance(item, dict) and item.get("filename")
    )
    if not gold_files:
        raise ValueError("PR files input contains no filenames")

    location = locate_issue(repo, str(issue.get("title", "")), str(issue.get("body") or ""), top_k)
    ranked_paths = [candidate["path"] for candidate in location["candidates"]]
    ranks = {path: ranked_paths.index(path) + 1 for path in gold_files if path in ranked_paths}
    implementation_gold = [
        path
        for path in gold_files
        if "/tests/" not in f"/{path}" and not Path(path).name.startswith("test_")
    ]
    implementation_hits = {path: ranks[path] for path in implementation_gold if path in ranks}

    return {
        "schema_version": "real-project-trial/v1",
        "source": {
            "repository": expected_repo,
            "repository_url": f"https://github.com/{expected_repo}",
            "issue_url": str(issue.get("html_url", "")),
            "issue_number": issue.get("number"),
            "issue_title": location["query"]["title"],
            "fix_pr_url": fix_pr_url,
            "base_commit": actual_commit,
            "issue_input_sha256": _sha256(issue_path),
            "pr_input_sha256": _sha256(pr_path),
            "pr_files_input_sha256": _sha256(pr_files_path),
            "issue_referenced_by_pr": str(issue.get("html_url", "")) in str(pr.get("body") or ""),
        },
        "safety": {
            "third_party_code_executed": False,
            "dependencies_installed": False,
            "scan_mode": "tracked source files read-only",
        },
        "evaluation": {
            "gold_files": gold_files,
            "candidate_paths": ranked_paths,
            "gold_file_ranks": ranks,
            "gold_recall_at_k": round(len(ranks) / len(gold_files), 3),
            "implementation_gold_files": implementation_gold,
            "implementation_hits": implementation_hits,
            "implementation_recall_at_k": round(
                len(implementation_hits) / len(implementation_gold), 3
            ) if implementation_gold else 1.0,
        },
        "location": location,
    }


def _atomic_write(path: Path, payload: Dict[str, Any]) -> None:
    if path.exists():
        raise FileExistsError(f"output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a real GitHub Issue localization trial.")
    parser.add_argument("--issue-json", type=Path, required=True)
    parser.add_argument("--pr-json", type=Path, required=True)
    parser.add_argument("--pr-files-json", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--top-k", type=int, default=10)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = evaluate_trial(
            args.issue_json,
            args.pr_json,
            args.pr_files_json,
            args.repo,
            args.top_k,
        )
        _atomic_write(args.output, report)
    except (FileExistsError, OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
