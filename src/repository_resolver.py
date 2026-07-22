"""Resolve a validated IssueDraft to one authorized GitHub repository."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Protocol, Sequence, Tuple
from urllib.parse import quote

from src.ai_issue_generator import DRAFT_SCHEMA_VERSION, RESULT_SCHEMA_VERSION
from src.issue_draft import _atomic_write_json
from src.issue_intake import find_sensitive_data


SCOPE_SCHEMA_VERSION = "repository-search-scope/v1"
RESOLUTION_SCHEMA_VERSION = "repository-resolution/v1"
POLICY_VERSION = "repository-resolution-policy/v1"
MINIMUM_RESOLVED_SCORE = 70
MINIMUM_MARGIN = 20
MINIMUM_STRONG_FAMILIES = 2
AMBIGUOUS_SCORE = 40
MAX_SCOPE_BYTES = 256_000
MAX_INPUT_BYTES = 1_000_000
REPOSITORY_PATTERN = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}"
)
SCOPE_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
BRANCH_PATTERN = re.compile(r"[^\s~^:?*\[\]\\]{1,255}")
LABEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
BLOB_SHA_PATTERN = re.compile(r"[0-9a-f]{40,64}")
FQCN_PATTERN = re.compile(
    r"\b(?:[a-z_][A-Za-z0-9_$]*\.){2,}[A-Z][A-Za-z0-9_$]*\b"
)
CODE_METHOD_PATTERN = re.compile(r"[a-z_$][A-Za-z0-9_$]{1,127}")
HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"}
ALLOWED_INPUT_STATES = {"ready_for_human_review", "needs_human_context"}
FAMILY_ORDER = {"qualified_class": 0, "class_method": 1}
MAX_PROBE_REPOSITORY_SIZE_KB = 1_024
MAX_PROBE_TREE_ENTRIES = 500
MAX_PROBE_FILE_BYTES = 256_000


class RepositorySearchError(RuntimeError):
    """A fail-closed, safely reportable repository search failure."""


def _mapping(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _require_exact_keys(
    value: Mapping[str, Any], required: Sequence[str], optional: Sequence[str], field: str
) -> None:
    missing = sorted(set(required) - set(value))
    extra = sorted(set(value) - set(required) - set(optional))
    if missing:
        raise ValueError(f"{field} is missing required fields: {', '.join(missing)}")
    if extra:
        raise ValueError(f"{field} contains unsupported fields: {', '.join(extra)}")


@dataclass(frozen=True)
class RepositoryEntry:
    repository: str
    enabled: bool
    default_branch: str
    labels: Tuple[str, ...]


@dataclass(frozen=True)
class SearchLimits:
    max_queries: int
    max_candidate_repositories: int
    max_hits_per_query: int


@dataclass(frozen=True)
class RepositorySearchScope:
    scope_id: str
    repositories: Tuple[RepositoryEntry, ...]
    limits: SearchLimits

    @property
    def enabled_repositories(self) -> Tuple[RepositoryEntry, ...]:
        return tuple(entry for entry in self.repositories if entry.enabled)


@dataclass(frozen=True)
class SearchPlan:
    family: str
    matched_term: str
    search_terms: Tuple[str, ...]
    source_paths: Tuple[str, ...]
    weight: int
    strong: bool


@dataclass(frozen=True)
class SearchHits:
    """Opaque in-memory hit keys; keys must never be serialized."""

    keys: FrozenSet[str]


class RepositorySearchAdapter(Protocol):
    def search(self, repository: str, term: str, max_hits: int) -> SearchHits:
        ...


class GitHubCLICodeSearchAdapter:
    """Read-only GitHub code search using an existing authenticated gh session."""

    def __init__(self, timeout_seconds: float = 30.0):
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("GitHub search timeout must be between 1 and 120 seconds")
        self.timeout_seconds = timeout_seconds

    def search(self, repository: str, term: str, max_hits: int) -> SearchHits:
        if shutil.which("gh") is None:
            raise RepositorySearchError("GitHub CLI is unavailable")
        if not REPOSITORY_PATTERN.fullmatch(repository):
            raise RepositorySearchError("authorized repository name is invalid")
        if not 1 <= max_hits <= 100:
            raise RepositorySearchError("GitHub search hit limit is invalid")
        _validate_search_term(term)
        query = f'"{term}" repo:{repository}'
        command = [
            "gh",
            "api",
            "--method",
            "GET",
            "search/code",
            "-f",
            f"q={query}",
            "-f",
            f"per_page={max_hits}",
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
            raise RepositorySearchError("GitHub code search could not be completed") from exc
        if completed.returncode != 0:
            raise RepositorySearchError("GitHub code search failed closed")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RepositorySearchError("GitHub code search returned invalid JSON") from exc
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise RepositorySearchError("GitHub code search returned an invalid result")
        keys = set()
        for item in items[:max_hits]:
            if not isinstance(item, dict):
                continue
            path = _text(item.get("path"))
            if path:
                keys.add(path)
        return SearchHits(frozenset(keys))


class GitHubCLIRepositoryTreeProbeAdapter:
    """Small-repository probe that avoids asynchronous GitHub search indexing.

    This adapter is intentionally not the production default. It fails closed
    for repositories above fixed size and tree-entry limits, downloads only
    Java files that match a grounded qualified-class path, and keeps their
    contents in memory.
    """

    def __init__(
        self,
        repositories: Sequence[RepositoryEntry],
        timeout_seconds: float = 30.0,
    ):
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("GitHub search timeout must be between 1 and 120 seconds")
        self.timeout_seconds = timeout_seconds
        self._branches = {entry.repository: entry.default_branch for entry in repositories}
        self._trees: Dict[str, Tuple[Dict[str, Any], ...]] = {}
        self._contents: Dict[Tuple[str, str], str] = {}
        self._qualified_paths: Dict[str, FrozenSet[str]] = {}

    def _api_json(
        self, endpoint: str, *, allow_empty_repository: bool = False
    ) -> Dict[str, Any]:
        if shutil.which("gh") is None:
            raise RepositorySearchError("GitHub CLI is unavailable")
        try:
            command = ["gh", "api"]
            if allow_empty_repository:
                command.append("--include")
            command.append(endpoint)
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RepositorySearchError("GitHub repository probe could not be completed") from exc
        response_text = completed.stdout
        if allow_empty_repository:
            first_line = response_text.splitlines()[0] if response_text else ""
            status_match = re.fullmatch(r"HTTP/\S+ (\d{3})(?: .*)?", first_line)
            status = int(status_match.group(1)) if status_match else 0
            if completed.returncode != 0:
                if status == 409:
                    return {"_empty_repository": True}
                raise RepositorySearchError("GitHub repository probe failed closed")
            sections = re.split(r"\n\s*\n", response_text, maxsplit=1)
            if len(sections) != 2:
                raise RepositorySearchError("GitHub repository probe returned invalid headers")
            response_text = sections[1]
        elif completed.returncode != 0:
            raise RepositorySearchError("GitHub repository probe failed closed")
        try:
            payload = json.loads(response_text)
        except json.JSONDecodeError as exc:
            raise RepositorySearchError("GitHub repository probe returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RepositorySearchError("GitHub repository probe returned an invalid result")
        return payload

    def _tree(self, repository: str) -> Tuple[Dict[str, Any], ...]:
        if repository in self._trees:
            return self._trees[repository]
        if repository not in self._branches:
            raise RepositorySearchError("repository probe is outside the authorized scope")
        metadata = self._api_json(f"repos/{repository}")
        if metadata.get("archived") is True:
            raise RepositorySearchError("repository probe does not search archived repositories")
        size = metadata.get("size")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or size > MAX_PROBE_REPOSITORY_SIZE_KB
        ):
            raise RepositorySearchError("repository exceeds the small-probe size limit")
        branch = self._branches[repository]
        tree_payload = self._api_json(
            f"repos/{repository}/git/trees/{quote(branch, safe='')}?recursive=1",
            allow_empty_repository=size == 0,
        )
        if tree_payload.get("_empty_repository") is True:
            self._trees[repository] = ()
            return ()
        if tree_payload.get("truncated") is not False:
            raise RepositorySearchError("repository tree probe was truncated")
        raw_tree = tree_payload.get("tree")
        if not isinstance(raw_tree, list) or len(raw_tree) > MAX_PROBE_TREE_ENTRIES:
            raise RepositorySearchError("repository exceeds the small-probe tree limit")
        tree = []
        for item in raw_tree:
            if not isinstance(item, dict) or item.get("type") != "blob":
                continue
            path = _text(item.get("path"))
            sha = _text(item.get("sha"))
            blob_size = item.get("size")
            if (
                path.endswith(".java")
                and BLOB_SHA_PATTERN.fullmatch(sha)
                and isinstance(blob_size, int)
                and not isinstance(blob_size, bool)
            ):
                tree.append({"path": path, "sha": sha, "size": blob_size})
        self._trees[repository] = tuple(tree)
        return self._trees[repository]

    def _content(self, repository: str, item: Mapping[str, Any]) -> str:
        path = _text(item.get("path"))
        key = (repository, path)
        if key in self._contents:
            return self._contents[key]
        size = item.get("size")
        if not isinstance(size, int) or not 0 <= size <= MAX_PROBE_FILE_BYTES:
            raise RepositorySearchError("repository probe file exceeds the size limit")
        payload = self._api_json(f"repos/{repository}/git/blobs/{item['sha']}")
        if payload.get("encoding") != "base64" or not isinstance(payload.get("content"), str):
            raise RepositorySearchError("repository probe blob encoding is invalid")
        try:
            encoded = "".join(payload["content"].split())
            raw = base64.b64decode(encoded, validate=True)
            content = raw.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise RepositorySearchError("repository probe file is not valid UTF-8") from exc
        if len(raw) > MAX_PROBE_FILE_BYTES:
            raise RepositorySearchError("repository probe file exceeds the size limit")
        self._contents[key] = content
        return content

    def search(self, repository: str, term: str, max_hits: int) -> SearchHits:
        if not REPOSITORY_PATTERN.fullmatch(repository):
            raise RepositorySearchError("authorized repository name is invalid")
        if not 1 <= max_hits <= 100:
            raise RepositorySearchError("GitHub search hit limit is invalid")
        _validate_search_term(term)
        tree = self._tree(repository)
        if not tree:
            return SearchHits(frozenset())
        if FQCN_PATTERN.fullmatch(term):
            package, class_name = term.rsplit(".", 1)
            suffix = term.replace(".", "/") + ".java"
            matches = set()
            for item in tree:
                path = _text(item.get("path"))
                if not path.endswith(suffix):
                    continue
                content = self._content(repository, item)
                if (
                    f"package {package};" in content
                    and re.search(rf"\bclass\s+{re.escape(class_name)}\b", content)
                ) or term in content:
                    matches.add(path)
            bounded = frozenset(sorted(matches)[:max_hits])
            self._qualified_paths[repository] = bounded
            return SearchHits(bounded)
        if term and term[0].isupper():
            matches = {
                _text(item.get("path"))
                for item in tree
                if _text(item.get("path")).endswith(f"/{term}.java")
            }
            return SearchHits(frozenset(sorted(matches)[:max_hits]))
        matches = set()
        qualified_paths = self._qualified_paths.get(repository, frozenset())
        for item in tree:
            path = _text(item.get("path"))
            if path not in qualified_paths:
                continue
            content = self._content(repository, item)
            if re.search(rf"\b{re.escape(term)}\s*\(", content):
                matches.add(path)
        return SearchHits(frozenset(sorted(matches)[:max_hits]))


def _validate_search_term(term: str) -> None:
    if not term or len(term) > 256:
        raise RepositorySearchError("planned search term has an invalid length")
    if any(character in term for character in ('"', "\n", "\r", "\x00")):
        raise RepositorySearchError("planned search term contains unsupported characters")
    if find_sensitive_data(term):
        raise RepositorySearchError("planned search term failed sensitive-data validation")


def _load_json_object(path: Path, maximum_bytes: int, label: str) -> Dict[str, Any]:
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symbolic link")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"unable to read {label}") from exc
    if not raw or len(raw) > maximum_bytes:
        raise ValueError(f"{label} size is invalid")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must contain valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def load_search_scope(path: Path) -> RepositorySearchScope:
    payload = _load_json_object(path, MAX_SCOPE_BYTES, "repository search scope")
    _require_exact_keys(
        payload,
        ("schema_version", "scope_id", "provider", "repositories", "limits"),
        (),
        "repository search scope",
    )
    if payload.get("schema_version") != SCOPE_SCHEMA_VERSION:
        raise ValueError(f"repository search scope must use {SCOPE_SCHEMA_VERSION}")
    if payload.get("provider") != "github":
        raise ValueError("repository search scope provider must be github")
    scope_id = _text(payload.get("scope_id"))
    if not SCOPE_ID_PATTERN.fullmatch(scope_id):
        raise ValueError("scope_id has an invalid format")

    raw_repositories = payload.get("repositories")
    if not isinstance(raw_repositories, list) or not 1 <= len(raw_repositories) <= 500:
        raise ValueError("repositories must contain between 1 and 500 entries")
    repositories: List[RepositoryEntry] = []
    seen_repositories = set()
    for index, raw_entry in enumerate(raw_repositories):
        if not isinstance(raw_entry, dict):
            raise ValueError(f"repositories[{index}] must be an object")
        _require_exact_keys(
            raw_entry,
            ("repository", "enabled"),
            ("default_branch", "labels"),
            f"repositories[{index}]",
        )
        repository = _text(raw_entry.get("repository"))
        if not REPOSITORY_PATTERN.fullmatch(repository):
            raise ValueError(f"repositories[{index}].repository must use owner/name format")
        normalized_repository = repository.casefold()
        if normalized_repository in seen_repositories:
            raise ValueError(f"duplicate repository entry: {repository}")
        seen_repositories.add(normalized_repository)
        enabled = raw_entry.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError(f"repositories[{index}].enabled must be a boolean")
        default_branch = _text(raw_entry.get("default_branch")) or "main"
        if not BRANCH_PATTERN.fullmatch(default_branch) or any(
            invalid
            for invalid in (
                default_branch.startswith((".", "/")),
                default_branch.endswith((".", "/", ".lock")),
                ".." in default_branch,
                "@{" in default_branch,
                "//" in default_branch,
            )
        ):
            raise ValueError(f"repositories[{index}].default_branch is invalid")
        raw_labels = raw_entry.get("labels", [])
        if not isinstance(raw_labels, list) or len(raw_labels) > 20:
            raise ValueError(f"repositories[{index}].labels must be a bounded array")
        labels = tuple(_text(label) for label in raw_labels)
        if any(not LABEL_PATTERN.fullmatch(label) for label in labels):
            raise ValueError(f"repositories[{index}].labels contains an invalid label")
        if len(labels) != len(set(labels)):
            raise ValueError(f"repositories[{index}].labels contains duplicates")
        repositories.append(RepositoryEntry(repository, enabled, default_branch, labels))
    if not any(entry.enabled for entry in repositories):
        raise ValueError("repository search scope must enable at least one repository")

    raw_limits = payload.get("limits")
    if not isinstance(raw_limits, dict):
        raise ValueError("limits must be an object")
    _require_exact_keys(
        raw_limits,
        ("max_queries", "max_candidate_repositories", "max_hits_per_query"),
        (),
        "limits",
    )
    limits = []
    for name, maximum in (
        ("max_queries", 50),
        ("max_candidate_repositories", 50),
        ("max_hits_per_query", 100),
    ):
        value = raw_limits.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
            raise ValueError(f"limits.{name} must be between 1 and {maximum}")
        limits.append(value)
    return RepositorySearchScope(
        scope_id=scope_id,
        repositories=tuple(repositories),
        limits=SearchLimits(*limits),
    )


def load_issue_generation(path: Path) -> Dict[str, Any]:
    result = _load_json_object(path, MAX_INPUT_BYTES, "AI Issue result")
    if result.get("schema_version") != RESULT_SCHEMA_VERSION:
        raise ValueError(f"AI Issue result must use {RESULT_SCHEMA_VERSION}")
    if result.get("state") not in ALLOWED_INPUT_STATES:
        raise ValueError("AI Issue result is not eligible for repository resolution")
    digest = _text(result.get("input_sha256"))
    if not SHA256_PATTERN.fullmatch(digest):
        raise ValueError("AI Issue result input_sha256 is invalid")
    validation = _mapping(result.get("validation"))
    if validation.get("valid") is not True or validation.get("errors") not in ([], None):
        raise ValueError("AI Issue result did not pass local validation")
    review = _mapping(result.get("review"))
    if review.get("verdict") == "reject" or review.get("sensitive_data_detected") is not False:
        raise ValueError("AI Issue review did not pass the safety gate")
    policy = _mapping(result.get("policy"))
    if (
        policy.get("human_confirmation_required") is not True
        or policy.get("publication_allowed") is not False
        or policy.get("implementation_allowed") is not False
    ):
        raise ValueError("AI Issue result contains an unsafe authorization policy")
    draft = _mapping(result.get("draft"))
    if draft.get("schema_version") != DRAFT_SCHEMA_VERSION:
        raise ValueError(f"AI Issue draft must use {DRAFT_SCHEMA_VERSION}")
    if find_sensitive_data(draft):
        raise ValueError("AI Issue draft failed sensitive-data validation")
    evidence = draft.get("evidence")
    if not isinstance(evidence, list):
        raise ValueError("AI Issue draft evidence must be an array")
    _evidence_map(draft)
    return result


def _evidence_map(draft: Mapping[str, Any]) -> Dict[str, Tuple[str, ...]]:
    mapped: Dict[str, Tuple[str, ...]] = {}
    for item in draft.get("evidence", []):
        if not isinstance(item, dict):
            raise ValueError("AI Issue draft evidence entries must be objects")
        claim_path = _text(item.get("claim_path"))
        raw_paths = item.get("source_paths")
        if not claim_path.startswith("$.") or not isinstance(raw_paths, list) or not raw_paths:
            raise ValueError("AI Issue draft contains an invalid evidence mapping")
        paths = tuple(_text(path) for path in raw_paths)
        if any(not path.startswith("$.") for path in paths) or len(paths) != len(set(paths)):
            raise ValueError("AI Issue draft contains invalid source evidence paths")
        if claim_path in mapped:
            raise ValueError(f"AI Issue draft contains duplicate evidence for {claim_path}")
        mapped[claim_path] = paths
    return mapped


def plan_search_terms(result: Mapping[str, Any]) -> Tuple[SearchPlan, ...]:
    draft = _mapping(result.get("draft"))
    evidence = _evidence_map(draft)
    obj = _mapping(draft.get("object"))
    interface = _mapping(draft.get("interface"))
    code_object = _text(obj.get("code_object"))
    code_sources = evidence.get("$.object.code_object", ())
    qualified_classes = sorted(set(FQCN_PATTERN.findall(code_object))) if code_sources else []
    plans: List[SearchPlan] = []
    for qualified_class in qualified_classes[:3]:
        _validate_search_term(qualified_class)
        plans.append(
            SearchPlan(
                family="qualified_class",
                matched_term=qualified_class,
                search_terms=(qualified_class,),
                source_paths=code_sources,
                weight=40,
                strong=True,
            )
        )

    method = _text(interface.get("method"))
    method_sources = evidence.get("$.interface.method", ())
    if (
        qualified_classes
        and method_sources
        and method.upper() not in HTTP_METHODS
        and CODE_METHOD_PATTERN.fullmatch(method)
    ):
        class_name = qualified_classes[0].rsplit(".", 1)[-1]
        _validate_search_term(class_name)
        _validate_search_term(method)
        plans.append(
            SearchPlan(
                family="class_method",
                matched_term=f"{class_name}.{method}",
                search_terms=(class_name, method),
                source_paths=tuple(dict.fromkeys((*code_sources, *method_sources))),
                weight=35,
                strong=True,
            )
        )
    return tuple(plans)


def _evidence_reference(
    repository: str, plan: SearchPlan, matching_keys: FrozenSet[str]
) -> str:
    material = "\n".join(
        (repository, plan.family, plan.matched_term, *sorted(matching_keys))
    )
    return "search_ref:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _decision(
    top_score: Optional[int],
    runner_up_score: Optional[int],
    margin: Optional[int],
    reasons: Sequence[str],
) -> Dict[str, Any]:
    return {
        "policy_version": POLICY_VERSION,
        "minimum_resolved_score": MINIMUM_RESOLVED_SCORE,
        "minimum_margin": MINIMUM_MARGIN,
        "minimum_strong_families": MINIMUM_STRONG_FAMILIES,
        "top_score": top_score,
        "runner_up_score": runner_up_score,
        "margin": margin,
        "reasons": list(reasons),
    }


def _base_result(
    result: Mapping[str, Any], scope: RepositorySearchScope, status: str
) -> Dict[str, Any]:
    digest = _text(result.get("input_sha256"))
    return {
        "schema_version": RESOLUTION_SCHEMA_VERSION,
        "draft_ref": f"draft_ref:{digest[:16]}",
        "input_sha256": digest,
        "scope_id": scope.scope_id,
        "status": status,
        "selected_repository": None,
    }


def _blocked_result(
    result: Mapping[str, Any],
    scope: RepositorySearchScope,
    queries_executed: int,
    reason: str,
) -> Dict[str, Any]:
    output = _base_result(result, scope, "blocked")
    output.update(
        {
            "decision": _decision(None, None, None, [reason]),
            "candidates": [],
            "search_audit": {
                "provider": "github",
                "repositories_enabled": len(scope.enabled_repositories),
                "queries_executed": queries_executed,
                "candidate_repositories_verified": 0,
                "raw_source_snippets_persisted": False,
            },
        }
    )
    validate_resolution_result(output, scope)
    return output


def resolve_repository(
    result: Mapping[str, Any],
    scope: RepositorySearchScope,
    adapter: RepositorySearchAdapter,
) -> Dict[str, Any]:
    try:
        plans = plan_search_terms(result)
    except (RepositorySearchError, ValueError):
        return _blocked_result(
            result,
            scope,
            0,
            "IssueDraft search evidence failed closed",
        )
    enabled = scope.enabled_repositories
    required_queries = len(
        {
            (entry.repository, term)
            for entry in enabled
            for plan in plans
            for term in plan.search_terms
        }
    )
    if required_queries > scope.limits.max_queries:
        return _blocked_result(
            result,
            scope,
            0,
            "planned repository search exceeds the reviewed query budget",
        )

    cache: Dict[Tuple[str, str], SearchHits] = {}
    queries_executed = 0
    candidate_rows: List[Dict[str, Any]] = []
    try:
        for entry in enabled:
            observations = []
            for plan in plans:
                term_hits = []
                for term in plan.search_terms:
                    cache_key = (entry.repository, term)
                    if cache_key not in cache:
                        queries_executed += 1
                        cache[cache_key] = adapter.search(
                            entry.repository,
                            term,
                            scope.limits.max_hits_per_query,
                        )
                    term_hits.append(cache[cache_key].keys)
                matching_keys = (
                    frozenset.intersection(*term_hits) if term_hits else frozenset()
                )
                if matching_keys:
                    observations.append((plan, matching_keys))
            if not observations:
                continue
            evidence_rows = []
            score = 0
            used_source_paths = set()
            scored_families = set()
            strong_families = 0
            for plan, matching_keys in sorted(
                observations, key=lambda item: FAMILY_ORDER[item[0].family]
            ):
                source_paths = set(plan.source_paths)
                if plan.family not in scored_families:
                    score = min(100, score + plan.weight)
                    scored_families.add(plan.family)
                    if plan.strong and source_paths - used_source_paths:
                        strong_families += 1
                        used_source_paths.update(source_paths)
                evidence_rows.append(
                    {
                        "family": plan.family,
                        "matched_term": plan.matched_term,
                        "source_paths": list(plan.source_paths),
                        "ref": entry.default_branch,
                        "hit_count": min(len(matching_keys), scope.limits.max_hits_per_query),
                        "evidence_ref": _evidence_reference(
                            entry.repository, plan, matching_keys
                        ),
                    }
                )
            candidate_rows.append(
                {
                    "repository": entry.repository,
                    "score": score,
                    "strong_families": strong_families,
                    "conflicts": [],
                    "evidence": evidence_rows,
                }
            )
    except RepositorySearchError:
        return _blocked_result(
            result,
            scope,
            queries_executed,
            "authorized repository search failed closed",
        )

    candidate_rows.sort(key=lambda row: (-row["score"], row["repository"].casefold()))
    verified_count = len(candidate_rows)
    top_score = candidate_rows[0]["score"] if candidate_rows else 0
    runner_up_score = candidate_rows[1]["score"] if len(candidate_rows) > 1 else 0
    margin = top_score - runner_up_score
    top = candidate_rows[0] if candidate_rows else None
    if (
        top is not None
        and top_score >= MINIMUM_RESOLVED_SCORE
        and top["strong_families"] >= MINIMUM_STRONG_FAMILIES
        and margin >= MINIMUM_MARGIN
        and not top["conflicts"]
    ):
        status = "resolved"
        reasons = [
            "one repository passed every resolved threshold",
            "the top repository has independent strong evidence families",
        ]
    elif top_score >= AMBIGUOUS_SCORE:
        status = "ambiguous"
        reasons = ["repository evidence exists but resolved thresholds were not met"]
    else:
        status = "unknown"
        reasons = [
            "no authorized repository reached the minimum candidate score"
            if plans
            else "the IssueDraft contains no supported evidence-grounded code identifiers"
        ]
    output = _base_result(result, scope, status)
    if status == "resolved":
        output["selected_repository"] = top["repository"]
    output_candidates = candidate_rows[: scope.limits.max_candidate_repositories]
    output.update(
        {
            "decision": _decision(top_score, runner_up_score, margin, reasons),
            "candidates": output_candidates,
            "search_audit": {
                "provider": "github",
                "repositories_enabled": len(enabled),
                "queries_executed": queries_executed,
                "candidate_repositories_verified": verified_count,
                "raw_source_snippets_persisted": False,
            },
        }
    )
    validate_resolution_result(output, scope)
    return output


def validate_resolution_result(
    result: Mapping[str, Any], scope: RepositorySearchScope
) -> None:
    if result.get("schema_version") != RESOLUTION_SCHEMA_VERSION:
        raise ValueError("repository resolution schema_version is invalid")
    status = result.get("status")
    if status not in {"resolved", "ambiguous", "unknown", "blocked"}:
        raise ValueError("repository resolution status is invalid")
    selected = result.get("selected_repository")
    if status == "resolved":
        if not isinstance(selected, str):
            raise ValueError("resolved result must select a repository")
    elif selected is not None:
        raise ValueError("unresolved result must not select a repository")
    candidates = result.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("repository resolution candidates must be an array")
    repositories = [
        candidate.get("repository")
        for candidate in candidates
        if isinstance(candidate, dict)
    ]
    if len(repositories) != len(candidates) or len(repositories) != len(set(repositories)):
        raise ValueError("repository resolution candidates are invalid or duplicated")
    enabled_names = {entry.repository for entry in scope.enabled_repositories}
    if not set(repositories) <= enabled_names:
        raise ValueError("repository resolution contains a repository outside the scope")
    scores = [candidate.get("score") for candidate in candidates]
    if any(not isinstance(score, int) or not 0 <= score <= 100 for score in scores):
        raise ValueError("repository resolution contains an invalid score")
    if scores != sorted(scores, reverse=True):
        raise ValueError("repository resolution candidates are not score ordered")
    if status == "resolved" and (not candidates or selected != repositories[0]):
        raise ValueError("resolved repository must be the top candidate")
    audit = _mapping(result.get("search_audit"))
    if audit.get("raw_source_snippets_persisted") is not False:
        raise ValueError("repository resolution must not persist raw source snippets")
    queries = audit.get("queries_executed")
    if not isinstance(queries, int) or not 0 <= queries <= scope.limits.max_queries:
        raise ValueError("repository resolution query audit exceeds the scope budget")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resolve one validated AI Issue draft to an authorized GitHub repository."
    )
    parser.add_argument("input", type=Path, help="Validated ai-issue-generation/v1 JSON.")
    parser.add_argument("--scope", type=Path, required=True, help="Reviewed search scope JSON.")
    parser.add_argument("--output", type=Path, required=True, help="Local resolution JSON.")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--adapter",
        choices=("github-code-search", "github-tree-probe"),
        default="github-code-search",
        help="Use code search by default; tree probe is limited to small synthetic tests.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.output.exists():
        print("error: output already exists", file=sys.stderr)
        return 2
    try:
        issue_result = load_issue_generation(args.input)
        scope = load_search_scope(args.scope)
        adapter: RepositorySearchAdapter
        if args.adapter == "github-tree-probe":
            adapter = GitHubCLIRepositoryTreeProbeAdapter(
                scope.enabled_repositories, args.timeout
            )
        else:
            adapter = GitHubCLICodeSearchAdapter(args.timeout)
        resolution = resolve_repository(
            issue_result,
            scope,
            adapter,
        )
        _atomic_write_json(args.output, resolution)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(args.output)
    return 4 if resolution["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
