"""Predict benchmark repository routes from bounded local source snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from src.issue_draft import _atomic_write_json, _atomic_write_text
from src.repository_resolver import REPOSITORY_PATTERN
from src.repository_routing_benchmark import (
    BLOCKED_STATEMENT,
    CASE_REF_PATTERN,
    INPUT_SCHEMA_VERSION,
    PREDICTION_SCHEMA_VERSION,
)


SNAPSHOT_MANIFEST_SCHEMA_VERSION = "repository-routing-snapshot-manifest/v1"
PILOT_AUDIT_SCHEMA_VERSION = "repository-routing-pilot-audit/v1"
PILOT_POLICY_VERSION = "repository-routing-lexical-snapshot/v1"
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
IDENTIFIER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
DOTTED_PATTERN = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b"
)
CALL_PATTERN = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
CAMEL_PATTERN = re.compile(r"\b[A-Z][A-Za-z0-9]{3,}\b")
SNAKE_PATTERN = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
PATH_PATTERN = re.compile(r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.pyi?\b")
CODE_SPAN_PATTERN = re.compile(r"`([^`\n]{1,160})`")
SOURCE_EXTENSIONS = {
    ".cfg",
    ".ini",
    ".md",
    ".py",
    ".pyi",
    ".rst",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
IGNORED_DIRECTORY_NAMES = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "vendor",
}
STOPWORDS = {
    "assert",
    "class",
    "config",
    "default",
    "error",
    "example",
    "false",
    "function",
    "import",
    "issue",
    "method",
    "module",
    "none",
    "object",
    "python",
    "return",
    "self",
    "should",
    "test",
    "tests",
    "true",
    "type",
    "value",
}
MAX_MANIFEST_BYTES = 256_000
MAX_INPUT_BYTES = 256_000_000
MAX_REPOSITORIES = 50
MAX_FILES_PER_REPOSITORY = 50_000
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_INDEX_BYTES = 1_000_000_000
MAX_TERMS_PER_CASE = 24
MAX_HITS_PER_TERM = 20
MINIMUM_RESOLVED_SCORE = 55
MINIMUM_MARGIN = 20
MINIMUM_EVIDENCE_TERMS = 2
AMBIGUOUS_SCORE = 25


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _load_json(path: Path, maximum_bytes: int, label: str) -> Any:
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symbolic link")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"unable to read {label}") from exc
    if not raw or len(raw) > maximum_bytes:
        raise ValueError(f"{label} size is invalid")
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must contain valid UTF-8 JSON") from exc


def _load_jsonl(path: Path, label: str) -> List[Dict[str, Any]]:
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symbolic link")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"unable to read {label}") from exc
    if not raw or len(raw) > MAX_INPUT_BYTES:
        raise ValueError(f"{label} size is invalid")
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} must use UTF-8") from exc
    rows: List[Dict[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{label} is invalid at line {line_number}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{label} line {line_number} must be an object")
        rows.append(row)
    if not rows:
        raise ValueError(f"{label} must contain at least one record")
    return rows


@dataclass(frozen=True)
class SnapshotEntry:
    repository: str
    path: Path
    commit: str


@dataclass(frozen=True)
class SnapshotManifest:
    snapshot_kind: str
    captured_at: str
    entries: Tuple[SnapshotEntry, ...]


@dataclass(frozen=True)
class QueryTerm:
    value: str
    family: str
    weight: int


@dataclass
class RepositoryIndex:
    repository: str
    commit: str
    aliases: Set[str]
    token_files: Dict[str, Set[str]]
    files_scanned: int
    bytes_scanned: int


def load_snapshot_manifest(path: Path) -> SnapshotManifest:
    payload = _load_json(path, MAX_MANIFEST_BYTES, "snapshot manifest")
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "snapshot_kind",
        "captured_at",
        "repositories",
    }:
        raise ValueError("snapshot manifest has an invalid schema")
    if payload.get("schema_version") != SNAPSHOT_MANIFEST_SCHEMA_VERSION:
        raise ValueError(
            f"snapshot manifest must use {SNAPSHOT_MANIFEST_SCHEMA_VERSION}"
        )
    snapshot_kind = _text(payload.get("snapshot_kind"))
    if snapshot_kind not in {"current_head_proxy", "historical_cutoff"}:
        raise ValueError("snapshot_kind is invalid")
    captured_at = _text(payload.get("captured_at"))
    if not captured_at or len(captured_at) > 64:
        raise ValueError("captured_at is invalid")
    raw_entries = payload.get("repositories")
    if not isinstance(raw_entries, list) or not 2 <= len(raw_entries) <= MAX_REPOSITORIES:
        raise ValueError("snapshot manifest must contain between 2 and 50 repositories")
    base = path.resolve().parent
    entries: List[SnapshotEntry] = []
    seen = set()
    for index, raw_entry in enumerate(raw_entries):
        if not isinstance(raw_entry, dict) or set(raw_entry) != {
            "repository",
            "path",
            "commit",
        }:
            raise ValueError(f"repositories[{index}] has an invalid schema")
        repository = _text(raw_entry.get("repository"))
        normalized = repository.casefold()
        if not REPOSITORY_PATTERN.fullmatch(repository) or normalized in seen:
            raise ValueError(f"repositories[{index}].repository is invalid or duplicated")
        seen.add(normalized)
        commit = _text(raw_entry.get("commit"))
        if not COMMIT_PATTERN.fullmatch(commit):
            raise ValueError(f"repositories[{index}].commit is invalid")
        raw_path = _text(raw_entry.get("path"))
        if not raw_path or "\x00" in raw_path:
            raise ValueError(f"repositories[{index}].path is invalid")
        snapshot_path = Path(raw_path)
        if not snapshot_path.is_absolute():
            snapshot_path = base / snapshot_path
        if snapshot_path.is_symlink():
            raise ValueError(f"repositories[{index}].path is not a safe directory")
        snapshot_path = snapshot_path.resolve()
        if not snapshot_path.is_dir():
            raise ValueError(f"repositories[{index}].path is not a safe directory")
        entries.append(SnapshotEntry(repository, snapshot_path, commit))
    return SnapshotManifest(snapshot_kind, captured_at, tuple(entries))


def _normalized_identifier(value: str) -> str:
    return value.casefold()


def _file_tokens(path: Path, content: str, allowed_terms: Set[str]) -> Set[str]:
    tokens = {_normalized_identifier(token) for token in IDENTIFIER_PATTERN.findall(content)}
    for part in path.parts:
        tokens.update(
            _normalized_identifier(token) for token in IDENTIFIER_PATTERN.findall(part)
        )
    return tokens & allowed_terms


def build_repository_indexes(
    manifest: SnapshotManifest,
    allowed_terms: Set[str],
    repositories_to_scan: Set[str],
) -> Tuple[Dict[str, RepositoryIndex], Dict[str, Any]]:
    if not allowed_terms:
        allowed_terms = set()
    indexes: Dict[str, RepositoryIndex] = {}
    total_bytes = 0
    total_files = 0
    for entry in manifest.entries:
        token_files: Dict[str, Set[str]] = defaultdict(set)
        repository_name = entry.repository.rsplit("/", 1)[-1].casefold()
        aliases = {
            repository_name,
            repository_name.replace("-", "_"),
            repository_name.replace("-", ""),
        }
        if entry.repository not in repositories_to_scan:
            indexes[entry.repository] = RepositoryIndex(
                repository=entry.repository,
                commit=entry.commit,
                aliases=aliases,
                token_files={},
                files_scanned=0,
                bytes_scanned=0,
            )
            continue
        repository_bytes = 0
        repository_files = 0
        for root, directory_names, file_names in os.walk(
            entry.path, topdown=True, followlinks=False
        ):
            directory_names[:] = sorted(
                name
                for name in directory_names
                if name not in IGNORED_DIRECTORY_NAMES
                and not (Path(root) / name).is_symlink()
            )
            for file_name in sorted(file_names):
                file_path = Path(root) / file_name
                if file_path.suffix.casefold() not in SOURCE_EXTENSIONS:
                    continue
                if file_path.is_symlink():
                    continue
                try:
                    size = file_path.stat().st_size
                except OSError as exc:
                    raise ValueError("unable to inspect repository snapshot") from exc
                if size < 0 or size > MAX_FILE_BYTES:
                    continue
                repository_files += 1
                if repository_files > MAX_FILES_PER_REPOSITORY:
                    raise ValueError(
                        f"repository snapshot exceeds file limit: {entry.repository}"
                    )
                repository_bytes += size
                total_bytes += size
                if total_bytes > MAX_TOTAL_INDEX_BYTES:
                    raise ValueError("snapshot index exceeds the total byte limit")
                try:
                    content = file_path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                except OSError as exc:
                    raise ValueError("unable to read repository snapshot") from exc
                relative_path = file_path.relative_to(entry.path).as_posix()
                relative_parts = Path(relative_path).parts
                if file_name == "__init__.py":
                    package = ""
                    if len(relative_parts) == 2:
                        package = relative_parts[0]
                    elif (
                        len(relative_parts) >= 3
                        and relative_parts[0] in {"lib", "src"}
                    ):
                        package = relative_parts[1]
                    normalized_package = package.casefold()
                    if (
                        len(normalized_package) >= 4
                        and normalized_package not in STOPWORDS
                        and IDENTIFIER_PATTERN.fullmatch(normalized_package)
                    ):
                        aliases.add(normalized_package)
                for token in _file_tokens(
                    Path(relative_path), content, allowed_terms
                ):
                    paths = token_files[token]
                    if len(paths) < MAX_HITS_PER_TERM:
                        paths.add(relative_path)
        total_files += repository_files
        indexes[entry.repository] = RepositoryIndex(
            repository=entry.repository,
            commit=entry.commit,
            aliases=aliases,
            token_files=dict(token_files),
            files_scanned=repository_files,
            bytes_scanned=repository_bytes,
        )
    audit = {
        "repositories_known": len(indexes),
        "repositories_indexed": len(repositories_to_scan),
        "files_scanned": total_files,
        "bytes_scanned": total_bytes,
        "query_terms_indexed": len(allowed_terms),
        "raw_source_snippets_persisted": False,
    }
    return indexes, audit


def _term_priority(term: QueryTerm) -> Tuple[int, str, str]:
    return (-term.weight, term.family, term.value.casefold())


def extract_query_terms(problem_statement: str) -> Tuple[QueryTerm, ...]:
    candidates: Dict[str, QueryTerm] = {}

    def add(value: str, family: str, weight: int) -> None:
        normalized = _normalized_identifier(value)
        if (
            len(normalized) < 4
            or len(normalized) > 128
            or normalized in STOPWORDS
            or normalized.isdigit()
        ):
            return
        candidate = QueryTerm(value=value, family=family, weight=weight)
        previous = candidates.get(normalized)
        if previous is None or _term_priority(candidate) < _term_priority(previous):
            candidates[normalized] = candidate

    for path in PATH_PATTERN.findall(problem_statement):
        stem = Path(path).stem
        add(stem, "python_path", 45)
        for token in IDENTIFIER_PATTERN.findall(path):
            add(token, "python_path", 45)
    for dotted in DOTTED_PATTERN.findall(problem_statement):
        parts = dotted.split(".")
        for token in parts[-3:]:
            add(token, "dotted_identifier", 32)
    for name in CALL_PATTERN.findall(problem_statement):
        add(name, "call_identifier", 30)
    for span in CODE_SPAN_PATTERN.findall(problem_statement):
        for token in IDENTIFIER_PATTERN.findall(span):
            add(token, "code_span", 26)
    for token in CAMEL_PATTERN.findall(problem_statement):
        add(token, "camel_identifier", 24)
    for token in SNAKE_PATTERN.findall(problem_statement):
        add(token, "snake_identifier", 22)
    return tuple(sorted(candidates.values(), key=_term_priority)[:MAX_TERMS_PER_CASE])


def _validate_input_record(record: Mapping[str, Any]) -> Dict[str, Any]:
    required = {
        "schema_version",
        "case_ref",
        "source_type",
        "problem_statement",
        "problem_sha256",
        "candidate_repositories",
        "preflight",
        "derived_from",
        "answer_fields_present",
    }
    if set(record) != required or record.get("schema_version") != INPUT_SCHEMA_VERSION:
        raise ValueError("routing benchmark input has an invalid schema")
    case_ref = _text(record.get("case_ref"))
    if not CASE_REF_PATTERN.fullmatch(case_ref):
        raise ValueError("routing benchmark input case_ref is invalid")
    statement = _text(record.get("problem_statement"))
    digest = _text(record.get("problem_sha256"))
    if not statement or hashlib.sha256(statement.encode()).hexdigest() != digest:
        raise ValueError("routing benchmark input problem digest is invalid")
    candidates = record.get("candidate_repositories")
    if (
        not isinstance(candidates, list)
        or len(candidates) < 1
        or len(candidates) > MAX_REPOSITORIES
        or any(
            not isinstance(repository, str)
            or not REPOSITORY_PATTERN.fullmatch(repository)
            for repository in candidates
        )
        or len({repository.casefold() for repository in candidates}) != len(candidates)
    ):
        raise ValueError("routing benchmark candidate repositories are invalid")
    preflight = record.get("preflight")
    if not isinstance(preflight, dict) or preflight.get("status") not in {
        "eligible",
        "blocked",
    }:
        raise ValueError("routing benchmark preflight is invalid")
    if record.get("answer_fields_present") is not False:
        raise ValueError("routing benchmark input contains answer fields")
    if preflight["status"] == "blocked" and statement != BLOCKED_STATEMENT:
        raise ValueError("blocked routing benchmark input is not minimized")
    return dict(record)


def _evidence_ref(repository: str, terms: Iterable[str], commit: str) -> str:
    material = "\n".join((repository, commit, *sorted(terms)))
    return "pilot_ref:" + hashlib.sha256(material.encode()).hexdigest()[:32]


def _present_unique_alias_owners(
    problem_statement: str,
    indexes: Mapping[str, RepositoryIndex],
) -> Set[str]:
    statement_casefold = problem_statement.casefold()
    alias_owners: Dict[str, Set[str]] = defaultdict(set)
    for repository, index in indexes.items():
        for alias in index.aliases:
            alias_owners[alias].add(repository)
    owners = set()
    for alias, repositories in alias_owners.items():
        if len(repositories) != 1:
            continue
        if re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(alias)}(?![A-Za-z0-9_])",
            statement_casefold,
        ):
            owners.update(repositories)
    return owners


def predict_record(
    record: Mapping[str, Any],
    indexes: Mapping[str, RepositoryIndex],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    validated = _validate_input_record(record)
    candidates = validated["candidate_repositories"]
    missing = sorted(set(candidates) - set(indexes))
    if missing:
        raise ValueError("routing benchmark input references an unindexed repository")
    if validated["preflight"]["status"] == "blocked":
        prediction = {
            "schema_version": PREDICTION_SCHEMA_VERSION,
            "case_ref": validated["case_ref"],
            "status": "blocked",
            "selected_repository": None,
            "policy_version": PILOT_POLICY_VERSION,
            "top_score": None,
            "runner_up_score": None,
            "margin": None,
        }
        return prediction, {
            "case_ref": validated["case_ref"],
            "terms_extracted": 0,
            "repositories_with_evidence": 0,
            "top_evidence_terms": 0,
            "evidence_ref": None,
            "outside_scope_alias_ref": None,
        }

    terms = extract_query_terms(validated["problem_statement"])
    statement_casefold = validated["problem_statement"].casefold()
    outside_scope_alias_owners = sorted(
        _present_unique_alias_owners(validated["problem_statement"], indexes)
        - set(candidates),
        key=str.casefold,
    )
    if outside_scope_alias_owners:
        material = "\n".join(outside_scope_alias_owners)
        prediction = {
            "schema_version": PREDICTION_SCHEMA_VERSION,
            "case_ref": validated["case_ref"],
            "status": "unknown",
            "selected_repository": None,
            "policy_version": PILOT_POLICY_VERSION,
            "top_score": 0,
            "runner_up_score": 0,
            "margin": 0,
        }
        return prediction, {
            "case_ref": validated["case_ref"],
            "terms_extracted": len(terms),
            "repositories_with_evidence": 0,
            "top_evidence_terms": 0,
            "evidence_ref": None,
            "outside_scope_alias_ref": (
                "alias_ref:"
                + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
            ),
        }
    alias_owners: Dict[str, Set[str]] = defaultdict(set)
    for repository in candidates:
        for alias in indexes[repository].aliases:
            alias_owners[alias].add(repository)
    document_frequency = Counter(
        term.value.casefold()
        for term in terms
        for repository in candidates
        if term.value.casefold() in indexes[repository].token_files
    )
    rows = []
    for repository in candidates:
        index = indexes[repository]
        matched_terms = []
        score = 0.0
        matched_aliases = sorted(
            (
                alias
                for alias in index.aliases
                if len(alias_owners[alias]) == 1
                and re.search(
                    rf"(?<![A-Za-z0-9_]){re.escape(alias)}(?![A-Za-z0-9_])",
                    statement_casefold,
                )
            ),
            key=lambda alias: (-len(alias), alias),
        )
        if matched_aliases:
            alias = matched_aliases[0]
            alias_term = QueryTerm(alias, "repository_alias", 50)
            score += alias_term.weight
            matched_terms.append((alias_term, float(alias_term.weight)))
        for term in terms:
            normalized = term.value.casefold()
            if normalized not in index.token_files:
                continue
            frequency = document_frequency[normalized]
            if frequency <= 0 or frequency > 4:
                continue
            multiplier = {1: 1.0, 2: 0.65, 3: 0.4, 4: 0.25}[frequency]
            contribution = term.weight * multiplier
            score += contribution
            matched_terms.append((term, contribution))
        if matched_terms:
            rows.append(
                {
                    "repository": repository,
                    "score": min(100, round(score)),
                    "terms": matched_terms,
                    "commit": index.commit,
                }
            )
    rows.sort(key=lambda row: (-row["score"], row["repository"].casefold()))
    top = rows[0] if rows else None
    top_score = top["score"] if top else 0
    runner_up_score = rows[1]["score"] if len(rows) > 1 else 0
    margin = top_score - runner_up_score
    top_terms = (
        {term.value.casefold() for term, _contribution in top["terms"]} if top else set()
    )
    if (
        top
        and top_score >= MINIMUM_RESOLVED_SCORE
        and len(top_terms) >= MINIMUM_EVIDENCE_TERMS
        and margin >= MINIMUM_MARGIN
    ):
        status = "resolved"
        selected_repository: Optional[str] = top["repository"]
    elif top_score >= AMBIGUOUS_SCORE:
        status = "ambiguous"
        selected_repository = None
    else:
        status = "unknown"
        selected_repository = None
    prediction = {
        "schema_version": PREDICTION_SCHEMA_VERSION,
        "case_ref": validated["case_ref"],
        "status": status,
        "selected_repository": selected_repository,
        "policy_version": PILOT_POLICY_VERSION,
        "top_score": top_score,
        "runner_up_score": runner_up_score,
        "margin": margin,
    }
    case_audit = {
        "case_ref": validated["case_ref"],
        "terms_extracted": len(terms),
        "repositories_with_evidence": len(rows),
        "top_evidence_terms": len(top_terms),
        "evidence_ref": (
            _evidence_ref(top["repository"], top_terms, top["commit"]) if top else None
        ),
        "outside_scope_alias_ref": None,
    }
    return prediction, case_audit


def predict_records(
    records: Sequence[Mapping[str, Any]],
    manifest: SnapshotManifest,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    validated_records = [_validate_input_record(record) for record in records]
    repositories_to_scan = {
        repository
        for record in validated_records
        for repository in record["candidate_repositories"]
    }
    allowed_terms = {
        term.value.casefold()
        for record in validated_records
        if record["preflight"]["status"] == "eligible"
        for term in extract_query_terms(record["problem_statement"])
    }
    indexes, index_audit = build_repository_indexes(
        manifest,
        allowed_terms,
        repositories_to_scan,
    )
    predictions = []
    case_audits = []
    seen = set()
    for record in validated_records:
        prediction, case_audit = predict_record(record, indexes)
        case_ref = prediction["case_ref"]
        if case_ref in seen:
            raise ValueError("routing benchmark input contains duplicate case_ref")
        seen.add(case_ref)
        predictions.append(prediction)
        case_audits.append(case_audit)
    status_counts = Counter(prediction["status"] for prediction in predictions)
    audit = {
        "schema_version": PILOT_AUDIT_SCHEMA_VERSION,
        "policy_version": PILOT_POLICY_VERSION,
        "snapshot_kind": manifest.snapshot_kind,
        "historical_snapshot": manifest.snapshot_kind == "historical_cutoff",
        "captured_at": manifest.captured_at,
        "thresholds": {
            "minimum_resolved_score": MINIMUM_RESOLVED_SCORE,
            "minimum_margin": MINIMUM_MARGIN,
            "minimum_evidence_terms": MINIMUM_EVIDENCE_TERMS,
            "ambiguous_score": AMBIGUOUS_SCORE,
        },
        "index": index_audit,
        "cases": len(predictions),
        "status_counts": dict(sorted(status_counts.items())),
        "case_audit": case_audits,
        "private_labels_loaded": False,
        "raw_problem_statements_persisted": False,
    }
    return predictions, audit


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Predict repository routes from bounded local source snapshots."
    )
    parser.add_argument("inputs", type=Path)
    parser.add_argument("--snapshots", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--audit-output", type=Path, required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        output_paths = (args.output.resolve(), args.audit_output.resolve())
        if len(set(output_paths)) != 2:
            raise ValueError("prediction output paths must be distinct")
        if args.output.exists() or args.audit_output.exists():
            raise FileExistsError("prediction output already exists")
        manifest = load_snapshot_manifest(args.snapshots)
        records = _load_jsonl(args.inputs, "routing benchmark inputs")
        predictions, audit = predict_records(records, manifest)
        content = "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            for record in predictions
        )
        _atomic_write_text(args.output, content)
        _atomic_write_json(args.audit_output, audit)
    except (FileExistsError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(args.output)
    print(args.audit_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
