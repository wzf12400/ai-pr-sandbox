"""Validated, source-neutral input for local Issue draft generation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List


SCHEMA_VERSION = "issue-intake/v1"
SOURCE_TYPES = {"manual", "jira", "kibana"}
REQUEST_TYPES = {"Bug", "Feature", "Performance", "Security", "Refactor", "Documentation", "Unknown"}
SEVERITIES = {"S0", "S1", "S2", "S3", "Unknown"}
AUTOMATION_SCOPES = {"triage_only", "analysis_only", "draft_pr", "manual_only"}
MAX_EVIDENCE_LINES = 50
MAX_INTERFACE_SUMMARY_CHARS = 4000


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _mapping(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _texts(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


@dataclass(frozen=True)
class TargetContext:
    product: str = ""
    repository: str = ""
    service: str = ""
    module: str = ""
    code_object: str = ""
    owner: str = ""

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "TargetContext":
        return cls(**{name: _text(payload.get(name)) for name in cls.__dataclass_fields__})


@dataclass(frozen=True)
class ProblemContext:
    background: str = ""
    current_behavior: str = ""
    expected_behavior: str = ""
    first_observed_at: str = ""

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ProblemContext":
        return cls(**{name: _text(payload.get(name)) for name in cls.__dataclass_fields__})


@dataclass(frozen=True)
class InterfaceContext:
    protocol: str = ""
    method: str = ""
    path_or_topic: str = ""
    upstream: str = ""
    downstream: str = ""
    request_sample: str = ""
    actual_response: str = ""
    expected_response: str = ""

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "InterfaceContext":
        return cls(**{name: _text(payload.get(name)) for name in cls.__dataclass_fields__})


@dataclass(frozen=True)
class ReproductionContext:
    preconditions: str = ""
    steps: List[str] = field(default_factory=list)
    frequency: str = ""
    reproducible: str = ""
    workaround: str = ""

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ReproductionContext":
        return cls(
            preconditions=_text(payload.get("preconditions")),
            steps=_texts(payload.get("steps")),
            frequency=_text(payload.get("frequency")),
            reproducible=_text(payload.get("reproducible")),
            workaround=_text(payload.get("workaround")),
        )


@dataclass(frozen=True)
class ErrorEvidence:
    error_code: str = ""
    exception_type: str = ""
    message: str = ""
    stack_trace: str = ""
    log_excerpt: str = ""

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ErrorEvidence":
        return cls(**{name: _text(payload.get(name)) for name in cls.__dataclass_fields__})


@dataclass(frozen=True)
class RuntimeContext:
    environment: str = ""
    version: str = ""
    commit_sha: str = ""
    image_tag: str = ""
    region: str = ""
    cluster: str = ""
    node: str = ""
    os_or_device: str = ""
    occurred_at: str = ""
    trace_id: str = ""
    request_id: str = ""
    session_or_job_id: str = ""

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "RuntimeContext":
        return cls(**{name: _text(payload.get(name)) for name in cls.__dataclass_fields__})


@dataclass(frozen=True)
class ImpactContext:
    affected_subjects: str = ""
    affected_flow: str = ""
    quantity_or_ratio: str = ""
    data_correctness: str = ""
    regulated_areas: str = ""
    business_risk: str = ""

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ImpactContext":
        return cls(**{name: _text(payload.get(name)) for name in cls.__dataclass_fields__})


@dataclass(frozen=True)
class IntakeRecord:
    source_type: str
    source_reference: str
    summary: str
    source_url: str = ""
    project_key: str = ""
    request_type: str = "Unknown"
    severity: str = "Unknown"
    target: TargetContext = field(default_factory=TargetContext)
    problem: ProblemContext = field(default_factory=ProblemContext)
    interface: InterfaceContext = field(default_factory=InterfaceContext)
    reproduction: ReproductionContext = field(default_factory=ReproductionContext)
    error: ErrorEvidence = field(default_factory=ErrorEvidence)
    runtime: RuntimeContext = field(default_factory=RuntimeContext)
    attachments: List[str] = field(default_factory=list)
    impact: ImpactContext = field(default_factory=ImpactContext)
    acceptance_criteria: List[str] = field(default_factory=list)
    automation_scope: str = "triage_only"
    data_safety_status: str = "unreviewed"
    schema_version: str = SCHEMA_VERSION
    input_safety_findings: List[str] = field(default_factory=list, repr=False, compare=False)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "IntakeRecord":
        return cls(
            source_type=_text(payload.get("source_type")),
            source_reference=_text(payload.get("source_reference")),
            summary=_text(payload.get("summary")),
            source_url=_text(payload.get("source_url")),
            project_key=_text(payload.get("project_key")),
            request_type=_text(payload.get("request_type")) or "Unknown",
            severity=_text(payload.get("severity")) or "Unknown",
            target=TargetContext.from_dict(_mapping(payload.get("target"))),
            problem=ProblemContext.from_dict(_mapping(payload.get("problem"))),
            interface=InterfaceContext.from_dict(_mapping(payload.get("interface"))),
            reproduction=ReproductionContext.from_dict(_mapping(payload.get("reproduction"))),
            error=ErrorEvidence.from_dict(_mapping(payload.get("error"))),
            runtime=RuntimeContext.from_dict(_mapping(payload.get("runtime"))),
            attachments=_texts(payload.get("attachments")),
            impact=ImpactContext.from_dict(_mapping(payload.get("impact"))),
            acceptance_criteria=_texts(payload.get("acceptance_criteria")),
            automation_scope=_text(payload.get("automation_scope")) or "triage_only",
            data_safety_status=_text(payload.get("data_safety_status")) or "unreviewed",
            schema_version=_text(payload.get("schema_version")) or SCHEMA_VERSION,
            input_safety_findings=[
                f"{finding.path}: {finding.category}" for finding in find_sensitive_data(payload)
            ],
        )

    @property
    def deduplication_key(self) -> str:
        identity = f"{self.source_type}:{self.source_reference}".lower().encode("utf-8")
        return hashlib.sha256(identity).hexdigest()

    def validate(self) -> ValidationResult:
        errors: List[str] = []
        warnings: List[str] = []

        if self.schema_version != SCHEMA_VERSION:
            errors.append(f"schema_version must be {SCHEMA_VERSION}")
        if self.source_type not in SOURCE_TYPES:
            errors.append(f"source_type must be one of {sorted(SOURCE_TYPES)}")
        if not self.source_reference:
            errors.append("source_reference is required")
        if not self.summary:
            errors.append("summary is required")
        if self.request_type not in REQUEST_TYPES:
            errors.append(f"request_type must be one of {sorted(REQUEST_TYPES)}")
        if self.severity not in SEVERITIES:
            errors.append(f"severity must be one of {sorted(SEVERITIES)}")
        if self.automation_scope not in AUTOMATION_SCOPES:
            errors.append(f"automation_scope must be one of {sorted(AUTOMATION_SCOPES)}")
        if self.data_safety_status != "sanitized":
            errors.append("data_safety_status must be sanitized before draft generation")

        if not any((self.target.product, self.target.repository, self.target.service, self.target.module)):
            errors.append("at least one target object is required")
        if not self.problem.current_behavior:
            errors.append("problem.current_behavior is required")
        if not self.problem.expected_behavior:
            errors.append("problem.expected_behavior is required")
        if not self.reproduction.steps:
            errors.append("at least one reproduction step is required")
        if len(self.acceptance_criteria) < 3:
            errors.append("at least three acceptance criteria are required")

        evidence_fields = {
            "error.stack_trace": self.error.stack_trace,
            "error.log_excerpt": self.error.log_excerpt,
        }
        for name, value in evidence_fields.items():
            if len(value.splitlines()) > MAX_EVIDENCE_LINES:
                errors.append(f"{name} must not exceed {MAX_EVIDENCE_LINES} lines")

        interface_summaries = {
            "interface.request_sample": self.interface.request_sample,
            "interface.actual_response": self.interface.actual_response,
            "interface.expected_response": self.interface.expected_response,
        }
        for name, value in interface_summaries.items():
            if len(value) > MAX_INTERFACE_SUMMARY_CHARS:
                errors.append(f"{name} must not exceed {MAX_INTERFACE_SUMMARY_CHARS} characters")

        if self.source_type == "jira":
            if not self.project_key:
                errors.append("Jira input requires project_key")
            if not self.source_url:
                errors.append("Jira input requires source_url")

        if self.source_type == "kibana":
            if not self.runtime.occurred_at:
                errors.append("Kibana input requires runtime.occurred_at")
            if not self.runtime.environment:
                errors.append("Kibana input requires runtime.environment")
            if not self.error.message and not self.error.log_excerpt:
                errors.append("Kibana input requires error.message or error.log_excerpt")
            if not self.runtime.trace_id and not self.runtime.request_id:
                warnings.append("Kibana input has no trace_id or request_id")

        normalized_findings = {
            f"{finding.path}: {finding.category}" for finding in find_sensitive_data(asdict(self))
        }
        for finding in sorted(set(self.input_safety_findings) | normalized_findings):
            errors.append(f"sensitive data detected at {finding}")

        return ValidationResult(not errors, errors, warnings)


@dataclass(frozen=True)
class SensitiveFinding:
    path: str
    category: str


SENSITIVE_PATTERNS = (
    ("credential", re.compile(r"(?i)\b(?:authorization|api[-_ ]?key|token|password|passwd|cookie)\s*[:=]\s*\S+")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("email address", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("Chinese ID number", re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)")),
    ("phone number", re.compile(r"(?<!\d)(?:1[3-9]\d{9}|\+\d(?:[ -]?\d){9,14})(?!\d)")),
)


def _walk_text(value: Any, path: str = "root") -> Iterable[tuple[str, str]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_text(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_text(item, f"{path}[{index}]")
    elif isinstance(value, str):
        yield path, value


def find_sensitive_data(payload: Dict[str, Any]) -> List[SensitiveFinding]:
    findings: List[SensitiveFinding] = []
    for path, value in _walk_text(payload):
        for category, pattern in SENSITIVE_PATTERNS:
            if pattern.search(value):
                findings.append(SensitiveFinding(path, category))
    return findings


def load_intake(path: Path) -> IntakeRecord:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON at line {exc.lineno}, column {exc.colno}") from exc
    if not isinstance(payload, dict):
        raise ValueError("input JSON must contain one object")
    return IntakeRecord.from_dict(payload)
