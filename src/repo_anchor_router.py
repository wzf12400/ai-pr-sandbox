"""Evidence-based repository routing via GitHub code search.

Zero per-project rules: anchors (request-path segments, camelCase identifiers,
stack-frame class names, k8s service names) are extracted from a sanitized
incident and searched against the authorized repositories' code. The repo
whose code contains an anchor owns the incident. Search results are cached
locally so repeated incidents cost no API calls.

Fail-open design: any network/rate-limit/parse problem returns None, and the
caller falls back to the control plane's keyword matcher.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

SCOPE_PATH = Path("control-plane/config/repository-search-scope.json")
CACHE_PATH = Path(".issue-entry-state/log-routing-cache.json")
CACHE_TTL_SECONDS = 7 * 24 * 3600
MAX_ANCHORS = 4
MAX_QUERIES = 4
HTTP_TIMEOUT_SECONDS = 15
USER_AGENT = "ai-pr-sandbox-anchor-router"

REQUEST_PATH_PATTERN = re.compile(r"\brequest_path\s*=\s*(?P<path>/[^\s?;,|]+)")
STACK_FRAME_PATTERN = re.compile(
    r"\bat\s+(?:[a-z][\w$]*\.)+(?P<class>[A-Z][A-Za-z0-9_$]*)\."
)
CAMEL_CASE_PATTERN = re.compile(r"\b[a-z]+(?:[A-Z][a-z0-9]+){1,}\b")
SERVICE_NAME_PATTERN = re.compile(r"\bbackend-[a-z0-9-]{2,40}\b")

# 搜索价值低或噪音大的锚点
GENERIC_SEGMENTS = {
    "api", "v1", "v2", "v3", "get", "list", "query", "page", "info",
    "detail", "save", "update", "delete", "check", "status", "config",
}
GENERIC_CLASSES = {
    "Controller", "Service", "ServiceImpl", "Repository", "Mapper",
    "Exception", "RuntimeException", "Native", "Method",
}


def _load_authorized_repositories(path: Path = SCOPE_PATH) -> List[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    repos = []
    for item in payload.get("repositories") or []:
        if isinstance(item, dict) and item.get("enabled") and item.get("repository"):
            repos.append(str(item["repository"]))
    return repos


def extract_anchors(text: str, endpoints: List[str], services: List[str]) -> List[str]:
    """从脱敏文本中提取有区分度的搜索锚点，按价值排序，上限 MAX_ANCHORS。"""
    anchors: List[str] = []

    def add(candidate: str) -> None:
        candidate = candidate.strip()
        if candidate and candidate not in anchors:
            anchors.append(candidate)

    paths = list(endpoints or []) + [
        m.group("path") for m in REQUEST_PATH_PATTERN.finditer(text)
    ]
    for path in paths:
        segments = [s for s in path.split("/") if s]
        # 优先取含驼峰的段（业务名），再取普通业务段
        camel = [s for s in segments if CAMEL_CASE_PATTERN.fullmatch(s)]
        plain = [
            s for s in segments
            if s.lower() not in GENERIC_SEGMENTS and len(s) >= 5
            and not s.startswith("[")  # 跳过 [REDACTED:*]
        ]
        for seg in camel + plain:
            if seg.lower() not in GENERIC_SEGMENTS:
                add(seg)

    for match in CAMEL_CASE_PATTERN.finditer(text):
        token = match.group(0)
        if len(token) >= 6 and token.lower() not in GENERIC_SEGMENTS:
            add(token)

    for match in STACK_FRAME_PATTERN.finditer(text):
        name = match.group("class")
        if name not in GENERIC_CLASSES and len(name) >= 6:
            add(name)

    for service in services or []:
        if SERVICE_NAME_PATTERN.fullmatch(service):
            add(service)

    return anchors[:MAX_ANCHORS]


def _load_cache(path: Path = CACHE_PATH) -> Dict[str, Any]:
    try:
        if path.is_file() and path.stat().st_size <= 1_000_000:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("anchors"), dict):
                return payload
    except (OSError, ValueError):
        pass
    return {"version": 1, "anchors": {}}


def _save_cache(cache: Dict[str, Any], path: Path = CACHE_PATH) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(path)
    except OSError:
        pass


class SearchBudgetExceeded(RuntimeError):
    pass


def _search_code(
    anchor: str,
    orgs: List[str],
    token: str,
    cache: Dict[str, Any],
    cache_path: Path = CACHE_PATH,
) -> Dict[str, int]:
    """返回 {repository: 命中数}。带缓存；限流/失败抛 SearchBudgetExceeded 让上层降级。"""
    cached = cache["anchors"].get(anchor)
    if isinstance(cached, dict) and time.time() - cached.get("at", 0) < CACHE_TTL_SECONDS:
        return {str(k): int(v) for k, v in (cached.get("hits") or {}).items()}

    hits: Dict[str, int] = {}
    for org in orgs:
        query = urllib.parse.quote(f"{anchor} org:{org}")
        request = urllib.request.Request(
            f"https://api.github.com/search/code?q={query}&per_page=10",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": USER_AGENT,
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429):
                raise SearchBudgetExceeded(f"code search rate limited (HTTP {exc.code})")
            raise SearchBudgetExceeded(f"code search failed (HTTP {exc.code})")
        except (OSError, ValueError) as exc:
            raise SearchBudgetExceeded(f"code search error: {exc}") from exc
        for item in payload.get("items") or []:
            repo = (item.get("repository") or {}).get("full_name")
            if repo:
                hits[repo] = hits.get(repo, 0) + 1

    cache["anchors"][anchor] = {"at": time.time(), "hits": hits}
    _save_cache(cache, cache_path)
    return hits


def decide(hits_per_anchor: Dict[str, Dict[str, int]], authorized: List[str]) -> Optional[Dict[str, Any]]:
    """汇总各锚点命中：唯一领先仓库才路由，平票/无命中返回 None。"""
    totals: Dict[str, int] = {}
    evidence: Dict[str, str] = {}
    for anchor, hits in hits_per_anchor.items():
        for repo, count in hits.items():
            if repo not in authorized:
                continue
            totals[repo] = totals.get(repo, 0) + count
            evidence.setdefault(repo, anchor)
    if not totals:
        return None
    ranked = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None  # 平票不敢猜
    winner, score = ranked[0]
    return {
        "repository": winner,
        "anchor": evidence[winner],
        "hit_count": score,
        "basis": f"code anchor '{evidence[winner]}' found in {winner} ({score} hits)",
    }


def route_incident(
    summary: str,
    endpoints: List[str],
    services: List[str],
    token: str,
    scope_path: Path = SCOPE_PATH,
    cache_path: Path = CACHE_PATH,
) -> Optional[Dict[str, Any]]:
    """给一条脱敏聚类找仓库。任何失败都返回 None（上层降级到关键词匹配）。"""
    if not token:
        return None
    authorized = _load_authorized_repositories(scope_path)
    if not authorized:
        return None
    anchors = extract_anchors(summary, endpoints, services)
    if not anchors:
        return None
    orgs = sorted({repo.split("/", 1)[0] for repo in authorized})
    cache = _load_cache(cache_path)
    hits_per_anchor: Dict[str, Dict[str, int]] = {}
    queries = 0
    for anchor in anchors:
        if queries >= MAX_QUERIES:
            break
        try:
            hits_per_anchor[anchor] = _search_code(anchor, orgs, token, cache, cache_path)
            queries += 1
        except SearchBudgetExceeded:
            break
    return decide(hits_per_anchor, authorized)
