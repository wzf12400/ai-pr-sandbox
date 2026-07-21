"""Sanitize a raw Kibana/Elasticsearch hit before AI or Issue processing."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import sys
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


POLICY_VERSION = "kibana-sanitizer/v1"
HMAC_KEY_ENV = "LOG_SANITIZER_HMAC_KEY"
MIN_HMAC_KEY_BYTES = 32
MAX_SAFE_SUMMARY_LINES = 50
MAX_SAFE_SUMMARY_CHARS = 6000

MESSAGE_PATTERN = re.compile(
    r"^\[(?P<time>[^\]]+)\]\s+"
    r"\[TID:\s*(?P<trace>[^\]]+)\]\s+"
    r"(?P<level>TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\s+"
    r"\[(?P<thread>[^\]]+)\]\s+"
    r"(?P<logger>[A-Za-z_$][\w.$]*):(?P<line>\d+)\s+-\s+"
    r"(?P<body>.*)$",
    re.DOTALL,
)
BUSINESS_TARGET_PATTERN = re.compile(
    r"(?P<class>(?:[A-Za-z_$][\w$]*\.)+[A-Za-z_$][\w$]*):\s*"
    r"(?P<method>[A-Za-z_$][\w$]*)\s*:"
)
DURATION_PATTERN = re.compile(r"(?i)\bcost\s+time\s*:\s*(?P<duration>\d+)\s*ms\b")
PAGE_TITLE_PATTERN = re.compile(r"(?i)\bpageTitle\s*:\s*(?P<value>[^,\s]+)")

SECRET_KEY_CATEGORIES = {
    "password": "password",
    "passwd": "password",
    "pwd": "password",
    "passphrase": "password",
    "token": "token",
    "accesstoken": "token",
    "refreshtoken": "token",
    "idtoken": "token",
    "jwt": "token",
    "authorization": "authorization",
    "proxyauthorization": "authorization",
    "cookie": "cookie",
    "setcookie": "cookie",
    "jsessionid": "cookie",
    "apikey": "api_key_candidate",
    "appkey": "api_key_candidate",
    "sign": "credential",
    "signature": "credential",
    "secretkey": "api_key_candidate",
    "clientsecret": "api_key_candidate",
    "privatekey": "private_key",
    "databaseurl": "connection_string",
    "connectionstring": "connection_string",
    "datasourceurl": "connection_string",
    "datasourcepassword": "password",
}
IDENTIFIER_KEYS = {
    "duid": "device_identifier",
    "deviceid": "device_identifier",
    "userid": "user_identifier",
    "sessionid": "session_identifier",
}
SAFE_COMMON_PARAM_FIELDS = {
    "appName": "app_name",
    "appVersion": "app_version",
    "system": "platform",
    "apiLevel": "api_level",
}
HIGH_ENTROPY_CONTEXT_ALLOWLIST = {
    "commit",
    "commitsha",
    "dockerid",
    "containerhash",
    "traceid",
    "requestid",
    "requestpath",
    "podid",
    "imagedigest",
}
MISSING_TRACE_VALUES = {"", "-", "0", "n/a", "na", "none", "null", "unknown"}

PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN (?P<key_type>[A-Z0-9 ]*PRIVATE KEY)-----.*?"
    r"-----END (?P=key_type)-----",
    re.DOTALL,
)
AUTHORIZATION_PATTERN = re.compile(
    r"(?i)\b(?:authorization|proxy-authorization)[\"']?\s*[:=]\s*[\"']?\s*"
    r"(?:(?:bearer|basic)\s+)?[^\s,;}\"']+"
)
COOKIE_PATTERN = re.compile(
    r"(?im)\b(?:cookie|set-cookie)[\"']?\s*[:=]\s*[\"']?[^\r\n}\"']+"
)
JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")
CONNECTION_URI_PATTERN = re.compile(
    r"(?i)\b(?:jdbc:)?(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp|mssql|sqlserver)"
    r"://[^\s]+"
)
GENERIC_CREDENTIAL_URI_PATTERN = re.compile(
    r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^@\s/]+@[^\s]+"
)
KNOWN_TOKEN_PATTERN = re.compile(
    r"\b(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16})\b"
)
SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(?P<key>password|passwd|pwd|passphrase|token|access[_-]?token|"
    r"refresh[_-]?token|id[_-]?token|api[_-]?key|app[_-]?key|secret[_-]?key|"
    r"client[_-]?secret|private[_-]?key|sign(?:ature)?|database[_-]?url|connection[_-]?string|"
    r"datasource[_-]?password)[\"']?\s*[:=]\s*[\"']?"
    r"(?P<value>[^\s,;)\]}\"']+)"
)
HIGH_ENTROPY_CANDIDATE_PATTERN = re.compile(r"[A-Za-z0-9+/_-]{20,}={0,2}")
REQUEST_URL_PATTERN = re.compile(r"https?://[^\s]+")
SAFE_ROUTE_SEGMENT_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,40}$")
CLIENT_DESCRIPTOR_PATTERN = re.compile(
    r"(?P<application>(?:[A-Za-z0-9_-]+\.){2,}[A-Za-z0-9_.-]+(?:/\d+)?)\s+"
    r"\((?P<identifiers>[^)\r\n]{1,300})\)(?=\s+Country/)"
)
SQL_STATEMENT_PATTERN = re.compile(
    r"(?is)(?P<prefix>###\s*SQL:\s*).*?(?=(?:\s*\|\s*###\s*[A-Za-z ]+:)|$)"
)


@dataclass(frozen=True)
class Finding:
    path: str
    category: str
    action: str
    rule_id: str


def _canonical_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _hmac_ref(key: bytes, namespace: str, value: str) -> str:
    if not value:
        return ""
    digest = hmac.new(key, f"{namespace}:{value}".encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{namespace}_ref:{digest[:16]}"


def _trace_ref(key: bytes, value: str) -> str:
    normalized = value.strip().lower()
    return "" if normalized in MISSING_TRACE_VALUES else _hmac_ref(key, "trace", value)


def _mapping(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _integer(value: Any) -> Optional[int]:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _extract_parenthesized(text: str, marker: str) -> Tuple[str, Optional[Tuple[int, int]]]:
    start = text.find(marker)
    if start < 0:
        return "", None
    content_start = start + len(marker)
    depth = 1
    for index in range(content_start, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return text[content_start:index], (start, index + 1)
    return "", None


def _split_key_values(value: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    for part in re.split(r",\s*(?=[A-Za-z_][\w.-]*\s*=)", value):
        if "=" not in part:
            continue
        key, raw_value = part.split("=", 1)
        fields[key.strip()] = raw_value.strip()
    return fields


def _entropy(value: str) -> float:
    if not value:
        return 0.0
    counts: Dict[str, int] = {}
    for character in value:
        counts[character] = counts.get(character, 0) + 1
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def _preceding_assignment_key(text: str, start: int) -> str:
    prefix = text[max(0, start - 48):start]
    match = re.search(r"([A-Za-z_][\w.-]{0,30})\s*[:=]\s*$", prefix)
    return _canonical_key(match.group(1)) if match else ""


def _is_allowlisted_code_identifier(text: str, match: re.Match[str]) -> bool:
    candidate = match.group(0)
    prefix = text[max(0, match.start() - 300):match.start()]
    suffix = text[match.end():match.end() + 160]
    if "class path resource [" in prefix and re.match(r"\.xml\]", suffix):
        if re.fullmatch(
            r"(?:[A-Za-z_$][A-Za-z0-9_$]*/)*[A-Za-z_$][A-Za-z0-9_$]*",
            candidate,
        ):
            return True
    if not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]{19,}", candidate):
        return False
    if candidate.endswith(("Exception", "Error")):
        if re.search(r"(?:[A-Za-z_$][\w$]*\.)+$", prefix) and re.match(r"\s*:", suffix):
            return True
    if re.search(r"\bat\s+(?:[A-Za-z_$][\w$]*\.)+$", prefix):
        if re.match(r"(?:\.[A-Za-z_$][\w$]*)?\([^\r\n)]*\)", suffix):
            return True
    return False


def _is_safe_route_segment(segment: str) -> bool:
    if not SAFE_ROUTE_SEGMENT_PATTERN.fullmatch(segment):
        return False
    if len(segment) < 20:
        return True
    is_hex = bool(re.fullmatch(r"[A-Fa-f0-9]+", segment))
    threshold = 3.2 if is_hex and len(segment) >= 32 else 4.0
    return _entropy(segment) < threshold


def _minimize_request_urls(text: str, path: str) -> Tuple[str, List[Finding]]:
    findings: List[Finding] = []

    def replace(match: re.Match[str]) -> str:
        raw_url = match.group(0)
        url = raw_url.rstrip(".,;:")
        trailing = raw_url[len(url):]
        try:
            parsed = urllib.parse.urlsplit(url)
        except ValueError:
            findings.append(
                Finding(f"{path}.url", "malformed_url", "blocked", "request-url")
            )
            return f"[REDACTED:malformed_url]{trailing}"
        findings.append(Finding(f"{path}.url.host", "internal_host", "removed", "request-url"))
        if parsed.fragment:
            findings.append(
                Finding(f"{path}.url.fragment", "url_fragment", "removed", "request-url")
            )

        safe_segments: List[str] = []
        for segment in parsed.path.split("/"):
            if not segment:
                continue
            if _is_safe_route_segment(segment):
                safe_segments.append(segment)
            else:
                safe_segments.append("[REDACTED:path_segment]")
                findings.append(
                    Finding(f"{path}.url.path", "path_identifier", "removed", "request-url")
                )

        safe_keys: List[str] = []
        for part in parsed.query.split("&")[:50]:
            if not part:
                continue
            raw_key = urllib.parse.unquote_plus(part.split("=", 1)[0]).strip()
            canonical = _canonical_key(raw_key)
            category = SECRET_KEY_CATEGORIES.get(canonical)
            if category:
                safe_keys.append(f"[REDACTED:{category}]")
                findings.append(
                    Finding(f"{path}.url.query.{raw_key}", category, "removed", "request-url")
                )
            elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,40}", raw_key):
                safe_keys.append(raw_key)
                findings.append(
                    Finding(f"{path}.url.query.{raw_key}", "url_parameter", "removed", "request-url")
                )
            else:
                safe_keys.append("[REDACTED:query_key]")
                findings.append(
                    Finding(f"{path}.url.query", "query_key", "removed", "request-url")
                )

        safe_path = "/" + "/".join(safe_segments)
        query_summary = f" query_keys={','.join(safe_keys)}" if safe_keys else ""
        return f"request_path={safe_path}{query_summary}{trailing}"

    return REQUEST_URL_PATTERN.sub(replace, text), findings


def _remove_client_descriptors(text: str, path: str) -> Tuple[str, List[Finding]]:
    findings: List[Finding] = []

    def replace(_: re.Match[str]) -> str:
        findings.extend(
            [
                Finding(path, "application_identifier", "removed", "client-descriptor"),
                Finding(path, "client_identifier", "removed", "client-descriptor"),
            ]
        )
        return "[REDACTED:client_descriptor]"

    return CLIENT_DESCRIPTOR_PATTERN.sub(replace, text), findings


def _remove_sql_statements(text: str, path: str) -> Tuple[str, List[Finding]]:
    findings: List[Finding] = []

    def replace(match: re.Match[str]) -> str:
        findings.append(
            Finding(f"{path}.sql", "sql_statement", "removed", "sql-statement")
        )
        return f"{match.group('prefix')}[REDACTED:sql_statement]"

    return SQL_STATEMENT_PATTERN.sub(replace, text), findings


def _redact_unclassified_entropy(text: str, path: str) -> Tuple[str, List[Finding]]:
    findings: List[Finding] = []

    def replace(match: re.Match[str]) -> str:
        candidate = match.group(0)
        context_key = _preceding_assignment_key(text, match.start())
        if context_key in HIGH_ENTROPY_CONTEXT_ALLOWLIST:
            return candidate
        if _is_allowlisted_code_identifier(text, match):
            return candidate
        is_hex = bool(re.fullmatch(r"[A-Fa-f0-9]+", candidate))
        threshold = 3.2 if is_hex and len(candidate) >= 32 else 4.0
        if _entropy(candidate) < threshold:
            return candidate
        findings.append(Finding(path, "unclassified_high_entropy", "blocked", "entropy"))
        return "[REDACTED:unclassified_high_entropy]"

    return HIGH_ENTROPY_CANDIDATE_PATTERN.sub(replace, text), findings


def _apply_pattern(
    text: str,
    pattern: re.Pattern[str],
    path: str,
    category: str,
    rule_id: str,
) -> Tuple[str, List[Finding]]:
    findings: List[Finding] = []

    def replace(_: re.Match[str]) -> str:
        findings.append(Finding(path, category, "removed", rule_id))
        return f"[REDACTED:{category}]"

    return pattern.sub(replace, text), findings


def redact_free_text(text: str, path: str = "message") -> Tuple[str, List[Finding]]:
    sanitized = text.replace("\x00", "[NUL]").replace("\r", "\\r")
    findings: List[Finding] = []
    sanitized, url_findings = _minimize_request_urls(sanitized, path)
    findings.extend(url_findings)
    sanitized, client_findings = _remove_client_descriptors(sanitized, path)
    findings.extend(client_findings)
    sanitized, sql_findings = _remove_sql_statements(sanitized, path)
    findings.extend(sql_findings)
    pattern_rules = (
        (PRIVATE_KEY_PATTERN, "private_key", "private-key-block"),
        (AUTHORIZATION_PATTERN, "authorization", "authorization-header"),
        (COOKIE_PATTERN, "cookie", "cookie-header"),
        (CONNECTION_URI_PATTERN, "connection_string", "database-uri"),
        (GENERIC_CREDENTIAL_URI_PATTERN, "connection_string", "credential-uri"),
        (JWT_PATTERN, "token", "jwt"),
        (KNOWN_TOKEN_PATTERN, "token", "known-token-prefix"),
    )
    for pattern, category, rule_id in pattern_rules:
        sanitized, detected = _apply_pattern(sanitized, pattern, path, category, rule_id)
        findings.extend(detected)

    def redact_assignment(match: re.Match[str]) -> str:
        key = match.group("key")
        category = SECRET_KEY_CATEGORIES.get(_canonical_key(key), "credential")
        findings.append(Finding(path, category, "removed", "secret-assignment"))
        return f"{key}=[REDACTED:{category}]"

    sanitized = SECRET_ASSIGNMENT_PATTERN.sub(redact_assignment, sanitized)
    sanitized, entropy_findings = _redact_unclassified_entropy(sanitized, path)
    findings.extend(entropy_findings)
    return sanitized, findings


def _parse_message(message: str, key: bytes) -> Tuple[Dict[str, Any], List[Finding]]:
    findings: List[Finding] = []
    match = MESSAGE_PATTERN.match(message)
    if match:
        parsed = match.groupdict()
        body = parsed["body"]
        trace_ref = _trace_ref(key, parsed["trace"])
        level = parsed["level"]
        logger_class = parsed["logger"]
        logger_line = int(parsed["line"])
    else:
        body = message
        trace_ref = ""
        level = "UNKNOWN"
        logger_class = ""
        logger_line = None

    common_params, common_span = _extract_parenthesized(body, "CommonParams(")
    client: Dict[str, str] = {}
    if common_span:
        common_fields = _split_key_values(common_params)
        for field_name, field_value in common_fields.items():
            canonical = _canonical_key(field_name)
            if canonical in SECRET_KEY_CATEGORIES:
                findings.append(
                    Finding(
                        f"message.CommonParams.{field_name}",
                        SECRET_KEY_CATEGORIES[canonical],
                        "removed",
                        "sensitive-field-name",
                    )
                )
            elif canonical in IDENTIFIER_KEYS:
                findings.append(
                    Finding(
                        f"message.CommonParams.{field_name}",
                        IDENTIFIER_KEYS[canonical],
                        "removed",
                        "identifier-field-name",
                    )
                )
            elif field_name in SAFE_COMMON_PARAM_FIELDS and field_value.lower() not in {"null", "none", ""}:
                client[SAFE_COMMON_PARAM_FIELDS[field_name]] = field_value
        start, end = common_span
        body = body[:start] + "CommonParams([SANITIZED])" + body[end:]

    page_title_match = PAGE_TITLE_PATTERN.search(body)
    if page_title_match:
        client["page_title"] = page_title_match.group("value")

    business_match = BUSINESS_TARGET_PATTERN.search(body)
    duration_match = DURATION_PATTERN.search(body)
    safe_summary, text_findings = redact_free_text(body, "message.summary")
    findings.extend(text_findings)
    safe_summary = safe_summary.strip()
    if len(safe_summary.splitlines()) > MAX_SAFE_SUMMARY_LINES:
        safe_summary = "\n".join(safe_summary.splitlines()[:MAX_SAFE_SUMMARY_LINES])
        findings.append(Finding("message.summary", "oversized_text", "truncated", "line-limit"))
    if len(safe_summary) > MAX_SAFE_SUMMARY_CHARS:
        safe_summary = safe_summary[:MAX_SAFE_SUMMARY_CHARS]
        findings.append(Finding("message.summary", "oversized_text", "truncated", "character-limit"))

    event = {
        "level": level,
        "trace_ref": trace_ref,
        "logger_class": logger_class,
        "logger_line": logger_line,
        "business_class": business_match.group("class") if business_match else "",
        "business_method": business_match.group("method") if business_match else "",
        "duration_ms": int(duration_match.group("duration")) if duration_match else None,
        "client": client,
        "safe_summary": safe_summary,
    }
    return event, findings


def _safe_image_tag(container_image: str) -> str:
    image = container_image.rsplit("/", 1)[-1]
    return image if "@" not in image else image.split("@", 1)[0]


def _find_known_secret_signatures(text: str) -> List[str]:
    categories: List[str] = []
    checks = (
        (PRIVATE_KEY_PATTERN, "private_key"),
        (AUTHORIZATION_PATTERN, "authorization"),
        (COOKIE_PATTERN, "cookie"),
        (CONNECTION_URI_PATTERN, "connection_string"),
        (GENERIC_CREDENTIAL_URI_PATTERN, "connection_string"),
        (JWT_PATTERN, "token"),
        (KNOWN_TOKEN_PATTERN, "token"),
        (SECRET_ASSIGNMENT_PATTERN, "credential"),
    )
    for pattern, category in checks:
        if pattern.search(text):
            categories.append(category)
    return categories


def sanitize_hit(payload: Dict[str, Any], hmac_key: bytes) -> Dict[str, Any]:
    if len(hmac_key) < MIN_HMAC_KEY_BYTES:
        raise ValueError(f"HMAC key must contain at least {MIN_HMAC_KEY_BYTES} bytes")

    source = _mapping(payload.get("_source"))
    kubernetes = _mapping(source.get("kubernetes"))
    labels = _mapping(kubernetes.get("labels"))
    raw_message = _text(source.get("message"))
    message_event, findings = _parse_message(raw_message, hmac_key)

    document_id = _text(payload.get("_id"))
    service = (
        _text(labels.get("app_kubernetes_io/name"))
        or _text(kubernetes.get("container_name"))
        or _text(kubernetes.get("namespace_name"))
    )
    container_image = _text(kubernetes.get("container_image"))

    omitted_infrastructure = {
        "pod_name": kubernetes.get("pod_name"),
        "pod_id": kubernetes.get("pod_id"),
        "docker_id": kubernetes.get("docker_id"),
        "host": kubernetes.get("host"),
        "container_hash": kubernetes.get("container_hash"),
    }
    for name, value in omitted_infrastructure.items():
        if value:
            findings.append(
                Finding(f"_source.kubernetes.{name}", "infrastructure_identifier", "internal_only", "field-policy")
            )

    blocked = any(finding.action == "blocked" for finding in findings)
    credential_categories = {
        "password",
        "token",
        "cookie",
        "authorization",
        "private_key",
        "connection_string",
        "api_key_candidate",
        "credential",
    }
    security_review_required = any(finding.category in credential_categories for finding in findings)
    level = message_event["level"]
    is_error = level in {"ERROR", "FATAL"}

    result: Dict[str, Any] = {
        "schema_version": "sanitized-kibana-event/v1",
        "source": {
            "type": "kibana",
            "event_ref": _hmac_ref(hmac_key, "event", document_id),
            "timestamp": _text(source.get("@timestamp")),
            "stream": _text(source.get("stream")),
        },
        "target": {
            "service": service,
            "namespace": _text(kubernetes.get("namespace_name")),
            "business_class": message_event["business_class"],
            "business_method": message_event["business_method"],
            "logger_class": message_event["logger_class"],
            "logger_line": message_event["logger_line"],
        },
        "runtime": {
            "region": _text(labels.get("topology_kubernetes_io/region")),
            "zone": _text(labels.get("topology_kubernetes_io/zone")),
            "image_tag": _safe_image_tag(container_image),
        },
        "event": {
            "level": level,
            "trace_ref": message_event["trace_ref"],
            "duration_ms": message_event["duration_ms"],
            "client": message_event["client"],
            "summary": message_event["safe_summary"],
            "is_error": is_error,
            "is_issue_candidate": is_error and not blocked,
        },
    }

    serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
    residual_categories = _find_known_secret_signatures(serialized)
    for category in residual_categories:
        findings.append(Finding("sanitized_output", category, "blocked", "final-rescan"))
    if residual_categories:
        blocked = True

    if blocked:
        status = "blocked"
    elif findings:
        status = "passed_with_redactions"
    else:
        status = "passed"

    result["sanitization"] = {
        "status": status,
        "policy_version": POLICY_VERSION,
        "ai_allowed": not blocked,
        "github_issue_allowed": not blocked and not security_review_required,
        "security_review_required": security_review_required,
        "removed_categories": sorted({finding.category for finding in findings}),
        "findings": [asdict(finding) for finding in findings],
    }
    if blocked:
        result["event"]["is_issue_candidate"] = False
    return result


def write_sanitized_event(path: Path, payload: Dict[str, Any]) -> None:
    if path.exists():
        raise FileExistsError(f"output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load_hit(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON at line {exc.lineno}, column {exc.colno}") from exc
    if not isinstance(payload, dict):
        raise ValueError("input JSON must contain one Elasticsearch hit object")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sanitize one raw Kibana/Elasticsearch hit.")
    parser.add_argument("input", type=Path, help="Raw Elasticsearch hit JSON.")
    parser.add_argument("--output", type=Path, required=True, help="Sanitized local JSON output.")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    raw_key = os.environ.get(HMAC_KEY_ENV, "")
    if len(raw_key.encode("utf-8")) < MIN_HMAC_KEY_BYTES:
        print(
            f"error: {HMAC_KEY_ENV} must contain at least {MIN_HMAC_KEY_BYTES} bytes",
            file=sys.stderr,
        )
        return 2

    try:
        result = sanitize_hit(load_hit(args.input), raw_key.encode("utf-8"))
        write_sanitized_event(args.output, result)
    except (FileExistsError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"{args.output} ({result['sanitization']['status']})")
    return 4 if result["sanitization"]["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
