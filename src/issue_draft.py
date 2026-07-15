"""Generate a local Markdown Issue draft from a validated intake JSON file."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

from src.issue_intake import IntakeRecord, load_intake


SOURCE_LABELS = {
    "manual": "人工提交",
    "jira": "Jira 工单",
    "kibana": "日志或监控告警",
}
AUTOMATION_LABELS = {
    "triage_only": "仅允许自动分类和补充信息",
    "analysis_only": "允许生成分析与修复建议",
    "draft_pr": "允许创建草稿 PR，必须人工审核",
    "manual_only": "仅人工处理",
}


class DuplicateInputError(ValueError):
    pass


class DraftStore:
    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> Dict[str, Dict[str, str]]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid state file: {self.path}") from exc
        records = payload.get("records", {}) if isinstance(payload, dict) else {}
        if not isinstance(records, dict):
            raise ValueError(f"invalid state file: {self.path}")
        return records

    def contains(self, key: str) -> bool:
        return key in self._load()

    def mark(self, record: IntakeRecord, output: Path) -> None:
        records = self._load()
        records[record.deduplication_key] = {
            "source_type": record.source_type,
            "source_reference": record.source_reference,
            "output": str(output),
        }
        _atomic_write_json(self.path, {"version": 1, "records": records})


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _atomic_write_json(path: Path, payload: Dict[str, object]) -> None:
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    _atomic_write_text(path, content)


def _lines(values: Dict[str, str]) -> str:
    return "\n".join(f"{label}：{value or '待确认'}" for label, value in values.items())


def _numbered(items: List[str]) -> str:
    return "\n".join(f"{index}. {item}" for index, item in enumerate(items, start=1))


def _checklist(items: List[str]) -> str:
    return "\n".join(f"- [ ] {item}" for item in items)


def _code_block(value: str) -> str:
    longest = max((len(match) for match in re.findall(r"`+", value)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{value or '无'}\n{fence}"


def render_markdown(record: IntakeRecord) -> str:
    source = _lines(
        {
            "输入来源": SOURCE_LABELS[record.source_type],
            "来源编号": record.source_reference,
            "来源链接": record.source_url or "无",
            "Jira Project Key": record.project_key or "无",
        }
    )
    target = _lines(
        {
            "产品或业务线": record.target.product,
            "GitHub 仓库": record.target.repository,
            "服务或应用": record.target.service,
            "模块或页面": record.target.module,
            "类 / 方法 / 任务名称": record.target.code_object,
            "责任团队或维护人": record.target.owner,
        }
    )
    problem = _lines(
        {
            "背景": record.problem.background,
            "当前行为": record.problem.current_behavior,
            "期望行为": record.problem.expected_behavior,
            "首次发现时间": record.problem.first_observed_at,
        }
    )
    interface = _lines(
        {
            "协议": record.interface.protocol,
            "方法": record.interface.method,
            "接口路径或 Topic": record.interface.path_or_topic,
            "上游调用方": record.interface.upstream,
            "下游依赖": record.interface.downstream,
        }
    )
    interface += "\n\n请求参数或消息体：\n" + _code_block(record.interface.request_sample)
    interface += "\n\n实际响应：\n" + _code_block(record.interface.actual_response)
    interface += "\n\n期望响应：\n" + _code_block(record.interface.expected_response)
    reproduction = _lines(
        {
            "前置条件": record.reproduction.preconditions,
            "发生频率": record.reproduction.frequency,
            "是否稳定复现": record.reproduction.reproducible,
            "临时规避方式": record.reproduction.workaround,
        }
    )
    reproduction += "\n\n" + _numbered(record.reproduction.steps)
    error = _code_block(
        _lines(
            {
                "错误码": record.error.error_code,
                "异常类型": record.error.exception_type,
                "错误消息": record.error.message,
                "堆栈信息": record.error.stack_trace,
                "相关日志片段": record.error.log_excerpt,
            }
        )
    )
    runtime = _lines(
        {
            "环境": record.runtime.environment,
            "版本": record.runtime.version,
            "Commit SHA": record.runtime.commit_sha,
            "镜像 Tag": record.runtime.image_tag,
            "部署区域": record.runtime.region,
            "集群": record.runtime.cluster,
            "节点": record.runtime.node,
            "操作系统 / 浏览器 / 设备": record.runtime.os_or_device,
            "发生时间及时区": record.runtime.occurred_at,
            "Trace ID": record.runtime.trace_id,
            "Request ID": record.runtime.request_id,
            "Session / Job ID": record.runtime.session_or_job_id,
        }
    )
    attachments = "\n".join(f"- {item}" for item in record.attachments) or "无"
    impact = _lines(
        {
            "受影响用户 / 客户 / 租户": record.impact.affected_subjects,
            "受影响功能或业务流程": record.impact.affected_flow,
            "影响数量或比例": record.impact.quantity_or_ratio,
            "是否影响数据正确性": record.impact.data_correctness,
            "是否影响资金、权限、隐私或合规": record.impact.regulated_areas,
            "业务损失或潜在风险": record.impact.business_risk,
        }
    )

    sections = [
        ("输入来源", source),
        ("工作项类型", record.request_type),
        ("严重程度", record.severity),
        ("目标对象", target),
        ("问题描述与期望结果", problem),
        ("接口与调用链", interface),
        ("复现步骤与触发条件", reproduction),
        ("报错信息与日志", error),
        ("运行环境与关联标识", runtime),
        ("截图、录屏与附件", attachments),
        ("影响范围", impact),
        ("验收标准", _checklist(record.acceptance_criteria)),
        ("自动化处理范围", AUTOMATION_LABELS[record.automation_scope]),
        ("数据安全确认", "- [x] 输入已完成脱敏，可用于本地草稿生成。"),
    ]
    body = "\n\n".join(f"## {heading}\n\n{content}" for heading, content in sections)
    return f"# [待分诊] {record.summary}\n\n{body}\n"


def generate_local_draft(record: IntakeRecord, output: Path, state_file: Path) -> Path:
    validation = record.validate()
    if not validation.valid:
        raise ValueError("; ".join(validation.errors))
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")

    store = DraftStore(state_file)
    if store.contains(record.deduplication_key):
        raise DuplicateInputError(
            f"duplicate input: {record.source_type}:{record.source_reference}"
        )

    _atomic_write_text(output, render_markdown(record))
    store.mark(record, output)
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a validated local Issue Markdown draft.")
    parser.add_argument("input", type=Path, help="Path to one intake JSON object.")
    parser.add_argument("--output", type=Path, required=True, help="Local Markdown output path.")
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path(".issue-draft-state.json"),
        help="Local deduplication state file.",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        record = load_intake(args.input)
        for warning in record.validate().warnings:
            print(f"warning: {warning}", file=sys.stderr)
        output = generate_local_draft(record, args.output, args.state_file)
    except DuplicateInputError as exc:
        print(f"duplicate: {exc}", file=sys.stderr)
        return 3
    except (FileExistsError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
