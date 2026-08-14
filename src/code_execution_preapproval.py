"""Bind automatic code-approval labels to reviewed, deterministic policy bytes."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.copilot_code_modifier import load_issue_code_policy


POLICY_SCHEMA_VERSION = "code-execution-preapproval-policy/v1"
POLICY_ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,80}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
ALLOWED_SOURCE_TYPES = frozenset({"LOG", "JIRA"})
# Natural-language Issues always require separate downstream human approval.
NATURAL_LANGUAGE_SOURCE_TYPES = frozenset({"NATURAL_LANGUAGE", "natural_language"})
MAX_POLICY_BYTES = 64_000


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _exact_keys(
    payload: Mapping[str, Any], required: Sequence[str], field: str
) -> None:
    missing = sorted(set(required) - set(payload))
    extra = sorted(set(payload) - set(required))
    if missing:
        raise ValueError(f"{field} is missing fields: {', '.join(missing)}")
    if extra:
        raise ValueError(f"{field} contains unsupported fields: {', '.join(extra)}")


@dataclass(frozen=True)
class CodeExecutionPreapprovalPolicy:
    policy_id: str
    policy_sha256: str
    repository: str
    issue_publication_policy_sha256: str
    issue_code_policy_sha256: str
    allowed_source_types: frozenset[str]
    required_labels: tuple[str, ...]
    max_issues_per_run: int

    def labels_for(self, source_type: str, publication_status: str) -> tuple[str, ...]:
        if source_type in NATURAL_LANGUAGE_SOURCE_TYPES:
            return ()
        if source_type not in self.allowed_source_types:
            return ()
        # Reused Issues keep their existing approval state. Automatic approval is
        # deliberately limited to the exact Issue created by this invocation.
        if publication_status != "created":
            return ()
        return self.required_labels


def load_code_execution_preapproval_policy(
    path: Path,
    confirmed_sha256: str,
    issue_publication_policy_path: Path,
    issue_code_policy_path: Path,
) -> CodeExecutionPreapprovalPolicy:
    if (
        path.is_symlink()
        or issue_publication_policy_path.is_symlink()
        or issue_code_policy_path.is_symlink()
    ):
        raise ValueError("code preapproval policies must not be symbolic links")
    try:
        raw = path.read_bytes()
        issue_publication_raw = issue_publication_policy_path.read_bytes()
    except OSError as exc:
        raise ValueError("unable to read code preapproval policy inputs") from exc
    if not raw or len(raw) > MAX_POLICY_BYTES:
        raise ValueError("code preapproval policy size is invalid")
    digest = hashlib.sha256(raw).hexdigest()
    if not SHA256_PATTERN.fullmatch(confirmed_sha256) or digest != confirmed_sha256:
        raise ValueError("code preapproval policy SHA-256 confirmation does not match")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("code preapproval policy must contain valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("code preapproval policy must be an object")
    required = (
        "schema_version",
        "policy_id",
        "repository",
        "issue_publication_policy_sha256",
        "issue_code_policy_sha256",
        "allowed_source_types",
        "required_labels",
        "max_issues_per_run",
    )
    _exact_keys(payload, required, "code preapproval policy")
    if payload.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise ValueError(f"code preapproval policy must use {POLICY_SCHEMA_VERSION}")
    policy_id = _text(payload.get("policy_id"))
    if not POLICY_ID_PATTERN.fullmatch(policy_id):
        raise ValueError("code preapproval policy_id is invalid")
    issue_publication_digest = hashlib.sha256(issue_publication_raw).hexdigest()
    if payload.get("issue_publication_policy_sha256") != issue_publication_digest:
        raise ValueError("code preapproval policy is not bound to the Issue publication policy")
    code_policy = load_issue_code_policy(issue_code_policy_path)
    if payload.get("issue_code_policy_sha256") != code_policy.sha256:
        raise ValueError("code preapproval policy is not bound to the Issue code policy")
    if payload.get("repository") != code_policy.repository:
        raise ValueError("code preapproval repository does not match the Issue code policy")
    raw_sources = payload.get("allowed_source_types")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ValueError("allowed_source_types must be a nonempty array")
    source_types = frozenset(_text(item) for item in raw_sources)
    if (
        "" in source_types
        or len(source_types) != len(raw_sources)
        or source_types & NATURAL_LANGUAGE_SOURCE_TYPES
        or not source_types <= ALLOWED_SOURCE_TYPES
    ):
        raise ValueError("code preapproval policy contains an invalid source type")
    raw_labels = payload.get("required_labels")
    if not isinstance(raw_labels, list) or not raw_labels:
        raise ValueError("required_labels must be a nonempty array")
    labels = tuple(_text(item) for item in raw_labels)
    if labels != code_policy.required_labels:
        raise ValueError("code preapproval labels do not match the Issue code policy")
    if payload.get("max_issues_per_run") != 1:
        raise ValueError("code preapproval is limited to one Issue per invocation")
    return CodeExecutionPreapprovalPolicy(
        policy_id=policy_id,
        policy_sha256=digest,
        repository=code_policy.repository,
        issue_publication_policy_sha256=issue_publication_digest,
        issue_code_policy_sha256=code_policy.sha256,
        allowed_source_types=source_types,
        required_labels=labels,
        max_issues_per_run=1,
    )
