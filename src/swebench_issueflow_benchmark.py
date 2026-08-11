"""Run SWE-bench problems through the terminal agent's Issue-generation entry."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from src import ai_issue_generator
from src.copilot_issue_provider import CopilotCLIIssueProvider
from src.issue_draft import _atomic_write_json
from src.local_control_center import (
    ROUTING_MODE_DEMO_SINGLE_REPOSITORY,
    ControlCenterConfig,
    ManagedRepository,
    _compose_managed_evidence,
)


SCHEMA_VERSION = "swebench-issueflow-generation/v1"
CASE_REF_PATTERN = re.compile(r"swebench_codefix_ref:[0-9a-f]{32}")
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
MAX_TASK_FILE_BYTES = 64_000_000
MAX_TASKS = 500


def load_tasks(path: Path) -> List[Dict[str, Any]]:
    if path.is_symlink():
        raise ValueError("SWE-bench Issue-flow input must not be a symbolic link")
    try:
        if not 0 < path.stat().st_size <= MAX_TASK_FILE_BYTES:
            raise ValueError("SWE-bench Issue-flow input size is invalid")
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError("unable to read SWE-bench Issue-flow input") from exc
    tasks: List[Dict[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            task = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"SWE-bench Issue-flow input is invalid at line {line_number}"
            ) from exc
        if not isinstance(task, dict):
            raise ValueError("every SWE-bench Issue-flow task must be an object")
        _validate_task(task)
        tasks.append(task)
    if not 1 <= len(tasks) <= MAX_TASKS:
        raise ValueError(
            f"SWE-bench Issue-flow input must contain between 1 and {MAX_TASKS} tasks"
        )
    if len({task["case_ref"] for task in tasks}) != len(tasks):
        raise ValueError("SWE-bench Issue-flow input contains duplicate case references")
    return tasks


def _validate_task(task: Mapping[str, Any]) -> None:
    case_ref = task.get("case_ref")
    repository = task.get("repository")
    problem = task.get("problem_statement")
    if not isinstance(case_ref, str) or not CASE_REF_PATTERN.fullmatch(case_ref):
        raise ValueError("SWE-bench Issue-flow case_ref is invalid")
    if not isinstance(repository, str) or not REPOSITORY_PATTERN.fullmatch(repository):
        raise ValueError("SWE-bench Issue-flow repository is invalid")
    if not isinstance(problem, str) or not problem.strip():
        raise ValueError("SWE-bench Issue-flow problem statement is invalid")
    if task.get("answer_fields_present") is not False:
        raise ValueError("SWE-bench Issue-flow task contains answer-bearing fields")


def terminal_evidence(task: Mapping[str, Any]) -> Dict[str, Any]:
    """Use the same single-repository natural-language projection as ai-agent."""
    _validate_task(task)
    repository = str(task["repository"])
    managed = ManagedRepository(
        repository=repository,
        local_path="",
        enabled=True,
        policy_id="swebench-issueflow",
        policy_sha256="0" * 64,
        base_branch="main",
        allowed_models=("gpt-5.6-sol",),
        default_model="gpt-5.6-sol",
        required_labels=("ai-code-approved",),
        allowed_write_paths=("**",),
    )
    config = ControlCenterConfig(
        github_login="benchmark-local",
        copilot_model="gpt-5.6-sol",
        routing_mode=ROUTING_MODE_DEMO_SINGLE_REPOSITORY,
        repositories=(managed,),
        log_source=None,
        sha256="0" * 64,
    )
    return _compose_managed_evidence(str(task["problem_statement"]), config)


def handoff_eligible(result: Mapping[str, Any]) -> bool:
    validation = result.get("validation")
    review = result.get("review")
    return bool(
        result.get("state") in {"ready_for_human_review", "needs_human_context"}
        and isinstance(validation, dict)
        and validation.get("valid") is True
        and isinstance(review, dict)
        and review.get("verdict") != "reject"
    )


def _safe_error_category(exc: ValueError) -> str:
    message = str(exc)
    if "valid JSON object" in message or "structured response" in message:
        return "structured_response_invalid"
    if "description contains unclassified high-entropy data" in message:
        return "input_safety_blocked"
    if "structured Issue generation failed" in message:
        return "issue_provider_failed"
    return "issue_generation_failed"


def run_generation(
    tasks: Sequence[Mapping[str, Any]],
    output_dir: Path,
    provider: Any,
) -> Dict[str, Any]:
    if output_dir.exists():
        raise ValueError("SWE-bench Issue-flow output already exists")
    output_dir.mkdir(parents=True)
    cases: List[Dict[str, Any]] = []
    for index, task in enumerate(tasks, 1):
        case_dir = output_dir / f"case-{index:03d}"
        case_dir.mkdir()
        try:
            evidence = terminal_evidence(task)
            _atomic_write_json(case_dir / "evidence.json", evidence)
            generation = ai_issue_generator.generate_issue(
                evidence,
                provider,
                provider,
            )
            ai_issue_generator.write_result(
                generation,
                case_dir / "generation.json",
                case_dir / "issue-draft.md",
            )
            case = {
                "case_ref": task["case_ref"],
                "generation_status": "completed",
                "state": generation["state"],
                "validation_valid": generation["validation"]["valid"],
                "review_verdict": generation["review"].get("verdict"),
                "handoff_eligible": handoff_eligible(generation),
                "private_labels_used": False,
                "raw_model_response_persisted": False,
            }
        except ValueError as exc:
            case = {
                "case_ref": task["case_ref"],
                "generation_status": "error",
                "state": None,
                "validation_valid": False,
                "review_verdict": None,
                "handoff_eligible": False,
                "reason_category": _safe_error_category(exc),
                "private_labels_used": False,
                "raw_model_response_persisted": False,
            }
        _atomic_write_json(case_dir / "stage-result.json", case)
        cases.append(case)
        print(
            json.dumps(
                {
                    "case": index,
                    "generation_status": case["generation_status"],
                    "state": case["state"],
                    "handoff_eligible": case["handoff_eligible"],
                },
                sort_keys=True,
            ),
            flush=True,
        )
    completed = sum(case["generation_status"] == "completed" for case in cases)
    eligible = sum(case["handoff_eligible"] for case in cases)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "cases": cases,
        "counts": {
            "inputs": len(cases),
            "structured_generation_completed": completed,
            "handoff_eligible": eligible,
        },
        "metrics": {
            "structured_generation_rate": completed / len(cases),
            "handoff_eligibility_rate": eligible / len(cases),
        },
        "boundaries": {
            "input_path": "terminal_natural_language_single_repository",
            "private_labels_used": False,
            "raw_model_responses_persisted": False,
            "github_writes": False,
            "code_modification_started": False,
        },
    }
    _atomic_write_json(output_dir / "summary.json", summary)
    markdown = (
        "# SWE-bench terminal Issue-generation pilot\n\n"
        f"- Inputs: {len(cases)}\n"
        f"- Structured generation completed: {completed}/{len(cases)}\n"
        f"- Eligible for approved-Issue handoff: {eligible}/{len(cases)}\n"
        "- GitHub writes: none\n"
        "- Code modification started: no\n"
    )
    (output_dir / "summary.md").write_text(markdown, encoding="utf-8")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate reviewed Issues from SWE-bench tasks through the terminal "
            "agent's natural-language evidence path."
        )
    )
    parser.add_argument("tasks", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--start-index", type=int, default=1)
    parser.add_argument("--max-cases", type=int, default=5)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if (
        not 1 <= args.start_index <= MAX_TASKS
        or not 1 <= args.max_cases <= MAX_TASKS
    ):
        print("error: case selection is invalid", file=sys.stderr)
        return 2
    try:
        all_tasks = load_tasks(args.tasks)
        start = args.start_index - 1
        tasks = all_tasks[start : start + args.max_cases]
        if not tasks:
            raise ValueError("SWE-bench Issue-flow case selection is empty")
        provider = CopilotCLIIssueProvider(args.model, timeout_seconds=300)
        run_generation(tasks, args.output_dir, provider)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(args.output_dir / "summary.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
