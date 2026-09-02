"""Create a guarded triage Issue draft from one sanitized Kibana event."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


SANITIZED_SCHEMA_VERSION = "sanitized-kibana-event/v1"


def _mapping(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _display(value: Any) -> str:
    if value is None or value == "":
        return "未从日志获得"
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


@dataclass(frozen=True)
class TriageDecision:
    state: str
    reason: str
    publication_allowed: bool
    security_review_required: bool
    missing_information: List[str]


def evaluate_event(payload: Dict[str, Any]) -> TriageDecision:
    if payload.get("schema_version") != SANITIZED_SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {SANITIZED_SCHEMA_VERSION}")

    sanitization = _mapping(payload.get("sanitization"))
    event = _mapping(payload.get("event"))
    if not sanitization.get("ai_allowed", False):
        return TriageDecision(
            state="blocked",
            reason="脱敏策略阻断，原始事件不得进入 Issue 或 AI 流程",
            publication_allowed=False,
            security_review_required=True,
            missing_information=[],
        )
    if not event.get("is_error", False):
        return TriageDecision(
            state="ignored_non_error",
            reason="事件级别不是 ERROR 或 FATAL",
            publication_allowed=False,
            security_review_required=bool(sanitization.get("security_review_required")),
            missing_information=[],
        )

    target = _mapping(payload.get("target"))
    missing: List[str] = []
    if not _text(target.get("service")):
        missing.append("责任服务或应用")
    if not _text(target.get("business_class")) and not _text(target.get("logger_class")):
        missing.append("代码入口对象")
    missing.extend(["接口路径或消息 Topic", "期望行为", "最短复现步骤", "验收标准"])

    security_review = bool(sanitization.get("security_review_required"))
    github_allowed = bool(sanitization.get("github_issue_allowed"))
    return TriageDecision(
        state="needs_human_context" if missing or security_review else "ready_for_review",
        reason="日志证据已脱敏，但发布前仍需补充业务上下文并人工确认",
        publication_allowed=github_allowed and not missing and not security_review,
        security_review_required=security_review,
        missing_information=missing,
    )


def render_triage_markdown(payload: Dict[str, Any]) -> str:
    decision = evaluate_event(payload)
    if decision.state in {"blocked", "ignored_non_error"}:
        raise ValueError(f"event is not eligible for a triage draft: {decision.state}")

    source = _mapping(payload.get("source"))
    target = _mapping(payload.get("target"))
    runtime = _mapping(payload.get("runtime"))
    event = _mapping(payload.get("event"))
    client = _mapping(event.get("client"))
    sanitization = _mapping(payload.get("sanitization"))
    code_entry = ".".join(
        part
        for part in (_text(target.get("business_class")), _text(target.get("business_method")))
        if part
    ) or _text(target.get("logger_class"))
    title_object = _text(target.get("service")) or code_entry or "未知服务"
    level = _text(event.get("level")) or "ERROR"
    summary = _text(event.get("summary")) or "日志未提供可公开的错误摘要"

    missing = "\n".join(f"- [ ] {item}" for item in decision.missing_information)
    removed = ", ".join(sanitization.get("removed_categories", [])) or "无"
    lines = [
        f"# [待分诊] {title_object} 出现 {level} 事件",
        "",
        "## 来源",
        "",
        f"- 类型：Kibana 脱敏事件",
        f"- 事件引用：{_display(source.get('event_ref'))}",
        f"- 时间：{_display(source.get('timestamp'))}",
        "",
        "## 对象",
        "",
        f"- 服务：{_display(target.get('service'))}",
        f"- Namespace：{_display(target.get('namespace'))}",
        f"- 代码入口：{_display(code_entry)}",
        f"- 日志位置：{_display(target.get('logger_class'))}:{_display(target.get('logger_line'))}",
        "",
        "## 接口",
        "",
        "- 接口路径或 Topic：未从日志获得",
        f"- 应用：{_display(client.get('app_name'))}",
        f"- 平台：{_display(client.get('platform'))}",
        f"- 页面：{_display(client.get('page_title'))}",
        "",
        "## 报错",
        "",
        f"- 级别：{level}",
        f"- Trace 引用：{_display(event.get('trace_ref'))}",
        f"- 耗时：{_display(event.get('duration_ms'))} ms",
        "",
        "```text",
        summary,
        "```",
        "",
        "## 环境",
        "",
        f"- Region / Zone：{_display(runtime.get('region'))} / {_display(runtime.get('zone'))}",
        f"- 镜像 Tag：{_display(runtime.get('image_tag'))}",
        "",
        "## 待补充",
        "",
        missing,
        "",
        "## 安全与发布状态",
        "",
        f"- 分诊状态：{decision.state}",
        f"- 脱敏策略：{_display(sanitization.get('policy_version'))}",
        f"- 已移除类别：{removed}",
        f"- 需要安全复核：{_display(decision.security_review_required)}",
        f"- 允许发布 GitHub Issue：{_display(decision.publication_allowed)}",
        f"- 原因：{decision.reason}",
        "",
    ]
    return "\n".join(lines)


def write_triage_draft(path: Path, content: str) -> None:
    if path.exists():
        raise FileExistsError(f"output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def load_sanitized_event(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON at line {exc.lineno}, column {exc.colno}") from exc
    if not isinstance(payload, dict):
        raise ValueError("input JSON must contain one object")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a local triage Issue from a sanitized event.")
    parser.add_argument("input", type=Path, help="Sanitized Kibana event JSON.")
    parser.add_argument("--output", type=Path, required=True, help="Local Markdown draft path.")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = load_sanitized_event(args.input)
        decision = evaluate_event(payload)
        if decision.state == "ignored_non_error":
            print(f"skipped: {decision.reason}")
            return 3
        if decision.state == "blocked":
            print(f"blocked: {decision.reason}", file=sys.stderr)
            return 4
        write_triage_draft(args.output, render_triage_markdown(payload))
    except (FileExistsError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
