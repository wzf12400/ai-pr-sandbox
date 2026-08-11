"""Deterministically approve, deduplicate, and publish one resolved Issue."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Protocol, Sequence, Tuple

from src import ai_issue_generator
from src.issue_intake import find_sensitive_data
from src.repository_resolver import (
    MINIMUM_MARGIN,
    MINIMUM_RESOLVED_SCORE,
    MINIMUM_STRONG_FAMILIES,
    POLICY_VERSION as RESOLUTION_POLICY_VERSION,
    REPOSITORY_PATTERN,
    RESOLUTION_SCHEMA_VERSION,
    RepositorySearchAdapter,
    RepositorySearchScope,
    resolve_repository,
)


AUTO_POLICY_SCHEMA_VERSION = "repository-auto-publish-policy/v1"
AUTOMATION_SCHEMA_VERSION = "repository-issue-automation/v1"
FINGERPRINT_VERSION = "repository-issue-fingerprint/v2"
LEGACY_FINGERPRINT_VERSION = "repository-issue-fingerprint/v1"
ROUTING_MODE_DEMO_SINGLE_REPOSITORY = "DEMO_SINGLE_REPOSITORY"
ROUTING_MODE_PRODUCTION_EVIDENCE = "PRODUCTION_EVIDENCE_ROUTING"
ROUTING_MODES = frozenset(
    {
        ROUTING_MODE_DEMO_SINGLE_REPOSITORY,
        ROUTING_MODE_PRODUCTION_EVIDENCE,
    }
)
AUTOMATION_INPUT_TYPES = frozenset(
    {"natural_language", "sanitized_evidence", "revision"}
)
POLICY_ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,80}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
ISSUE_URL_PATTERN = re.compile(r"https://github\.com/[^/]+/[^/]+/issues/(\d+)")
MAX_POLICY_BYTES = 64_000
MAX_ISSUES_SCANNED = 100
ALLOWED_GENERATION_STATES = {"ready_for_human_review", "needs_human_context"}
ALLOWED_ADAPTERS = {"github-code-search", "github-tree-probe"}
ALLOWED_PUBLICATION_PROVIDERS = {"github_cli", "github_rest_api"}
GITHUB_API_VERSION = "2026-03-10"


def _mapping(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalized(value: Any) -> str:
    text = " ".join(_text(value).casefold().split())
    return "" if text == "unknown" else text


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
class RepositoryAutoPublishPolicy:
    policy_id: str
    policy_sha256: str
    scope_id: str
    scope_sha256: str
    provider: str
    max_issues_per_run: int
    allowed_generation_states: frozenset[str]
    allowed_adapters: frozenset[str]


def load_auto_publish_policy(
    path: Path,
    confirmed_sha256: str,
    scope: RepositorySearchScope,
    scope_path: Path,
) -> RepositoryAutoPublishPolicy:
    if path.is_symlink() or scope_path.is_symlink():
        raise ValueError("automatic publication policy and scope must not be symbolic links")
    try:
        raw = path.read_bytes()
        scope_raw = scope_path.read_bytes()
    except OSError as exc:
        raise ValueError("unable to read automatic publication policy or scope") from exc
    if not raw or len(raw) > MAX_POLICY_BYTES:
        raise ValueError("automatic publication policy size is invalid")
    digest = hashlib.sha256(raw).hexdigest()
    if not SHA256_PATTERN.fullmatch(confirmed_sha256) or digest != confirmed_sha256:
        raise ValueError("automatic publication policy SHA-256 confirmation does not match")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("automatic publication policy must contain valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("automatic publication policy must be an object")
    required = (
        "schema_version",
        "policy_id",
        "scope_id",
        "scope_sha256",
        "provider",
        "max_issues_per_run",
        "allowed_generation_states",
        "allowed_adapters",
    )
    _exact_keys(payload, required, "automatic publication policy")
    if payload.get("schema_version") != AUTO_POLICY_SCHEMA_VERSION:
        raise ValueError(f"automatic publication policy must use {AUTO_POLICY_SCHEMA_VERSION}")
    policy_id = _text(payload.get("policy_id"))
    if not POLICY_ID_PATTERN.fullmatch(policy_id):
        raise ValueError("automatic publication policy_id is invalid")
    scope_id = _text(payload.get("scope_id"))
    scope_digest = _text(payload.get("scope_sha256"))
    actual_scope_digest = hashlib.sha256(scope_raw).hexdigest()
    if scope_id != scope.scope_id or scope_digest != actual_scope_digest:
        raise ValueError("automatic publication policy is not bound to the reviewed scope")
    provider = _text(payload.get("provider"))
    if provider not in ALLOWED_PUBLICATION_PROVIDERS:
        raise ValueError("automatic publication provider is unsupported")
    if payload.get("max_issues_per_run") != 1:
        raise ValueError("automatic publication is limited to one Issue per invocation")
    raw_states = payload.get("allowed_generation_states")
    if not isinstance(raw_states, list) or not raw_states:
        raise ValueError("allowed_generation_states must be a nonempty array")
    states = frozenset(_text(state) for state in raw_states)
    if "" in states or not states <= ALLOWED_GENERATION_STATES:
        raise ValueError("automatic publication policy contains an unsupported generation state")
    raw_adapters = payload.get("allowed_adapters")
    if not isinstance(raw_adapters, list) or not raw_adapters:
        raise ValueError("allowed_adapters must be a nonempty array")
    adapters = frozenset(_text(adapter) for adapter in raw_adapters)
    if "" in adapters or not adapters <= ALLOWED_ADAPTERS:
        raise ValueError("automatic publication policy contains an unsupported adapter")
    return RepositoryAutoPublishPolicy(
        policy_id=policy_id,
        policy_sha256=digest,
        scope_id=scope_id,
        scope_sha256=scope_digest,
        provider=provider,
        max_issues_per_run=1,
        allowed_generation_states=states,
        allowed_adapters=adapters,
    )


class GitHubIssueClient(Protocol):
    def list_issues(self, repository: str, limit: int) -> List[Dict[str, Any]]:
        ...

    def create_issue(self, repository: str, title: str, body: str) -> str:
        ...

    def get_issue(self, repository: str, number: int) -> Dict[str, Any]:
        ...

class GitHubCLIIssueClient:
    def __init__(self, timeout_seconds: float = 30.0):
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("GitHub Issue timeout must be between 1 and 120 seconds")
        self.timeout_seconds = timeout_seconds

    def list_issues(self, repository: str, limit: int) -> List[Dict[str, Any]]:
        if not REPOSITORY_PATTERN.fullmatch(repository) or not 1 <= limit <= MAX_ISSUES_SCANNED:
            raise ValueError("GitHub Issue search arguments are invalid")
        command = [
            "gh",
            "issue",
            "list",
            "--repo",
            repository,
            "--state",
            "all",
            "--limit",
            str(limit),
            "--json",
            "number,title,body,url,state",
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("GitHub Issue search could not be completed") from exc
        if completed.returncode != 0:
            raise ValueError("GitHub Issue search failed closed")
        try:
            issues = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("GitHub Issue search returned invalid JSON") from exc
        if not isinstance(issues, list):
            raise ValueError("GitHub Issue search returned an invalid result")
        return [issue for issue in issues[:limit] if isinstance(issue, dict)]

    def create_issue(self, repository: str, title: str, body: str) -> str:
        if not REPOSITORY_PATTERN.fullmatch(repository):
            raise ValueError("GitHub Issue repository is invalid")
        if not title.strip() or len(title) > 160 or not body.strip():
            raise ValueError("GitHub Issue title or body is invalid")
        temporary_path = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", prefix="issue-body-", suffix=".md", delete=False
            ) as handle:
                handle.write(body)
                temporary_path = handle.name
            completed = subprocess.run(
                [
                    "gh",
                    "issue",
                    "create",
                    "--repo",
                    repository,
                    "--title",
                    title,
                    "--body-file",
                    temporary_path,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("GitHub Issue creation could not be completed") from exc
        finally:
            if temporary_path:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass
        if completed.returncode != 0:
            raise ValueError("GitHub Issue creation failed closed")
        issue_url = completed.stdout.strip()
        if not ISSUE_URL_PATTERN.fullmatch(issue_url):
            raise ValueError("GitHub Issue creation returned an invalid URL")
        return issue_url

    def get_issue(self, repository: str, number: int) -> Dict[str, Any]:
        if not REPOSITORY_PATTERN.fullmatch(repository) or number < 1:
            raise ValueError("GitHub Issue reference is invalid")
        try:
            completed = subprocess.run(
                [
                    "gh",
                    "issue",
                    "view",
                    str(number),
                    "--repo",
                    repository,
                    "--json",
                    "number,title,body,url,state",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("GitHub Issue refetch could not be completed") from exc
        if completed.returncode != 0:
            raise ValueError("GitHub Issue refetch failed closed")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("GitHub Issue refetch returned invalid JSON") from exc
        return _validate_issue_snapshot(payload, repository, number, "url")


class GitHubRESTIssueClient:
    """Bounded GitHub REST adapter that never exposes token or response bodies."""

    def __init__(
        self,
        token: str,
        timeout_seconds: float = 30.0,
        api_base_url: str = "https://api.github.com",
    ) -> None:
        if not token.strip() or any(character in token for character in "\r\n"):
            raise ValueError("GitHub API token is missing or invalid")
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("GitHub Issue timeout must be between 1 and 120 seconds")
        if api_base_url.rstrip("/") != "https://api.github.com":
            raise ValueError("GitHub API base URL is not allowed")
        self._token = token.strip()
        self._timeout_seconds = timeout_seconds
        self._api_base_url = api_base_url.rstrip("/")

    @classmethod
    def from_environment(cls, timeout_seconds: float = 30.0) -> "GitHubRESTIssueClient":
        token = os.environ.get("GITHUB_ISSUE_TOKEN", "")
        return cls(token, timeout_seconds)

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> tuple[int, Any]:
        encoded = (
            None
            if payload is None
            else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )
        request = urllib.request.Request(
            self._api_base_url + path,
            data=encoded,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
                "User-Agent": "github-ai-agent-control-plane",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                status = response.status
                raw = response.read()
        except urllib.error.HTTPError as exception:
            raise ValueError(
                f"GitHub Issue API rejected the request with HTTP {exception.code}"
            ) from exception
        except (urllib.error.URLError, TimeoutError, OSError) as exception:
            raise ValueError("GitHub Issue API request could not be completed") from exception
        try:
            body = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError) as exception:
            raise ValueError("GitHub Issue API returned invalid JSON") from exception
        return status, body

    @staticmethod
    def _repository_path(repository: str) -> str:
        if not REPOSITORY_PATTERN.fullmatch(repository):
            raise ValueError("GitHub Issue repository is invalid")
        owner, name = repository.split("/", 1)
        return "/repos/{}/{}".format(
            urllib.parse.quote(owner, safe=""),
            urllib.parse.quote(name, safe=""),
        )

    def list_issues(self, repository: str, limit: int) -> List[Dict[str, Any]]:
        if not 1 <= limit <= MAX_ISSUES_SCANNED:
            raise ValueError("GitHub Issue search arguments are invalid")
        path = self._repository_path(repository)
        query = urllib.parse.urlencode(
            {"state": "all", "per_page": limit, "sort": "created", "direction": "desc"}
        )
        status, payload = self._request("GET", f"{path}/issues?{query}")
        if status != 200 or not isinstance(payload, list):
            raise ValueError("GitHub Issue API returned an invalid issue list")
        issues: List[Dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict) or "pull_request" in item:
                continue
            number = item.get("number")
            title = item.get("title")
            body = item.get("body")
            url = item.get("html_url")
            state = item.get("state")
            if (
                isinstance(number, int)
                and isinstance(title, str)
                and (body is None or isinstance(body, str))
                and isinstance(url, str)
                and ISSUE_URL_PATTERN.fullmatch(url)
                and state in {"open", "closed"}
            ):
                issues.append(
                    {
                        "number": number,
                        "title": title,
                        "body": body or "",
                        "url": url,
                        "state": state,
                    }
                )
        return issues[:limit]

    def create_issue(self, repository: str, title: str, body: str) -> str:
        path = self._repository_path(repository)
        safe_title = title.strip()
        if not safe_title or len(safe_title) > 160 or not body.strip():
            raise ValueError("GitHub Issue title or body is invalid")
        if find_sensitive_data({"title": safe_title, "body": body}):
            raise ValueError("GitHub Issue content failed sensitive-data validation")
        status, payload = self._request(
            "POST",
            f"{path}/issues",
            {"title": safe_title, "body": body},
        )
        if status != 201 or not isinstance(payload, dict):
            raise ValueError("GitHub Issue API returned an invalid creation result")
        issue_url = payload.get("html_url")
        if not isinstance(issue_url, str) or not ISSUE_URL_PATTERN.fullmatch(issue_url):
            raise ValueError("GitHub Issue API returned an invalid Issue URL")
        return issue_url

    def get_issue(self, repository: str, number: int) -> Dict[str, Any]:
        if number < 1:
            raise ValueError("GitHub Issue reference is invalid")
        path = self._repository_path(repository)
        status, payload = self._request("GET", f"{path}/issues/{number}")
        if status != 200:
            raise ValueError("GitHub Issue API returned an invalid refetch result")
        return _validate_issue_snapshot(payload, repository, number, "html_url")

    def add_labels(
        self, repository: str, number: int, labels: Sequence[str]
    ) -> Tuple[str, ...]:
        if number < 1:
            raise ValueError("GitHub Issue reference is invalid")
        requested = tuple(_text(label) for label in labels)
        if (
            not requested
            or len(requested) > 10
            or len(set(requested)) != len(requested)
            or any(not re.fullmatch(r"[A-Za-z0-9_.:/ -]{1,80}", label) for label in requested)
        ):
            raise ValueError("GitHub Issue labels are invalid")
        path = self._repository_path(repository)
        status, payload = self._request(
            "POST",
            f"{path}/issues/{number}/labels",
            {"labels": list(requested)},
        )
        if status != 200 or not isinstance(payload, list):
            raise ValueError("GitHub Issue API returned an invalid label result")
        applied = tuple(
            item.get("name")
            for item in payload
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        )
        if not set(requested) <= set(applied):
            raise ValueError("GitHub Issue API did not apply every required label")
        return requested


def _validate_issue_snapshot(
    payload: Any,
    repository: str,
    number: int,
    url_field: str,
) -> Dict[str, Any]:
    if not isinstance(payload, dict) or "pull_request" in payload:
        raise ValueError("GitHub Issue refetch returned an invalid Issue")
    title = payload.get("title")
    body = payload.get("body")
    url = payload.get(url_field)
    state = payload.get("state")
    expected_url = f"https://github.com/{repository}/issues/{number}"
    if (
        payload.get("number") != number
        or not isinstance(title, str)
        or not title.strip()
        or not isinstance(body, str)
        or not body.strip()
        or url != expected_url
        or state not in {"open", "closed"}
    ):
        raise ValueError("GitHub Issue refetch returned an inconsistent snapshot")
    if find_sensitive_data({"title": title, "body": body}):
        raise ValueError("GitHub Issue snapshot failed sensitive-data validation")
    return {
        "number": number,
        "title": title,
        "body": body,
        "url": url,
        "state": state,
        "repository_url": f"https://api.github.com/repos/{repository}",
    }


_REQUIREMENT_SPLIT_PATTERN = re.compile(r"(?:[，,。；;]|并且|同时|以及)")
_NUMBER_PATTERN = r"-?\d+(?:\.\d+)?"
_NUMERIC_REQUIREMENT_PATTERNS = (
    (
        "maximum_inclusive",
        re.compile(
            rf"(?:不超过|不得超过|至多|最多(?:为|是)?|上限(?:为|是)?|"
            rf"最大(?:值)?(?:为|是)?|小于(?:或)?等于|<=|≤)\s*({_NUMBER_PATTERN})"
        ),
    ),
    (
        "maximum_inclusive",
        re.compile(rf"({_NUMBER_PATTERN})\s*(?:以内|之内|及以下|或以下)"),
    ),
    (
        "maximum_exclusive",
        re.compile(rf"(?:小于|低于|少于|<)\s*({_NUMBER_PATTERN})"),
    ),
    (
        "minimum_inclusive",
        re.compile(
            rf"(?:不少于|不得少于|至少(?:为|是)?|下限(?:为|是)?|"
            rf"最小(?:值)?(?:为|是)?|大于(?:或)?等于|>=|≥)\s*({_NUMBER_PATTERN})"
        ),
    ),
    (
        "minimum_inclusive",
        re.compile(rf"({_NUMBER_PATTERN})\s*(?:以上|及以上|或以上)"),
    ),
    (
        "minimum_exclusive",
        re.compile(rf"(?:大于|高于|多于|>)\s*({_NUMBER_PATTERN})"),
    ),
)
_MARKDOWN_EXPECTED_PATTERN = re.compile(r"(?im)^- Expected:\s*(.+?)\s*$")
_MARKDOWN_ACCEPTANCE_SECTION_PATTERN = re.compile(
    r"(?ims)^## Acceptance Criteria\s*(.*?)(?=^## |\Z)"
)
_MARKDOWN_CHECKBOX_PATTERN = re.compile(r"(?im)^-\s*\[[ xX]\]\s*(.+?)\s*$")


def _requirement_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _text(value)).casefold()
    return " ".join(text.split())


def _normalized_number(value: str) -> str:
    try:
        number = float(value)
    except ValueError:
        return value
    return str(int(number)) if number.is_integer() else format(number, ".15g")


def _requirement_signature_from_texts(
    expected_behavior: Any,
    acceptance_criteria: Any,
) -> Dict[str, List[str]]:
    texts = []
    expected = _requirement_text(expected_behavior)
    if expected and expected != "unknown":
        texts.append(expected)
    if isinstance(acceptance_criteria, list):
        texts.extend(
            text
            for text in (_requirement_text(item) for item in acceptance_criteria)
            if text and text != "unknown"
        )
    constraints = set()
    qualifiers = set()
    for text in texts:
        clauses = [item.strip() for item in _REQUIREMENT_SPLIT_PATTERN.split(text)]
        for clause in clauses:
            if not clause:
                continue
            clause_constraints = set()
            for kind, pattern in _NUMERIC_REQUIREMENT_PATTERNS:
                clause_constraints.update(
                    f"{kind}:{_normalized_number(match.group(1))}"
                    for match in pattern.finditer(clause)
                )
            if clause_constraints:
                constraints.update(clause_constraints)
                continue
            numbers = re.findall(_NUMBER_PATTERN, clause)
            if numbers:
                constraints.update(
                    f"number:{_normalized_number(number)}" for number in numbers
                )
                continue
            normalized = re.sub(r"[^\w\u3400-\u9fff]+", " ", clause)
            normalized = " ".join(normalized.split())
            if normalized:
                qualifiers.add(normalized)
    return {
        "numeric_constraints": sorted(constraints),
        "qualifiers": sorted(qualifiers),
    }


def _requirement_signature(generation: Mapping[str, Any]) -> Dict[str, List[str]]:
    draft = _mapping(generation.get("draft"))
    problem = _mapping(draft.get("problem"))
    return _requirement_signature_from_texts(
        problem.get("expected_behavior"),
        draft.get("acceptance_criteria"),
    )


def _requirement_signature_from_issue_body(body: str) -> Dict[str, List[str]]:
    expected_match = _MARKDOWN_EXPECTED_PATTERN.search(body)
    acceptance_match = _MARKDOWN_ACCEPTANCE_SECTION_PATTERN.search(body)
    criteria = (
        _MARKDOWN_CHECKBOX_PATTERN.findall(acceptance_match.group(1))
        if acceptance_match
        else []
    )
    return _requirement_signature_from_texts(
        expected_match.group(1) if expected_match else "",
        criteria,
    )


def _issue_fingerprint(
    generation: Mapping[str, Any],
    repository: str,
    *,
    version: str,
    include_requirements: bool,
) -> str:
    draft = _mapping(generation.get("draft"))
    obj = _mapping(draft.get("object"))
    interface = _mapping(draft.get("interface"))
    error = _mapping(draft.get("error"))
    problem = _mapping(draft.get("problem"))
    revision = _mapping(generation.get("revision"))
    revision_parent = _text(revision.get("parent_issue_url"))
    revision_request_sha256 = _text(revision.get("request_sha256"))
    if revision and (
        revision.get("schema_version") != "issue-revision/v1"
        or not ISSUE_URL_PATTERN.fullmatch(revision_parent)
        or not SHA256_PATTERN.fullmatch(revision_request_sha256)
    ):
        raise ValueError("Issue revision metadata is invalid")
    material = {
        "version": version,
        "repository": repository.casefold(),
        "service": _normalized(obj.get("service")),
        "module": _normalized(obj.get("module")),
        "code_object": _normalized(obj.get("code_object")),
        "interface_method": _normalized(interface.get("method")),
        "interface_path": _normalized(interface.get("path_or_topic")),
        "error_code": _normalized(error.get("error_code")),
        "exception_type": _normalized(error.get("exception_type")),
        "error_message": _normalized(error.get("message")),
        "current_behavior": _normalized(problem.get("current_behavior")),
        "revision_parent": revision_parent.casefold(),
        "revision_request_sha256": revision_request_sha256,
    }
    if include_requirements:
        material["requirement_signature"] = _requirement_signature(generation)
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def issue_fingerprint(generation: Mapping[str, Any], repository: str) -> str:
    return _issue_fingerprint(
        generation,
        repository,
        version=FINGERPRINT_VERSION,
        include_requirements=True,
    )


def _legacy_issue_fingerprint(
    generation: Mapping[str, Any], repository: str
) -> str:
    return _issue_fingerprint(
        generation,
        repository,
        version=LEGACY_FINGERPRINT_VERSION,
        include_requirements=False,
    )


def _fingerprint_marker(fingerprint: str, version: str = FINGERPRINT_VERSION) -> str:
    return f"<!-- {version}:{fingerprint} -->"


def render_automated_issue_body(
    generation: Mapping[str, Any], repository: str, policy: RepositoryAutoPublishPolicy
) -> Tuple[str, str]:
    fingerprint = issue_fingerprint(generation, repository)
    body = ai_issue_generator.render_markdown(dict(generation))
    body = body.replace(
        "> AI-generated local draft. Human confirmation is required; automatic publication is disabled.",
        "> AI-generated draft. Publication was authorized by a reviewed deterministic policy; AI did not authorize this Issue.",
        1,
    )
    body = body.replace(
        "- AI implementation allowed: no",
        "- AI implementation permission: separate downstream policy required",
        1,
    )
    revision = _mapping(generation.get("revision"))
    if revision:
        body += (
            "\n\n## Revision lineage\n\n"
            f"- Revision of: {revision['parent_issue_url']}\n"
            "- This revision requires an independent human approval.\n"
        )
    body += (
        "\n\n## Automated routing audit\n\n"
        f"- Resolution policy: `{RESOLUTION_POLICY_VERSION}`\n"
        f"- Publication policy: `{policy.policy_id}`\n"
        f"- Publication policy SHA-256: `{policy.policy_sha256}`\n"
        "- Code modification approval: separate downstream policy required\n\n"
        f"{_fingerprint_marker(fingerprint)}\n"
    )
    if find_sensitive_data(body):
        raise ValueError("rendered GitHub Issue failed sensitive-data validation")
    return body, fingerprint


def match_existing_issues(
    generation: Mapping[str, Any], repository: str, issues: Sequence[Mapping[str, Any]]
) -> Dict[str, Any]:
    fingerprint = issue_fingerprint(generation, repository)
    marker = _fingerprint_marker(fingerprint)
    legacy_marker = _fingerprint_marker(
        _legacy_issue_fingerprint(generation, repository),
        LEGACY_FINGERPRINT_VERSION,
    )
    requirements = _requirement_signature(generation)
    draft = _mapping(generation.get("draft"))
    title = _normalized(draft.get("title"))
    obj = _mapping(draft.get("object"))
    interface = _mapping(draft.get("interface"))
    error = _mapping(draft.get("error"))
    signals = {
        "title": title,
        "code_object": _normalized(obj.get("code_object")),
        "exception_type": _normalized(error.get("exception_type")),
        "interface_path": _normalized(interface.get("path_or_topic")),
        "interface_method": _normalized(interface.get("method")),
    }
    is_revision = bool(_mapping(generation.get("revision")))
    candidates = []
    for issue in issues[:MAX_ISSUES_SCANNED]:
        body = _text(issue.get("body"))
        normalized_body = body.casefold()
        current_exact = marker in body
        legacy_exact = (
            legacy_marker in body
            and _requirement_signature_from_issue_body(body) == requirements
        )
        exact = current_exact or legacy_exact
        score = 100 if exact else 0
        reasons = []
        if current_exact:
            reasons.append("exact deterministic fingerprint")
        elif legacy_exact:
            reasons.append("compatible legacy deterministic fingerprint")
        if not exact and is_revision:
            continue
        if not exact:
            if title and _normalized(issue.get("title")) == title:
                score += 35
                reasons.append("exact normalized title")
            for name, weight in (
                ("code_object", 30),
                ("exception_type", 20),
                ("interface_path", 15),
                ("interface_method", 10),
            ):
                value = signals[name]
                if value and value in normalized_body:
                    score += weight
                    reasons.append(name)
            score = min(score, 100)
        if score < 80:
            continue
        number = issue.get("number")
        url = _text(issue.get("url"))
        state = _text(issue.get("state"))
        if not isinstance(number, int) or not ISSUE_URL_PATTERN.fullmatch(url):
            continue
        candidates.append(
            {
                "number": number,
                "url": url,
                "state": state,
                "score": score,
                "reasons": reasons,
                "exact_fingerprint": exact,
                "fingerprint_version": (
                    FINGERPRINT_VERSION
                    if current_exact
                    else LEGACY_FINGERPRINT_VERSION if legacy_exact else None
                ),
            }
        )
    candidates.sort(key=lambda item: (-item["score"], item["number"]))
    open_candidates = [
        item for item in candidates if item["state"].casefold() == "open"
    ]
    open_exact_candidates = [
        item for item in open_candidates if item["exact_fingerprint"]
    ]
    closed_exact_candidates = [
        item
        for item in candidates
        if item["exact_fingerprint"] and item["state"].casefold() == "closed"
    ]
    if len(open_exact_candidates) == 1:
        status = "existing_issue_candidate"
        selected = open_exact_candidates[0]
    elif len(open_exact_candidates) > 1:
        status = "ambiguous_existing_issues"
        selected = None
    elif len(closed_exact_candidates) == 1:
        status = "closed_issue_candidate"
        selected = closed_exact_candidates[0]
    elif len(closed_exact_candidates) > 1:
        status = "ambiguous_existing_issues"
        selected = None
    elif len(open_candidates) == 1:
        status = "existing_issue_candidate"
        selected = open_candidates[0]
    elif len(open_candidates) > 1:
        status = "ambiguous_existing_issues"
        selected = None
    else:
        status = "new_issue"
        selected = None
    return {
        "status": status,
        "fingerprint": fingerprint,
        "selected": selected,
        "candidates": candidates,
        "issues_scanned": min(len(issues), MAX_ISSUES_SCANNED),
        "raw_issue_bodies_persisted": False,
    }


def automate_repository_issue(
    generation: Mapping[str, Any],
    evidence: Mapping[str, Any],
    scope: RepositorySearchScope,
    search_adapter: RepositorySearchAdapter,
    adapter_name: str,
    policy: RepositoryAutoPublishPolicy,
    issue_client: GitHubIssueClient,
    auto_publish: bool,
    *,
    preselected_repository: str = "",
    routing_mode: str = ROUTING_MODE_PRODUCTION_EVIDENCE,
    input_type: str = "natural_language",
) -> Dict[str, Any]:
    if routing_mode not in ROUTING_MODES:
        raise ValueError("repository routing mode is invalid")
    if input_type not in AUTOMATION_INPUT_TYPES:
        raise ValueError("automation input type is invalid")
    generation_policy = _mapping(generation.get("policy"))
    review = _mapping(generation.get("review"))
    validation = _mapping(generation.get("validation"))
    safety = _mapping(evidence.get("safety"))
    pre_resolution_rules = {
        "generation_state_allowed": generation.get("state")
        in policy.allowed_generation_states,
        "generation_locally_valid": validation.get("valid") is True
        and validation.get("errors") in ([], None),
        "review_safety_passed": review.get("verdict") != "reject"
        and review.get("sensitive_data_detected") is False,
        "evidence_ai_allowed": safety.get("ai_allowed") is True,
        "security_review_not_required": safety.get("security_review_required") is not True,
        "ai_did_not_authorize_actions": generation_policy.get(
            "human_confirmation_required"
        )
        is True
        and generation_policy.get("publication_allowed") is False
        and generation_policy.get("implementation_allowed") is False,
        "adapter_allowed": adapter_name in policy.allowed_adapters,
    }
    enabled_repositories = {
        entry.repository: entry for entry in scope.enabled_repositories
    }
    if preselected_repository:
        if (
            preselected_repository not in enabled_repositories
            or len(enabled_repositories) != 1
        ):
            raise ValueError(
                "preselected repository requires the exact single enabled scope"
            )
        if (
            routing_mode == ROUTING_MODE_PRODUCTION_EVIDENCE
            and input_type == "sanitized_evidence"
        ):
            raise ValueError(
                "production evidence routing forbids single-repository log binding"
            )
    if preselected_repository and all(pre_resolution_rules.values()):
        digest = _text(generation.get("input_sha256"))
        selected_entry = enabled_repositories[preselected_repository]
        resolution = {
            "schema_version": RESOLUTION_SCHEMA_VERSION,
            "draft_ref": f"draft_ref:{digest[:16]}",
            "input_sha256": digest,
            "scope_id": scope.scope_id,
            "status": "resolved",
            "selected_repository": preselected_repository,
            "decision": {
                "policy_version": RESOLUTION_POLICY_VERSION,
                "minimum_resolved_score": MINIMUM_RESOLVED_SCORE,
                "minimum_margin": MINIMUM_MARGIN,
                "minimum_strong_families": MINIMUM_STRONG_FAMILIES,
                "top_score": 100,
                "runner_up_score": 0,
                "margin": 100,
                "reasons": [
                    "repository is bound by the operator-approved single-repository scope"
                ],
            },
            "candidates": [
                {
                    "repository": preselected_repository,
                    "score": 100,
                    "strong_families": MINIMUM_STRONG_FAMILIES,
                    "conflicts": [],
                    "evidence": [
                        {
                            "family": "operator_scope",
                            "matched_term": preselected_repository,
                            "source_paths": [],
                            "ref": selected_entry.default_branch,
                            "hit_count": 1,
                            "evidence_ref": (
                                "scope_ref:"
                                + hashlib.sha256(
                                    (
                                        f"{scope.scope_id}\n"
                                        f"{preselected_repository}"
                                    ).encode("utf-8")
                                ).hexdigest()[:32]
                            ),
                        }
                    ],
                }
            ],
            "search_audit": {
                "provider": "operator_scope",
                "repositories_enabled": 1,
                "queries_executed": 0,
                "candidate_repositories_verified": 1,
                "raw_source_snippets_persisted": False,
            },
        }
    elif all(pre_resolution_rules.values()):
        resolution = resolve_repository(generation, scope, search_adapter)
    else:
        digest = _text(generation.get("input_sha256"))
        resolution = {
            "schema_version": RESOLUTION_SCHEMA_VERSION,
            "draft_ref": f"draft_ref:{digest[:16]}",
            "input_sha256": digest,
            "scope_id": scope.scope_id,
            "status": "blocked",
            "selected_repository": None,
            "decision": {
                "policy_version": RESOLUTION_POLICY_VERSION,
                "minimum_resolved_score": MINIMUM_RESOLVED_SCORE,
                "minimum_margin": MINIMUM_MARGIN,
                "minimum_strong_families": MINIMUM_STRONG_FAMILIES,
                "top_score": None,
                "runner_up_score": None,
                "margin": None,
                "reasons": ["pre-resolution automatic publication gate failed closed"],
            },
            "candidates": [],
            "search_audit": {
                "provider": "github",
                "repositories_enabled": len(scope.enabled_repositories),
                "queries_executed": 0,
                "candidate_repositories_verified": 0,
                "raw_source_snippets_persisted": False,
            },
        }
    output: Dict[str, Any] = {
        "schema_version": AUTOMATION_SCHEMA_VERSION,
        "policy": {
            "policy_id": policy.policy_id,
            "policy_sha256": policy.policy_sha256,
            "scope_id": policy.scope_id,
            "scope_sha256": policy.scope_sha256,
            "routing_mode": routing_mode,
            "input_type": input_type,
        },
        "resolution": resolution,
        "issue_match": {
            "status": "not_resolved",
            "candidates": [],
            "issues_scanned": 0,
            "raw_issue_bodies_persisted": False,
        },
        "approval": {"approved": False, "rules": {}},
        "publication": {
            "requested": auto_publish,
            "status": "blocked",
            "repository": None,
            "issue_url": None,
            "issue_number": None,
        },
    }
    rules = {
        **pre_resolution_rules,
        "resolution_policy_matches": _mapping(resolution.get("decision")).get(
            "policy_version"
        )
        == RESOLUTION_POLICY_VERSION,
        "repository_uniquely_resolved": resolution.get("status") == "resolved",
    }
    output["approval"]["rules"] = rules
    if not all(rules.values()):
        return output

    repository = _text(resolution.get("selected_repository"))
    try:
        issues = issue_client.list_issues(repository, MAX_ISSUES_SCANNED)
        issue_match = match_existing_issues(generation, repository, issues)
    except ValueError:
        output["issue_match"]["status"] = "blocked"
        return output
    output["issue_match"] = issue_match
    output["publication"]["repository"] = repository
    if issue_match["status"] == "ambiguous_existing_issues":
        return output
    if issue_match["status"] == "existing_issue_candidate":
        selected = issue_match["selected"]
        output["approval"]["approved"] = True
        output["publication"].update(
            {
                "status": "deduplicated",
                "issue_url": selected["url"],
                "issue_number": selected["number"],
            }
        )
        return output
    if issue_match["status"] == "closed_issue_candidate":
        selected = issue_match["selected"]
        output["approval"]["approved"] = True
        output["publication"].update(
            {
                "status": "already_completed",
                "issue_url": selected["url"],
                "issue_number": selected["number"],
            }
        )
        return output

    body, _ = render_automated_issue_body(generation, repository, policy)
    output["approval"]["approved"] = True
    output["publication"]["status"] = "approved_not_published"
    if not auto_publish:
        return output
    try:
        issue_url = issue_client.create_issue(
            repository,
            _text(_mapping(generation.get("draft")).get("title")),
            body,
        )
    except ValueError:
        output["publication"]["status"] = "blocked"
        return output
    match = ISSUE_URL_PATTERN.fullmatch(issue_url)
    output["publication"].update(
        {
            "status": "created",
            "issue_url": issue_url,
            "issue_number": int(match.group(1)),
        }
    )
    return output
