"""Prepare leakage-controlled SWE-bench routing cases and score predictions."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from src.issue_draft import _atomic_write_json, _atomic_write_text
from src.issue_intake import find_sensitive_data
from src.repository_resolver import REPOSITORY_PATTERN


INPUT_SCHEMA_VERSION = "repository-routing-benchmark-input/v1"
LABEL_SCHEMA_VERSION = "repository-routing-benchmark-label/v1"
PREDICTION_SCHEMA_VERSION = "repository-routing-benchmark-prediction/v1"
REPORT_SCHEMA_VERSION = "repository-routing-evaluation/v1"
CASE_REF_PATTERN = re.compile(r"swebench_ref:[0-9a-f]{32}")
SHA_PATTERN = re.compile(r"[0-9a-f]{7,64}")
GITHUB_URL_PATTERN = re.compile(r"https://github\.com/[^\s<>()\[\]{}]+", re.IGNORECASE)
MAX_DATASET_BYTES = 512_000_000
MAX_ROWS = 100_000
MAX_PROBLEM_CHARS = 100_000
ALLOWED_STATUSES = {"resolved", "ambiguous", "unknown", "blocked"}
ALLOWED_VARIANTS = {
    "original",
    "gold_removed",
    "ambiguous_duplicate",
    "information_ablation",
}
BLOCKED_STATEMENT = "[BLOCKED_BY_LOCAL_SENSITIVE_DATA_POLICY]"


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _ratio(numerator: int, denominator: int) -> Optional[float]:
    return numerator / denominator if denominator else None


def _case_ref(dataset_revision: str, instance_id: str, variant: str) -> str:
    material = f"{dataset_revision}\n{instance_id}\n{variant}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"swebench_ref:{digest}"


def _problem_digest(problem_statement: str) -> str:
    return hashlib.sha256(problem_statement.encode("utf-8")).hexdigest()


def _source_ref(dataset_revision: str, instance_id: str) -> str:
    material = f"{dataset_revision}\n{instance_id}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"source_ref:{digest}"


def _load_json_rows(path: Path) -> List[Dict[str, Any]]:
    if path.is_symlink():
        raise ValueError("SWE-bench input must not be a symbolic link")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError("unable to read SWE-bench input") from exc
    if not raw or len(raw) > MAX_DATASET_BYTES:
        raise ValueError("SWE-bench input size is invalid")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("SWE-bench input must be UTF-8 JSON or JSONL") from exc
    stripped = content.lstrip()
    rows: Any
    if stripped.startswith("["):
        try:
            rows = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError("SWE-bench JSON array is invalid") from exc
    elif stripped.startswith("{"):
        try:
            first = json.loads(content)
        except json.JSONDecodeError:
            rows = []
            for line_number, line in enumerate(content.splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"SWE-bench JSONL is invalid at line {line_number}"
                    ) from exc
                rows.append(row)
        else:
            rows = first.get("data") if isinstance(first, dict) else None
            if rows is None:
                rows = [first]
    else:
        raise ValueError("SWE-bench input must contain JSON or JSONL")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS:
        raise ValueError(f"SWE-bench input must contain between 1 and {MAX_ROWS} rows")
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("every SWE-bench row must be an object")
    return rows


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if path.exists():
        raise FileExistsError(f"output already exists: {path}")
    content = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
    )
    _atomic_write_text(path, content)


def _mask_answer_bearing_text(
    problem_statement: str,
    repository: str,
    instance_id: str,
) -> str:
    masked = GITHUB_URL_PATTERN.sub("[REDACTED_GITHUB_URL]", problem_statement)
    for answer in (repository, instance_id):
        if answer:
            masked = re.sub(re.escape(answer), "[REDACTED_REPOSITORY]", masked, flags=re.I)
    return masked


def _validate_source_row(row: Mapping[str, Any]) -> Tuple[str, str, str, str, str]:
    repository = _text(row.get("repo"))
    instance_id = _text(row.get("instance_id"))
    problem_statement = _text(row.get("problem_statement"))
    base_commit = _text(row.get("base_commit"))
    created_at = _text(row.get("created_at"))
    if not REPOSITORY_PATTERN.fullmatch(repository):
        raise ValueError("SWE-bench row repo must use owner/name format")
    if not instance_id or len(instance_id) > 256:
        raise ValueError("SWE-bench row instance_id is invalid")
    if not problem_statement or len(problem_statement) > MAX_PROBLEM_CHARS:
        raise ValueError("SWE-bench row problem_statement size is invalid")
    if not SHA_PATTERN.fullmatch(base_commit):
        raise ValueError("SWE-bench row base_commit is invalid")
    if len(created_at) > 64:
        raise ValueError("SWE-bench row created_at is invalid")
    return repository, instance_id, problem_statement, base_commit, created_at


def _input_record(
    case_ref: str,
    problem_statement: str,
    candidate_repositories: Sequence[str],
    preflight_status: str,
    redacted_categories: Sequence[str],
    derived_from: Optional[str],
) -> Dict[str, Any]:
    return {
        "schema_version": INPUT_SCHEMA_VERSION,
        "case_ref": case_ref,
        "source_type": "public_github_issue",
        "problem_statement": problem_statement,
        "problem_sha256": _problem_digest(problem_statement),
        "candidate_repositories": list(candidate_repositories),
        "preflight": {
            "status": preflight_status,
            "redacted_categories": list(redacted_categories),
        },
        "derived_from": derived_from,
        "answer_fields_present": False,
    }


def _label_record(
    case_ref: str,
    expected_status: str,
    expected_repository: Optional[str],
    source_repository: str,
    dataset_revision: str,
    instance_id: str,
    base_commit: str,
    created_at: str,
    variant: str,
) -> Dict[str, Any]:
    return {
        "schema_version": LABEL_SCHEMA_VERSION,
        "case_ref": case_ref,
        "expected_status": expected_status,
        "expected_repository": expected_repository,
        "source_repository": source_repository,
        "source_ref": _source_ref(dataset_revision, instance_id),
        "dataset_revision": dataset_revision,
        "gold_base_commit": base_commit,
        "created_at": created_at,
        "variant": variant,
    }


def prepare_swebench_records(
    rows: Sequence[Mapping[str, Any]],
    dataset_revision: str,
    derive_out_of_scope: bool = False,
    candidate_repositories: Optional[Sequence[str]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    revision = dataset_revision.strip()
    if not revision or len(revision) > 128:
        raise ValueError("dataset revision must be a nonempty bounded string")
    validated = [_validate_source_row(row) for row in rows]
    repositories = (
        sorted(candidate_repositories, key=str.casefold)
        if candidate_repositories is not None
        else sorted({item[0] for item in validated}, key=str.casefold)
    )
    if not repositories or len(repositories) > 500:
        raise ValueError("candidate repository set must contain between 1 and 500 items")
    if any(not REPOSITORY_PATTERN.fullmatch(repository) for repository in repositories):
        raise ValueError("candidate repository set contains an invalid repository")
    if len({repository.casefold() for repository in repositories}) != len(repositories):
        raise ValueError("candidate repository set contains duplicates")
    inputs: List[Dict[str, Any]] = []
    labels: List[Dict[str, Any]] = []
    blocked_count = 0
    derived_count = 0
    seen_case_refs = set()
    for repository, instance_id, problem, base_commit, created_at in validated:
        if repository not in repositories:
            raise ValueError("gold repository is outside the configured candidate set")
        case_ref = _case_ref(revision, instance_id, "original")
        if case_ref in seen_case_refs:
            raise ValueError("SWE-bench input contains duplicate instance identifiers")
        seen_case_refs.add(case_ref)
        findings = find_sensitive_data({"problem_statement": problem})
        categories = sorted({finding.category for finding in findings})
        if findings:
            safe_problem = BLOCKED_STATEMENT
            expected_status = "blocked"
            expected_repository = None
            preflight_status = "blocked"
            blocked_count += 1
        else:
            safe_problem = _mask_answer_bearing_text(problem, repository, instance_id)
            expected_status = "resolved"
            expected_repository = repository
            preflight_status = "eligible"
        inputs.append(
            _input_record(
                case_ref,
                safe_problem,
                repositories,
                preflight_status,
                categories,
                None,
            )
        )
        labels.append(
            _label_record(
                case_ref,
                expected_status,
                expected_repository,
                repository,
                revision,
                instance_id,
                base_commit,
                created_at,
                "original",
            )
        )
        if derive_out_of_scope and not findings and len(repositories) > 1:
            negative_ref = _case_ref(revision, instance_id, "gold_removed")
            negative_repositories = [
                candidate for candidate in repositories if candidate != repository
            ]
            inputs.append(
                _input_record(
                    negative_ref,
                    safe_problem,
                    negative_repositories,
                    "eligible",
                    (),
                    case_ref,
                )
            )
            labels.append(
                _label_record(
                    negative_ref,
                    "unknown",
                    None,
                    repository,
                    revision,
                    instance_id,
                    base_commit,
                    created_at,
                    "gold_removed",
                )
            )
            derived_count += 1
    summary = {
        "schema_version": "repository-routing-benchmark-preparation/v1",
        "dataset_revision": revision,
        "source_rows": len(rows),
        "input_records": len(inputs),
        "label_records": len(labels),
        "candidate_repositories": repositories,
        "blocked_sensitive_rows": blocked_count,
        "derived_out_of_scope_rows": derived_count,
        "answer_fields_written_to_inputs": False,
    }
    return inputs, labels, summary


def _load_jsonl_records(path: Path, label: str) -> List[Dict[str, Any]]:
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symbolic link")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"unable to read {label}") from exc
    records: List[Dict[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{label} is invalid at line {line_number}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"{label} line {line_number} must be an object")
        records.append(record)
    if not 1 <= len(records) <= MAX_ROWS * 2:
        raise ValueError(f"{label} record count is invalid")
    return records


def _validate_label(record: Mapping[str, Any]) -> Dict[str, Any]:
    required = {
        "schema_version",
        "case_ref",
        "expected_status",
        "expected_repository",
        "source_repository",
        "source_ref",
        "dataset_revision",
        "gold_base_commit",
        "created_at",
        "variant",
    }
    if set(record) != required or record.get("schema_version") != LABEL_SCHEMA_VERSION:
        raise ValueError("routing label has an invalid schema")
    case_ref = _text(record.get("case_ref"))
    expected_status = _text(record.get("expected_status"))
    expected_repository = record.get("expected_repository")
    source_repository = _text(record.get("source_repository"))
    source_ref = _text(record.get("source_ref"))
    dataset_revision = _text(record.get("dataset_revision"))
    created_at = _text(record.get("created_at"))
    variant = _text(record.get("variant"))
    if not CASE_REF_PATTERN.fullmatch(case_ref):
        raise ValueError("routing label case_ref is invalid")
    if expected_status not in ALLOWED_STATUSES:
        raise ValueError("routing label expected_status is invalid")
    if expected_status == "resolved":
        if not isinstance(expected_repository, str) or not REPOSITORY_PATTERN.fullmatch(
            expected_repository
        ):
            raise ValueError("resolved routing label requires a repository")
    elif expected_repository is not None:
        raise ValueError("unresolved routing label must not select a repository")
    if not REPOSITORY_PATTERN.fullmatch(source_repository):
        raise ValueError("routing label source_repository is invalid")
    if not re.fullmatch(r"source_ref:[0-9a-f]{32}", source_ref):
        raise ValueError("routing label source_ref is invalid")
    if not dataset_revision or len(dataset_revision) > 128:
        raise ValueError("routing label dataset_revision is invalid")
    if not SHA_PATTERN.fullmatch(_text(record.get("gold_base_commit"))):
        raise ValueError("routing label gold_base_commit is invalid")
    if len(created_at) > 64:
        raise ValueError("routing label created_at is invalid")
    if variant not in ALLOWED_VARIANTS:
        raise ValueError("routing label variant is invalid")
    return dict(record)


def _validate_prediction(record: Mapping[str, Any]) -> Dict[str, Any]:
    required = {
        "schema_version",
        "case_ref",
        "status",
        "selected_repository",
        "policy_version",
        "top_score",
        "runner_up_score",
        "margin",
    }
    if (
        set(record) != required
        or record.get("schema_version") != PREDICTION_SCHEMA_VERSION
    ):
        raise ValueError("routing prediction has an invalid schema")
    case_ref = _text(record.get("case_ref"))
    status = _text(record.get("status"))
    selected = record.get("selected_repository")
    if not CASE_REF_PATTERN.fullmatch(case_ref):
        raise ValueError("routing prediction case_ref is invalid")
    if status not in ALLOWED_STATUSES:
        raise ValueError("routing prediction status is invalid")
    if status == "resolved":
        if not isinstance(selected, str) or not REPOSITORY_PATTERN.fullmatch(selected):
            raise ValueError("resolved routing prediction requires a repository")
    elif selected is not None:
        raise ValueError("unresolved routing prediction must not select a repository")
    for name in ("top_score", "runner_up_score", "margin"):
        value = record.get(name)
        if value is not None and (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 0 <= value <= 100
        ):
            raise ValueError(f"routing prediction {name} is invalid")
    policy_version = _text(record.get("policy_version"))
    if not policy_version or len(policy_version) > 128:
        raise ValueError("routing prediction policy_version is invalid")
    return dict(record)


def _index_unique(
    records: Iterable[Mapping[str, Any]], validator: Any, label: str
) -> Dict[str, Dict[str, Any]]:
    indexed: Dict[str, Dict[str, Any]] = {}
    for record in records:
        validated = validator(record)
        case_ref = validated["case_ref"]
        if case_ref in indexed:
            raise ValueError(f"{label} contains duplicate case_ref")
        indexed[case_ref] = validated
    return indexed


def _wilson(successes: int, total: int, z: float = 1.959963984540054) -> Optional[Dict[str, float]]:
    if total == 0:
        return None
    probability = successes / total
    denominator = 1 + z * z / total
    center = (probability + z * z / (2 * total)) / denominator
    spread = (
        z
        * math.sqrt(
            probability * (1 - probability) / total + z * z / (4 * total * total)
        )
        / denominator
    )
    return {"lower": max(0.0, center - spread), "upper": min(1.0, center + spread)}


def evaluate_routing_predictions(
    labels: Sequence[Mapping[str, Any]],
    predictions: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    label_index = _index_unique(labels, _validate_label, "routing labels")
    prediction_index = _index_unique(
        predictions, _validate_prediction, "routing predictions"
    )
    extra_predictions = sorted(set(prediction_index) - set(label_index))
    if extra_predictions:
        raise ValueError("routing predictions contain unknown case_ref values")
    dataset_revisions = {record["dataset_revision"] for record in label_index.values()}
    if len(dataset_revisions) != 1:
        raise ValueError("routing labels must use one dataset revision")
    expected_counts = Counter()
    predicted_counts = Counter()
    confusion: Dict[str, Counter[str]] = defaultdict(Counter)
    expected_resolved = 0
    predicted_resolved = 0
    resolved_on_expected_resolved = 0
    correct_resolved = 0
    wrong_resolved = 0
    exact_outcomes = 0
    nonresolved_expected = 0
    correct_safe_abstentions = 0
    missing_predictions = 0
    per_repository: Dict[str, Counter[str]] = defaultdict(Counter)
    for case_ref, label_record in label_index.items():
        expected_status = label_record["expected_status"]
        expected_repository = label_record["expected_repository"]
        prediction = prediction_index.get(case_ref)
        if prediction is None:
            predicted_status = "missing"
            selected_repository = None
            missing_predictions += 1
        else:
            predicted_status = prediction["status"]
            selected_repository = prediction["selected_repository"]
        expected_counts[expected_status] += 1
        predicted_counts[predicted_status] += 1
        confusion[expected_status][predicted_status] += 1
        if predicted_status == "resolved":
            predicted_resolved += 1
        if expected_status == "resolved":
            expected_resolved += 1
            repository_counts = per_repository[expected_repository]
            repository_counts["total"] += 1
            if predicted_status == "resolved":
                resolved_on_expected_resolved += 1
            if (
                predicted_status == "resolved"
                and selected_repository == expected_repository
            ):
                correct_resolved += 1
                exact_outcomes += 1
                repository_counts["correct_resolved"] += 1
            elif predicted_status == "resolved":
                wrong_resolved += 1
                repository_counts["wrong_resolved"] += 1
            else:
                repository_counts["abstained"] += 1
        else:
            nonresolved_expected += 1
            if predicted_status == "resolved":
                wrong_resolved += 1
            elif predicted_status == expected_status:
                correct_safe_abstentions += 1
                exact_outcomes += 1
    per_repository_rows = []
    recall_values = []
    for repository in sorted(per_repository, key=str.casefold):
        counts = per_repository[repository]
        recall = _ratio(counts["correct_resolved"], counts["total"])
        recall_values.append(recall or 0.0)
        per_repository_rows.append(
            {
                "repository": repository,
                "total": counts["total"],
                "correct_resolved": counts["correct_resolved"],
                "wrong_resolved": counts["wrong_resolved"],
                "abstained": counts["abstained"],
                "correct_route_recall": recall,
            }
        )
    auto_route_precision = _ratio(correct_resolved, predicted_resolved)
    false_route_rate = _ratio(wrong_resolved, predicted_resolved)
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "dataset_revision": next(iter(dataset_revisions)),
        "counts": {
            "labels": len(label_index),
            "predictions": len(prediction_index),
            "missing_predictions": missing_predictions,
            "expected_resolved": expected_resolved,
            "predicted_resolved": predicted_resolved,
            "resolved_on_expected_resolved": resolved_on_expected_resolved,
            "correct_resolved": correct_resolved,
            "wrong_resolved": wrong_resolved,
            "nonresolved_expected": nonresolved_expected,
            "correct_safe_abstentions": correct_safe_abstentions,
        },
        "metrics": {
            "auto_route_precision": auto_route_precision,
            "false_route_rate": false_route_rate,
            "resolved_coverage": _ratio(
                resolved_on_expected_resolved, expected_resolved
            ),
            "correct_route_recall": _ratio(correct_resolved, expected_resolved),
            "exact_outcome_accuracy": _ratio(exact_outcomes, len(label_index)),
            "safe_abstention_accuracy": _ratio(
                correct_safe_abstentions, nonresolved_expected
            ),
            "macro_repository_recall": (
                sum(recall_values) / len(recall_values) if recall_values else None
            ),
        },
        "confidence_intervals_95": {
            "auto_route_precision": _wilson(correct_resolved, predicted_resolved),
            "false_route_rate": _wilson(wrong_resolved, predicted_resolved),
        },
        "expected_status_counts": dict(sorted(expected_counts.items())),
        "predicted_status_counts": dict(sorted(predicted_counts.items())),
        "status_confusion": {
            expected: dict(sorted(predicted.items()))
            for expected, predicted in sorted(confusion.items())
        },
        "per_repository": per_repository_rows,
        "audit": {
            "private_labels_provided_to_predictor": False,
            "answer_fields_used_as_predictions": False,
            "missing_predictions_count_as_incorrect": True,
        },
    }
    return report


def _percent(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value * 100:.2f}%"


def render_evaluation_markdown(report: Mapping[str, Any]) -> str:
    counts = report["counts"]
    metrics = report["metrics"]
    lines = [
        "# Repository routing evaluation",
        "",
        f"- Dataset revision: `{report['dataset_revision']}`",
        f"- Labels / predictions: {counts['labels']} / {counts['predictions']}",
        f"- Missing predictions: {counts['missing_predictions']}",
        "",
        "## Core metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Auto-route precision | {_percent(metrics['auto_route_precision'])} |",
        f"| False-route rate | {_percent(metrics['false_route_rate'])} |",
        f"| Resolved coverage | {_percent(metrics['resolved_coverage'])} |",
        f"| Correct-route recall | {_percent(metrics['correct_route_recall'])} |",
        f"| Exact outcome accuracy | {_percent(metrics['exact_outcome_accuracy'])} |",
        f"| Safe abstention accuracy | {_percent(metrics['safe_abstention_accuracy'])} |",
        f"| Macro repository recall | {_percent(metrics['macro_repository_recall'])} |",
        "",
        "## Counts",
        "",
        f"- Correct resolved: {counts['correct_resolved']}",
        f"- Wrong resolved: {counts['wrong_resolved']}",
        f"- Expected non-resolved: {counts['nonresolved_expected']}",
        f"- Correct safe abstentions: {counts['correct_safe_abstentions']}",
        "",
        "## Per repository",
        "",
        "| Repository | Total | Correct | Wrong route | Abstained | Recall |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in report["per_repository"]:
        lines.append(
            "| {repository} | {total} | {correct_resolved} | {wrong_resolved} | "
            "{abstained} | {recall} |".format(
                **row, recall=_percent(row["correct_route_recall"])
            )
        )
    lines.extend(
        [
            "",
            "> Predictions never receive SWE-bench repository labels, URLs, patches, "
            "test patches, changed paths, or raw instance identifiers.",
            "",
        ]
    )
    return "\n".join(lines)


def build_prepare_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Split SWE-bench rows into leakage-controlled routing inputs and labels."
    )
    parser.add_argument("input", type=Path, help="SWE-bench JSON or JSONL export.")
    parser.add_argument("--dataset-revision", required=True)
    parser.add_argument("--inputs-output", type=Path, required=True)
    parser.add_argument("--labels-output", type=Path, required=True)
    parser.add_argument("--summary-output", type=Path, required=True)
    parser.add_argument("--derive-out-of-scope", action="store_true")
    return parser


def prepare_main(argv: Optional[List[str]] = None) -> int:
    args = build_prepare_parser().parse_args(argv)
    try:
        output_paths = (
            args.inputs_output,
            args.labels_output,
            args.summary_output,
        )
        if len({path.resolve() for path in output_paths}) != len(output_paths):
            raise ValueError("preparation output paths must be distinct")
        for output_path in output_paths:
            if output_path.exists():
                raise FileExistsError(f"output already exists: {output_path}")
        rows = _load_json_rows(args.input)
        inputs, labels, summary = prepare_swebench_records(
            rows,
            args.dataset_revision,
            derive_out_of_scope=args.derive_out_of_scope,
        )
        _write_jsonl(args.inputs_output, inputs)
        _write_jsonl(args.labels_output, labels)
        _atomic_write_json(args.summary_output, summary)
    except (FileExistsError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(args.inputs_output)
    print(args.labels_output)
    print(args.summary_output)
    return 0


def build_evaluate_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Score repository-routing predictions against separated labels."
    )
    parser.add_argument("labels", type=Path)
    parser.add_argument("predictions", type=Path)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    return parser


def evaluate_main(argv: Optional[List[str]] = None) -> int:
    args = build_evaluate_parser().parse_args(argv)
    try:
        if args.output_json.resolve() == args.output_md.resolve():
            raise ValueError("evaluation output paths must be distinct")
        labels = _load_jsonl_records(args.labels, "routing labels")
        predictions = _load_jsonl_records(args.predictions, "routing predictions")
        report = evaluate_routing_predictions(labels, predictions)
        if args.output_json.exists() or args.output_md.exists():
            raise FileExistsError("evaluation output already exists")
        _atomic_write_json(args.output_json, report)
        _atomic_write_text(args.output_md, render_evaluation_markdown(report))
    except (FileExistsError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(args.output_json)
    print(args.output_md)
    return 0
