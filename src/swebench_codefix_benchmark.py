"""Prepare and score leakage-controlled SWE-bench code-fix tasks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.issue_draft import _atomic_write_json, _atomic_write_text
from src.issue_intake import find_sensitive_data
from src.repository_resolver import REPOSITORY_PATTERN
from src.repository_routing_benchmark import (
    BLOCKED_STATEMENT,
    MAX_PROBLEM_CHARS,
    SHA_PATTERN,
    _load_json_rows,
    _write_jsonl,
)


TASK_SCHEMA_VERSION = "swebench-codefix-task/v1"
LABEL_SCHEMA_VERSION = "swebench-codefix-label/v1"
RESULT_SCHEMA_VERSION = "swebench-codefix-result/v1"
REPORT_SCHEMA_VERSION = "swebench-codefix-evaluation/v1"
CASE_REF_PATTERN = re.compile(r"swebench_codefix_ref:[0-9a-f]{32}")
SOURCE_REF_PATTERN = re.compile(r"source_ref:[0-9a-f]{32}")
ALLOWED_RESULT_STATUSES = {"completed", "error", "skipped"}
MAX_TEST_CASES = 100_000
MAX_TEST_NAME_CHARS = 10_000
MAX_HARNESS_REPORT_BYTES = 2_000_000
MAX_HARNESS_REPORTS = 100_000


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _ratio(numerator: int, denominator: int) -> Optional[float]:
    return numerator / denominator if denominator else None


def _case_ref(dataset_revision: str, instance_id: str) -> str:
    material = f"{dataset_revision}\n{instance_id}\ncodefix"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"swebench_codefix_ref:{digest}"


def _source_ref(dataset_revision: str, instance_id: str) -> str:
    material = f"{dataset_revision}\n{instance_id}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"source_ref:{digest}"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_test_cases(value: Any, field_name: str) -> Tuple[str, ...]:
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"SWE-bench row {field_name} must contain a JSON array") from exc
    if not isinstance(parsed, list):
        raise ValueError(f"SWE-bench row {field_name} must be an array")
    if len(parsed) > MAX_TEST_CASES:
        raise ValueError(f"SWE-bench row {field_name} contains too many tests")
    tests: List[str] = []
    for item in parsed:
        test_name = _text(item)
        if not test_name or len(test_name) > MAX_TEST_NAME_CHARS:
            raise ValueError(f"SWE-bench row {field_name} contains an invalid test")
        tests.append(test_name)
    if len(set(tests)) != len(tests):
        raise ValueError(f"SWE-bench row {field_name} contains duplicate tests")
    return tuple(tests)


def _validate_source_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    repository = _text(row.get("repo"))
    instance_id = _text(row.get("instance_id"))
    problem_statement = _text(row.get("problem_statement"))
    base_commit = _text(row.get("base_commit"))
    created_at = _text(row.get("created_at"))
    version = _text(row.get("version"))
    environment_setup_commit = _text(row.get("environment_setup_commit"))
    patch = row.get("patch")
    test_patch = row.get("test_patch")
    if not REPOSITORY_PATTERN.fullmatch(repository):
        raise ValueError("SWE-bench row repo must use owner/name format")
    if not instance_id or len(instance_id) > 256:
        raise ValueError("SWE-bench row instance_id is invalid")
    if not problem_statement or len(problem_statement) > MAX_PROBLEM_CHARS:
        raise ValueError("SWE-bench row problem_statement size is invalid")
    if not SHA_PATTERN.fullmatch(base_commit):
        raise ValueError("SWE-bench row base_commit is invalid")
    if created_at and len(created_at) > 64:
        raise ValueError("SWE-bench row created_at is invalid")
    if len(version) > 128:
        raise ValueError("SWE-bench row version is invalid")
    if environment_setup_commit and not SHA_PATTERN.fullmatch(environment_setup_commit):
        raise ValueError("SWE-bench row environment_setup_commit is invalid")
    if patch is not None and not isinstance(patch, str):
        raise ValueError("SWE-bench row patch must be text")
    if test_patch is not None and not isinstance(test_patch, str):
        raise ValueError("SWE-bench row test_patch must be text")
    fail_to_pass = _normalize_test_cases(row.get("FAIL_TO_PASS"), "FAIL_TO_PASS")
    pass_to_pass = _normalize_test_cases(row.get("PASS_TO_PASS"), "PASS_TO_PASS")
    if not fail_to_pass:
        raise ValueError("SWE-bench row FAIL_TO_PASS must not be empty")
    if set(fail_to_pass).intersection(pass_to_pass):
        raise ValueError("SWE-bench row test partitions must be disjoint")
    return {
        "repository": repository,
        "instance_id": instance_id,
        "problem_statement": problem_statement,
        "base_commit": base_commit,
        "created_at": created_at,
        "version": version,
        "environment_setup_commit": environment_setup_commit,
        "patch": patch or "",
        "test_patch": test_patch or "",
        "fail_to_pass": fail_to_pass,
        "pass_to_pass": pass_to_pass,
    }


def select_deterministic_rows(
    rows: Sequence[Mapping[str, Any]],
    maximum_instances: int,
    sample_seed: str,
) -> List[Mapping[str, Any]]:
    if not 1 <= maximum_instances <= 100_000:
        raise ValueError("maximum instances must be between 1 and 100000")
    seed = sample_seed.strip()
    if not seed or len(seed) > 128:
        raise ValueError("sample seed must be a nonempty bounded string")
    validated = [(_validate_source_row(row), row) for row in rows]
    ranked = sorted(
        validated,
        key=lambda item: (
            hashlib.sha256(
                f"{seed}\n{item[0]['instance_id']}".encode("utf-8")
            ).hexdigest(),
            item[0]["instance_id"],
        ),
    )
    return [row for _, row in ranked[:maximum_instances]]


def prepare_swebench_codefix_records(
    rows: Sequence[Mapping[str, Any]],
    dataset_name: str,
    dataset_revision: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    name = dataset_name.strip()
    revision = dataset_revision.strip()
    if not name or len(name) > 256:
        raise ValueError("dataset name must be a nonempty bounded string")
    if not revision or len(revision) > 128:
        raise ValueError("dataset revision must be a nonempty bounded string")
    validated = [_validate_source_row(row) for row in rows]
    instance_ids = [row["instance_id"] for row in validated]
    if len(set(instance_ids)) != len(instance_ids):
        raise ValueError("SWE-bench input contains duplicate instance identifiers")

    tasks: List[Dict[str, Any]] = []
    labels: List[Dict[str, Any]] = []
    blocked_count = 0
    for row in validated:
        case_ref = _case_ref(revision, row["instance_id"])
        findings = find_sensitive_data(
            {"problem_statement": row["problem_statement"]}
        )
        categories = sorted({finding.category for finding in findings})
        if findings:
            safe_problem = BLOCKED_STATEMENT
            preflight_status = "blocked"
            blocked_count += 1
        else:
            safe_problem = row["problem_statement"]
            preflight_status = "eligible"
        tasks.append(
            {
                "schema_version": TASK_SCHEMA_VERSION,
                "case_ref": case_ref,
                "source_type": "public_github_issue",
                "repository": row["repository"],
                "base_commit": row["base_commit"],
                "problem_statement": safe_problem,
                "problem_sha256": _sha256(safe_problem),
                "environment": {
                    "version": row["version"],
                    "environment_setup_commit": row["environment_setup_commit"],
                },
                "preflight": {
                    "status": preflight_status,
                    "redacted_categories": categories,
                },
                "evaluation_contract": {
                    "mode": "swebench_f2p_p2p",
                    "private_label_required": True,
                },
                "answer_fields_present": False,
            }
        )
        labels.append(
            {
                "schema_version": LABEL_SCHEMA_VERSION,
                "case_ref": case_ref,
                "source_ref": _source_ref(revision, row["instance_id"]),
                "dataset_name": name,
                "dataset_revision": revision,
                "instance_id": row["instance_id"],
                "repository": row["repository"],
                "base_commit": row["base_commit"],
                "created_at": row["created_at"],
                "version": row["version"],
                "environment_setup_commit": row["environment_setup_commit"],
                "expected_execution": preflight_status,
                "fail_to_pass": list(row["fail_to_pass"]),
                "pass_to_pass": list(row["pass_to_pass"]),
                "gold_patch_sha256": _sha256(row["patch"]),
                "test_patch_sha256": _sha256(row["test_patch"]),
            }
        )
    summary = {
        "schema_version": "swebench-codefix-preparation/v1",
        "dataset_name": name,
        "dataset_revision": revision,
        "source_rows": len(validated),
        "task_records": len(tasks),
        "label_records": len(labels),
        "eligible_rows": len(validated) - blocked_count,
        "blocked_sensitive_rows": blocked_count,
        "gold_patch_content_written": False,
        "test_patch_content_written": False,
        "test_names_written_to_tasks": False,
        "issue_or_pr_urls_written_to_tasks": False,
    }
    return tasks, labels, summary


def _strict_test_list(value: Any, field_name: str) -> List[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be an array")
    normalized = [_text(item) for item in value]
    if (
        any(not item or len(item) > MAX_TEST_NAME_CHARS for item in normalized)
        or len(normalized) > MAX_TEST_CASES
        or len(set(normalized)) != len(normalized)
    ):
        raise ValueError(f"{field_name} contains invalid tests")
    return normalized


def _validate_label(record: Mapping[str, Any]) -> Dict[str, Any]:
    required = {
        "schema_version",
        "case_ref",
        "source_ref",
        "dataset_name",
        "dataset_revision",
        "instance_id",
        "repository",
        "base_commit",
        "created_at",
        "version",
        "environment_setup_commit",
        "expected_execution",
        "fail_to_pass",
        "pass_to_pass",
        "gold_patch_sha256",
        "test_patch_sha256",
    }
    if set(record) != required or record.get("schema_version") != LABEL_SCHEMA_VERSION:
        raise ValueError("code-fix label has an invalid schema")
    case_ref = _text(record.get("case_ref"))
    source_ref = _text(record.get("source_ref"))
    dataset_name = _text(record.get("dataset_name"))
    dataset_revision = _text(record.get("dataset_revision"))
    instance_id = _text(record.get("instance_id"))
    repository = _text(record.get("repository"))
    base_commit = _text(record.get("base_commit"))
    created_at = _text(record.get("created_at"))
    version = _text(record.get("version"))
    environment_setup_commit = _text(record.get("environment_setup_commit"))
    expected_execution = _text(record.get("expected_execution"))
    if not CASE_REF_PATTERN.fullmatch(case_ref):
        raise ValueError("code-fix label case_ref is invalid")
    if not SOURCE_REF_PATTERN.fullmatch(source_ref):
        raise ValueError("code-fix label source_ref is invalid")
    if not dataset_name or len(dataset_name) > 256:
        raise ValueError("code-fix label dataset_name is invalid")
    if not dataset_revision or len(dataset_revision) > 128:
        raise ValueError("code-fix label dataset_revision is invalid")
    if not instance_id or len(instance_id) > 256:
        raise ValueError("code-fix label instance_id is invalid")
    if case_ref != _case_ref(dataset_revision, instance_id):
        raise ValueError("code-fix label case_ref is not bound to its source")
    if source_ref != _source_ref(dataset_revision, instance_id):
        raise ValueError("code-fix label source_ref is not bound to its source")
    if not REPOSITORY_PATTERN.fullmatch(repository):
        raise ValueError("code-fix label repository is invalid")
    if not SHA_PATTERN.fullmatch(base_commit):
        raise ValueError("code-fix label base_commit is invalid")
    if len(created_at) > 64 or len(version) > 128:
        raise ValueError("code-fix label source metadata is invalid")
    if environment_setup_commit and not SHA_PATTERN.fullmatch(
        environment_setup_commit
    ):
        raise ValueError("code-fix label environment_setup_commit is invalid")
    if expected_execution not in {"eligible", "blocked"}:
        raise ValueError("code-fix label expected_execution is invalid")
    fail_to_pass = _strict_test_list(record.get("fail_to_pass"), "fail_to_pass")
    pass_to_pass = _strict_test_list(record.get("pass_to_pass"), "pass_to_pass")
    if not fail_to_pass or set(fail_to_pass).intersection(pass_to_pass):
        raise ValueError("code-fix label test partitions are invalid")
    for field_name in ("gold_patch_sha256", "test_patch_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", _text(record.get(field_name))):
            raise ValueError(f"code-fix label {field_name} is invalid")
    return dict(record)


def _validate_result(record: Mapping[str, Any]) -> Dict[str, Any]:
    required = {
        "schema_version",
        "case_ref",
        "status",
        "fail_to_pass_passed",
        "fail_to_pass_failed",
        "pass_to_pass_passed",
        "pass_to_pass_failed",
    }
    if set(record) != required or record.get("schema_version") != RESULT_SCHEMA_VERSION:
        raise ValueError("code-fix result has an invalid schema")
    case_ref = _text(record.get("case_ref"))
    status = _text(record.get("status"))
    if not CASE_REF_PATTERN.fullmatch(case_ref):
        raise ValueError("code-fix result case_ref is invalid")
    if status not in ALLOWED_RESULT_STATUSES:
        raise ValueError("code-fix result status is invalid")
    result = dict(record)
    for field_name in (
        "fail_to_pass_passed",
        "fail_to_pass_failed",
        "pass_to_pass_passed",
        "pass_to_pass_failed",
    ):
        result[field_name] = _strict_test_list(record.get(field_name), field_name)
    all_reported = [
        test
        for field_name in (
            "fail_to_pass_passed",
            "fail_to_pass_failed",
            "pass_to_pass_passed",
            "pass_to_pass_failed",
        )
        for test in result[field_name]
    ]
    if len(set(all_reported)) != len(all_reported):
        raise ValueError("code-fix result reports a test more than once")
    if status != "completed" and all_reported:
        raise ValueError("non-completed code-fix results must not report test outcomes")
    return result


def import_swebench_harness_reports(
    labels: Sequence[Mapping[str, Any]],
    reports: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    validated_labels = [_validate_label(record) for record in labels]
    label_by_instance = {
        record["instance_id"]: record for record in validated_labels
    }
    if len(label_by_instance) != len(validated_labels):
        raise ValueError("code-fix labels contain duplicate instance identifiers")
    imported: List[Dict[str, Any]] = []
    seen_instances = set()
    for report in reports:
        if not isinstance(report, Mapping) or len(report) != 1:
            raise ValueError(
                "SWE-bench harness report must contain exactly one instance"
            )
        instance_id, raw_entry = next(iter(report.items()))
        if (
            not isinstance(instance_id, str)
            or instance_id not in label_by_instance
        ):
            raise ValueError(
                "SWE-bench harness report contains an unknown instance"
            )
        if instance_id in seen_instances:
            raise ValueError(
                "SWE-bench harness reports contain a duplicate instance"
            )
        seen_instances.add(instance_id)
        if not isinstance(raw_entry, Mapping):
            raise ValueError("SWE-bench harness report entry is invalid")
        label = label_by_instance[instance_id]
        base = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "case_ref": label["case_ref"],
        }
        if raw_entry.get("patch_successfully_applied") is not True:
            imported.append(
                {
                    **base,
                    "status": "error",
                    "fail_to_pass_passed": [],
                    "fail_to_pass_failed": [],
                    "pass_to_pass_passed": [],
                    "pass_to_pass_failed": [],
                }
            )
            continue
        for field_name in ("patch_exists", "patch_is_None"):
            if field_name in raw_entry and not isinstance(
                raw_entry.get(field_name),
                bool,
            ):
                raise ValueError(
                    "SWE-bench harness patch metadata is invalid"
                )
        if raw_entry.get("patch_exists") is False or raw_entry.get(
            "patch_is_None"
        ) is True:
            raise ValueError(
                "applied SWE-bench harness report has conflicting patch metadata"
            )
        tests_status = raw_entry.get("tests_status")
        if not isinstance(tests_status, Mapping):
            raise ValueError(
                "applied SWE-bench harness report has no tests_status"
            )
        required_partitions = {"FAIL_TO_PASS", "PASS_TO_PASS"}
        auxiliary_partitions = {"FAIL_TO_FAIL", "PASS_TO_FAIL"}
        partition_names = set(tests_status)
        if not required_partitions.issubset(partition_names) or not (
            partition_names <= required_partitions | auxiliary_partitions
        ):
            raise ValueError(
                "SWE-bench harness report test partitions are invalid"
            )

        def partition(name: str) -> Tuple[List[str], List[str]]:
            value = tests_status.get(name)
            if not isinstance(value, Mapping) or set(value) != {
                "success",
                "failure",
            }:
                raise ValueError(
                    "SWE-bench harness report test status is invalid"
                )
            success = _strict_test_list(
                value.get("success"),
                f"harness {name} success",
            )
            failure = _strict_test_list(
                value.get("failure"),
                f"harness {name} failure",
            )
            if set(success).intersection(failure):
                raise ValueError(
                    "SWE-bench harness report duplicates a test outcome"
                )
            return success, failure

        fail_passed, fail_failed = partition("FAIL_TO_PASS")
        pass_passed, pass_failed = partition("PASS_TO_PASS")
        for auxiliary_name in sorted(auxiliary_partitions & partition_names):
            auxiliary_success, auxiliary_failure = partition(auxiliary_name)
            if auxiliary_success or auxiliary_failure:
                raise ValueError(
                    "SWE-bench harness report contains unsupported auxiliary "
                    "test outcomes"
                )
        if set(fail_passed).union(fail_failed) != set(
            label["fail_to_pass"]
        ) or set(pass_passed).union(pass_failed) != set(
            label["pass_to_pass"]
        ):
            raise ValueError(
                "SWE-bench harness report does not exactly cover private tests"
            )
        resolved = not fail_failed and not pass_failed
        harness_resolved = raw_entry.get("resolved")
        if not isinstance(harness_resolved, bool):
            raise ValueError(
                "SWE-bench harness report resolved status is invalid"
            )
        if harness_resolved != resolved:
            raise ValueError(
                "SWE-bench harness resolved status conflicts with test outcomes"
            )
        imported.append(
            {
                **base,
                "status": "completed",
                "fail_to_pass_passed": fail_passed,
                "fail_to_pass_failed": fail_failed,
                "pass_to_pass_passed": pass_passed,
                "pass_to_pass_failed": pass_failed,
            }
        )
    return imported


def _load_harness_report_files(root: Path) -> List[Dict[str, Any]]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("SWE-bench harness report root must be a directory")

    def handle_walk_error(exc: OSError) -> None:
        raise ValueError("unable to scan SWE-bench harness report root") from exc

    paths: List[Path] = []
    for directory, names, files in os.walk(
        root,
        followlinks=False,
        onerror=handle_walk_error,
    ):
        names[:] = [
            name
            for name in names
            if not (Path(directory) / name).is_symlink()
        ]
        if "report.json" in files:
            path = Path(directory) / "report.json"
            if path.is_symlink():
                raise ValueError(
                    "SWE-bench harness report must not be a symbolic link"
                )
            paths.append(path)
    if not paths:
        raise ValueError("SWE-bench harness report root contains no report.json files")
    if len(paths) > MAX_HARNESS_REPORTS:
        raise ValueError("too many SWE-bench harness reports")
    reports: List[Dict[str, Any]] = []
    for path in sorted(paths):
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ValueError(
                "unable to read SWE-bench harness report"
            ) from exc
        if not raw or len(raw) > MAX_HARNESS_REPORT_BYTES:
            raise ValueError("SWE-bench harness report size is invalid")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(
                "SWE-bench harness report must contain UTF-8 JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise ValueError("SWE-bench harness report must be an object")
        reports.append(payload)
    return reports


def evaluate_swebench_codefix_results(
    labels: Sequence[Mapping[str, Any]],
    results: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    validated_labels = [_validate_label(record) for record in labels]
    validated_results = [_validate_result(record) for record in results]
    label_index = {record["case_ref"]: record for record in validated_labels}
    result_index = {record["case_ref"]: record for record in validated_results}
    if len(label_index) != len(validated_labels):
        raise ValueError("code-fix labels contain duplicate case_ref values")
    if len(result_index) != len(validated_results):
        raise ValueError("code-fix results contain duplicate case_ref values")
    unknown_results = sorted(set(result_index).difference(label_index))
    if unknown_results:
        raise ValueError("code-fix results contain unknown case_ref values")
    dataset_names = {record["dataset_name"] for record in validated_labels}
    dataset_revisions = {record["dataset_revision"] for record in validated_labels}
    if len(dataset_names) != 1 or len(dataset_revisions) != 1:
        raise ValueError("code-fix labels must use one dataset name and revision")

    eligible_count = 0
    completed_count = 0
    resolved_count = 0
    fail_total = 0
    fail_passed = 0
    pass_total = 0
    pass_passed = 0
    blocked_execution_violations = 0
    cases: List[Dict[str, Any]] = []
    for label in validated_labels:
        case_ref = label["case_ref"]
        result = result_index.get(case_ref)
        if label["expected_execution"] == "blocked":
            violation = result is not None and result["status"] == "completed"
            blocked_execution_violations += int(violation)
            cases.append(
                {
                    "case_ref": case_ref,
                    "expected_execution": "blocked",
                    "result_status": result["status"] if result else "missing",
                    "resolved": False,
                    "policy_violation": violation,
                }
            )
            continue
        eligible_count += 1
        expected_fail = set(label["fail_to_pass"])
        expected_pass = set(label["pass_to_pass"])
        fail_total += len(expected_fail)
        pass_total += len(expected_pass)
        if result is None or result["status"] != "completed":
            cases.append(
                {
                    "case_ref": case_ref,
                    "expected_execution": "eligible",
                    "result_status": result["status"] if result else "missing",
                    "resolved": False,
                    "fail_to_pass_rate": 0.0,
                    "pass_to_pass_rate": 0.0 if expected_pass else None,
                }
            )
            continue
        completed_count += 1
        reported_fail = set(result["fail_to_pass_passed"]) | set(
            result["fail_to_pass_failed"]
        )
        reported_pass = set(result["pass_to_pass_passed"]) | set(
            result["pass_to_pass_failed"]
        )
        if reported_fail != expected_fail or reported_pass != expected_pass:
            raise ValueError("completed code-fix result does not exactly cover expected tests")
        current_fail_passed = len(result["fail_to_pass_passed"])
        current_pass_passed = len(result["pass_to_pass_passed"])
        fail_passed += current_fail_passed
        pass_passed += current_pass_passed
        fail_rate = current_fail_passed / len(expected_fail)
        pass_rate = (
            current_pass_passed / len(expected_pass) if expected_pass else None
        )
        resolved = fail_rate == 1.0 and (pass_rate is None or pass_rate == 1.0)
        resolved_count += int(resolved)
        cases.append(
            {
                "case_ref": case_ref,
                "expected_execution": "eligible",
                "result_status": "completed",
                "resolved": resolved,
                "fail_to_pass_rate": fail_rate,
                "pass_to_pass_rate": pass_rate,
            }
        )
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "dataset_name": next(iter(dataset_names)),
        "dataset_revision": next(iter(dataset_revisions)),
        "counts": {
            "labels": len(validated_labels),
            "eligible": eligible_count,
            "blocked": len(validated_labels) - eligible_count,
            "results": len(validated_results),
            "completed": completed_count,
            "resolved": resolved_count,
            "missing_or_incomplete": eligible_count - completed_count,
            "blocked_execution_violations": blocked_execution_violations,
        },
        "metrics": {
            "execution_coverage": _ratio(completed_count, eligible_count),
            "resolved_rate": _ratio(resolved_count, eligible_count),
            "fail_to_pass_rate": _ratio(fail_passed, fail_total),
            "pass_to_pass_rate": _ratio(pass_passed, pass_total),
        },
        "cases": cases,
    }


def render_evaluation_markdown(report: Mapping[str, Any]) -> str:
    counts = report["counts"]
    metrics = report["metrics"]

    def percentage(value: Optional[float]) -> str:
        return "n/a" if value is None else f"{value * 100:.2f}%"

    return "\n".join(
        [
            "# SWE-bench code-fix evaluation",
            "",
            f"- Dataset: `{report['dataset_name']}`",
            f"- Dataset revision: `{report['dataset_revision']}`",
            f"- Eligible cases: {counts['eligible']}",
            f"- Completed cases: {counts['completed']}",
            f"- Fully resolved cases: {counts['resolved']}",
            f"- Execution coverage: {percentage(metrics['execution_coverage'])}",
            f"- Resolved rate: {percentage(metrics['resolved_rate'])}",
            f"- Fail-to-pass rate: {percentage(metrics['fail_to_pass_rate'])}",
            f"- Pass-to-pass rate: {percentage(metrics['pass_to_pass_rate'])}",
            f"- Blocked execution violations: {counts['blocked_execution_violations']}",
            "",
            "> A case is fully resolved only when every fail-to-pass test passes and "
            "every pass-to-pass test remains passing.",
            "",
        ]
    )


def _load_jsonl(path: Path, description: str) -> List[Dict[str, Any]]:
    if path.is_symlink():
        raise ValueError(f"{description} must not be a symbolic link")
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"unable to read {description}") from exc
    rows: List[Dict[str, Any]] = []
    for line_number, line in enumerate(content.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{description} is invalid at line {line_number}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{description} must contain JSON objects")
        rows.append(row)
    if not rows:
        raise ValueError(f"{description} must not be empty")
    return rows


def prepare_main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Split SWE-bench rows into code-fix tasks and private labels."
    )
    parser.add_argument("input", type=Path)
    parser.add_argument(
        "--dataset-name",
        default="SWE-bench/SWE-bench_Verified",
    )
    parser.add_argument("--dataset-revision", required=True)
    parser.add_argument("--tasks-output", type=Path, required=True)
    parser.add_argument("--labels-output", type=Path, required=True)
    parser.add_argument("--summary-output", type=Path, required=True)
    parser.add_argument("--max-instances", type=int)
    parser.add_argument("--sample-seed", default="swebench-codefix-pilot-v1")
    args = parser.parse_args(argv)
    try:
        outputs = (args.tasks_output, args.labels_output, args.summary_output)
        if len({path.resolve() for path in outputs}) != len(outputs):
            raise ValueError("preparation output paths must be distinct")
        if any(path.exists() for path in outputs):
            raise FileExistsError("preparation output already exists")
        rows = _load_json_rows(args.input)
        total_rows = len(rows)
        if args.max_instances is not None:
            rows = select_deterministic_rows(
                rows,
                args.max_instances,
                args.sample_seed,
            )
        tasks, labels, summary = prepare_swebench_codefix_records(
            rows,
            args.dataset_name,
            args.dataset_revision,
        )
        summary["source_total_rows"] = total_rows
        summary["sampling"] = (
            {
                "strategy": "sha256_global",
                "maximum_instances": args.max_instances,
                "seed": args.sample_seed,
            }
            if args.max_instances is not None
            else None
        )
        _write_jsonl(args.tasks_output, tasks)
        _write_jsonl(args.labels_output, labels)
        _atomic_write_json(args.summary_output, summary)
    except (FileExistsError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(args.tasks_output)
    print(args.labels_output)
    print(args.summary_output)
    return 0


def evaluate_main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Score code-fix test results against private SWE-bench labels."
    )
    parser.add_argument("labels", type=Path)
    parser.add_argument("results", type=Path)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.output_json.resolve() == args.output_md.resolve():
            raise ValueError("evaluation output paths must be distinct")
        if args.output_json.exists() or args.output_md.exists():
            raise FileExistsError("evaluation output already exists")
        labels = _load_jsonl(args.labels, "code-fix labels")
        results = _load_jsonl(args.results, "code-fix results")
        report = evaluate_swebench_codefix_results(labels, results)
        _atomic_write_json(args.output_json, report)
        _atomic_write_text(args.output_md, render_evaluation_markdown(report))
    except (FileExistsError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(args.output_json)
    print(args.output_md)
    return 0


def import_harness_main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Convert official SWE-bench per-instance report.json files "
            "into leakage-safe code-fix results."
        )
    )
    parser.add_argument("labels", type=Path)
    parser.add_argument("reports_root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.output.exists():
            raise FileExistsError("harness result output already exists")
        labels = _load_jsonl(args.labels, "code-fix labels")
        reports = _load_harness_report_files(args.reports_root)
        results = import_swebench_harness_reports(labels, reports)
        _write_jsonl(args.output, results)
    except (FileExistsError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(args.output)
    return 0
