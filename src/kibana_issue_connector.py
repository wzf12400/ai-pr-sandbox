"""Fetch bounded OpenSearch Dashboards error candidates and turn them into guarded Issues."""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src import ai_issue_generator, kibana_sanitizer
from src.issue_draft import _atomic_write_json
from src.issue_entry import _gateway_config, _infer_repository, publish_issue


USERNAME_ENV = "OPENSEARCH_USERNAME"
PASSWORD_ENV = "OPENSEARCH_PASSWORD"
TENANT_ENV = "OPENSEARCH_TENANT"
MAX_CANDIDATES = 20
MAX_PUBLISH_CANDIDATES = 3
MAX_FETCH_SIZE = 100
DATA_VIEW_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
INDEX_PATTERN = re.compile(r"^[A-Za-z0-9._*,-]{1,500}$")
RELATIVE_TIME_PATTERN = re.compile(r"^now(?:-\d+[mhdw])?$|^now$")


@dataclass(frozen=True)
class DiscoverTarget:
    base_url: str
    data_view_id: str
    time_from: str
    time_to: str


@dataclass(frozen=True)
class DashboardCredentials:
    username: str
    password: str = field(repr=False)
    tenant: str = ""


def parse_discover_url(url: str) -> DiscoverTarget:
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Discover URL must use HTTPS")
    marker = "/_dashboards/app/discover"
    if marker not in parsed.path:
        raise ValueError("URL must point to OpenSearch Dashboards Discover")
    base_path = parsed.path.split("/app/discover", 1)[0]
    fragment = urllib.parse.unquote(parsed.fragment)
    data_view = re.search(r"(?:^|[,(])index:([^,)]+)", fragment)
    time_range = re.search(r"time:\(from:([^,)]+),to:([^,)]+)\)", fragment)
    if not data_view or not time_range:
        raise ValueError("Discover URL must include a data-view ID and time range")
    data_view_id = data_view.group(1).strip("'\"")
    time_from = time_range.group(1).strip("'\"")
    time_to = time_range.group(2).strip("'\"")
    if not DATA_VIEW_PATTERN.fullmatch(data_view_id):
        raise ValueError("Discover data-view ID contains unsupported characters")
    if not RELATIVE_TIME_PATTERN.fullmatch(time_from) or not RELATIVE_TIME_PATTERN.fullmatch(time_to):
        raise ValueError("only bounded relative Discover time ranges are supported")
    return DiscoverTarget(
        base_url=urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, base_path, "", "")),
        data_view_id=data_view_id,
        time_from=time_from,
        time_to=time_to,
    )


def _safe_http_detail(exc: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(exc.read(4096).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    values: List[Any] = []
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            values.append(error.get("reason"))
        values.extend([payload.get("message"), payload.get("statusCode")])
    detail = next((str(value).strip() for value in values if value not in (None, "")), "")
    detail = " ".join(detail.split())[:300]
    sanitized, findings = kibana_sanitizer.redact_free_text(detail, "gateway_error")
    return "" if any(item.action == "blocked" for item in findings) else sanitized


class OpenSearchDashboardsClient:
    def __init__(
        self,
        target: DiscoverTarget,
        credentials: DashboardCredentials,
        timeout_seconds: float = 30,
        opener: Any = urllib.request.urlopen,
    ):
        self.target = target
        self.credentials = credentials
        self.timeout_seconds = timeout_seconds
        self._opener = opener

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not path.startswith("/api/"):
            raise ValueError("Dashboards request path is not allowed")
        token = base64.b64encode(
            f"{self.credentials.username}:{self.credentials.password}".encode("utf-8")
        ).decode("ascii")
        headers = {
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
            "osd-xsrf": "ai-pr-issue-connector",
        }
        if self.credentials.tenant:
            headers["securitytenant"] = self.credentials.tenant
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.target.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
                final_url = response.geturl()
        except urllib.error.HTTPError as exc:
            detail = _safe_http_detail(exc)
            suffix = f": {detail}" if detail else ""
            raise ValueError(f"OpenSearch Dashboards returned HTTP {exc.code}{suffix}") from exc
        except urllib.error.URLError as exc:
            raise ValueError("OpenSearch Dashboards request failed") from exc
        if "/app/login" in final_url:
            raise ValueError("OpenSearch Dashboards credentials were not accepted")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("OpenSearch Dashboards returned a non-JSON response") from exc
        if not isinstance(result, dict):
            raise ValueError("OpenSearch Dashboards response must be a JSON object")
        return result

    def resolve_index_pattern(self) -> Tuple[str, str]:
        data_view_id = urllib.parse.quote(self.target.data_view_id, safe="")
        payload = self._request_json("GET", f"/api/saved_objects/index-pattern/{data_view_id}")
        attributes = payload.get("attributes")
        if not isinstance(attributes, dict):
            raise ValueError("data view response has no attributes")
        title = str(attributes.get("title", "")).strip()
        time_field = str(attributes.get("timeFieldName", "@timestamp")).strip() or "@timestamp"
        if not INDEX_PATTERN.fullmatch(title):
            raise ValueError("resolved index pattern contains unsupported characters")
        if time_field != "@timestamp":
            raise ValueError("only @timestamp data views are supported in phase one")
        return title, time_field

    def fetch_error_hits(self, index_pattern: str, time_field: str, fetch_size: int) -> List[Dict[str, Any]]:
        if not 1 <= fetch_size <= MAX_FETCH_SIZE:
            raise ValueError(f"fetch size must be between 1 and {MAX_FETCH_SIZE}")
        search_path = urllib.parse.urlencode(
            {"path": f"{index_pattern}/_search", "method": "POST"}
        )
        payload = {
            "size": fetch_size,
            "track_total_hits": False,
            "_source": [
                "@timestamp",
                "stream",
                "logtag",
                "message",
                "kubernetes.namespace_name",
                "kubernetes.container_name",
                "kubernetes.container_image",
                "kubernetes.labels.app_kubernetes_io/name",
                "kubernetes.labels.topology_kubernetes_io/region",
                "kubernetes.labels.topology_kubernetes_io/zone",
            ],
            "query": {
                "bool": {
                    "filter": [
                        {
                            "range": {
                                time_field: {
                                    "gte": self.target.time_from,
                                    "lte": self.target.time_to,
                                }
                            }
                        },
                        {
                            "query_string": {
                                "query": 'message:(ERROR OR FATAL OR Exception OR "Caused by")',
                                "analyze_wildcard": True,
                            }
                        },
                    ]
                }
            },
            "sort": [{time_field: {"order": "desc", "unmapped_type": "date"}}],
        }
        response = self._request_json("POST", f"/api/console/proxy?{search_path}", payload)
        hits = response.get("hits", {}).get("hits", []) if isinstance(response.get("hits"), dict) else []
        if not isinstance(hits, list):
            raise ValueError("OpenSearch search response has invalid hits")
        return [hit for hit in hits if isinstance(hit, dict) and isinstance(hit.get("_source"), dict)]


def _credentials(prompt_password: bool, username: str) -> DashboardCredentials:
    resolved_username = username.strip() or os.environ.get(USERNAME_ENV, "").strip()
    password = os.environ.get(PASSWORD_ENV, "")
    if prompt_password and not password:
        password = getpass.getpass("OpenSearch password: ")
    if not resolved_username or not password:
        raise ValueError(
            f"{USERNAME_ENV} and {PASSWORD_ENV} are required, or use --username with --prompt-password"
        )
    return DashboardCredentials(
        username=resolved_username,
        password=password,
        tenant=os.environ.get(TENANT_ENV, "").strip(),
    )


def _load_published(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"version": 1, "published": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid connector state file: {path}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("published", {}), dict):
        raise ValueError(f"invalid connector state file: {path}")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch bounded OpenSearch error candidates and optionally create reviewed Issues."
    )
    parser.add_argument("--discover-url", required=True, help="OpenSearch Dashboards Discover URL.")
    parser.add_argument("--username", default="", help=f"Read-only username; defaults to {USERNAME_ENV}.")
    parser.add_argument("--prompt-password", action="store_true", help="Read the password without echoing it.")
    parser.add_argument("--max-candidates", type=int, default=5, help=f"Candidate limit, maximum {MAX_CANDIDATES}.")
    parser.add_argument("--fetch-size", type=int, default=50, help=f"Remote hit limit, maximum {MAX_FETCH_SIZE}.")
    parser.add_argument("--generate", action="store_true", help="Generate locally reviewed AI Issue drafts.")
    parser.add_argument("--publish", action="store_true", help="Publish valid generated drafts with gh.")
    parser.add_argument("--confirm", action="store_true", help="Confirm human-approved GitHub publication.")
    parser.add_argument("--repository", help="GitHub owner/name; defaults to origin.")
    parser.add_argument("--prompt-api-key", action="store_true", help="Read AI_API_KEY without echoing it.")
    parser.add_argument("--output-dir", type=Path, default=Path(".kibana-issue-output"))
    parser.add_argument("--state-file", type=Path, default=Path(".issue-entry-state/kibana.json"))
    parser.add_argument("--name", help="Output folder; defaults to a UTC timestamp.")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.max_candidates <= MAX_CANDIDATES:
        print(f"error: --max-candidates must be between 1 and {MAX_CANDIDATES}", file=sys.stderr)
        return 2
    if args.publish and (not args.generate or not args.confirm):
        print("error: --publish requires --generate and --confirm", file=sys.stderr)
        return 2
    if args.publish and args.max_candidates > MAX_PUBLISH_CANDIDATES:
        print(
            f"error: publication is limited to {MAX_PUBLISH_CANDIDATES} candidates per run",
            file=sys.stderr,
        )
        return 2

    run_name = args.name or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = args.output_dir / run_name
    if run_dir.exists():
        print(f"error: output already exists: {run_dir}", file=sys.stderr)
        return 2

    try:
        target = parse_discover_url(args.discover_url)
        raw_key = os.environ.get(kibana_sanitizer.HMAC_KEY_ENV, "").encode("utf-8")
        if len(raw_key) < kibana_sanitizer.MIN_HMAC_KEY_BYTES:
            raise ValueError(
                f"{kibana_sanitizer.HMAC_KEY_ENV} must contain at least "
                f"{kibana_sanitizer.MIN_HMAC_KEY_BYTES} bytes"
            )
        client = OpenSearchDashboardsClient(
            target,
            _credentials(args.prompt_password, args.username),
        )
        index_pattern, time_field = client.resolve_index_pattern()
        hits = client.fetch_error_hits(index_pattern, time_field, args.fetch_size)
        published_state = _load_published(args.state_file)
        seen = published_state.setdefault("published", {})

        candidates: List[Dict[str, Any]] = []
        candidate_refs = set()
        for hit in hits:
            sanitized = kibana_sanitizer.sanitize_hit(hit, raw_key)
            event_ref = str(sanitized.get("source", {}).get("event_ref", ""))
            if (
                sanitized.get("event", {}).get("is_issue_candidate")
                and event_ref
                and event_ref not in seen
                and event_ref not in candidate_refs
            ):
                candidates.append(sanitized)
                candidate_refs.add(event_ref)
            if len(candidates) >= args.max_candidates:
                break

        config = _gateway_config(args.prompt_api_key) if args.generate else None
        repository = args.repository or _infer_repository() if args.publish else ""
        if args.publish and not repository:
            raise ValueError("--repository is required when origin is not a GitHub repository")

        summary: Dict[str, Any] = {
            "schema_version": "kibana-issue-connector/v1",
            "source": {
                "base_url": target.base_url,
                "data_view_id": target.data_view_id,
                "time_from": target.time_from,
                "time_to": target.time_to,
            },
            "query": {
                "resolved_index_pattern": index_pattern,
                "fetch_size": args.fetch_size,
                "returned_hits": len(hits),
                "candidate_limit": args.max_candidates,
            },
            "mode": "publish" if args.publish else "generate" if args.generate else "dry_run",
            "candidates": [],
        }
        for position, sanitized in enumerate(candidates, start=1):
            candidate_dir = run_dir / f"candidate-{position:02d}"
            sanitized_path = candidate_dir / "sanitized-event.json"
            _atomic_write_json(sanitized_path, sanitized)
            item: Dict[str, Any] = {
                "event_ref": sanitized["source"]["event_ref"],
                "timestamp": sanitized["source"]["timestamp"],
                "service": sanitized["target"]["service"],
                "level": sanitized["event"]["level"],
                "status": "sanitized",
                "artifact": str(sanitized_path),
            }
            if args.generate and config is not None:
                result = ai_issue_generator.generate_issue(
                    sanitized,
                    ai_issue_generator.OpenAICompatibleChatProvider(config, config.model),
                    ai_issue_generator.OpenAICompatibleChatProvider(config, config.review_model),
                )
                result_path = candidate_dir / "result.json"
                markdown_path = candidate_dir / "issue.md"
                ai_issue_generator.write_result(result, result_path, markdown_path)
                item.update(
                    {
                        "status": result["state"],
                        "issue_draft": str(markdown_path),
                        "validation_valid": result["validation"]["valid"],
                    }
                )
                if args.publish:
                    if not sanitized.get("sanitization", {}).get("github_issue_allowed", False):
                        raise ValueError("sanitized event requires security review before publication")
                    issue_url = publish_issue(result, markdown_path, repository, sanitized)
                    item["status"] = "published"
                    item["issue_url"] = issue_url
                    seen[item["event_ref"]] = {
                        "issue_url": issue_url,
                        "published_at": datetime.now(timezone.utc).isoformat(),
                    }
                    _atomic_write_json(args.state_file, published_state)
            summary["candidates"].append(item)
        _atomic_write_json(run_dir / "summary.json", summary)
    except (FileExistsError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(run_dir / "summary.json")
    for item in summary["candidates"]:
        if item.get("issue_url"):
            print(item["issue_url"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
