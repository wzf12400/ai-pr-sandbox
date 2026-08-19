"""Tests for the Jira connector's deterministic mapping and routing logic."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import jira_connector


FRONTEND = {
    "repository": "org/app-web",
    "components": ["前端", "H5"],
    "labels": ["fe"],
    "keywords": ["页面", "按钮", "样式"],
}
BACKEND = {
    "repository": "org/app-server",
    "components": ["服务端"],
    "labels": ["be"],
    "keywords": ["接口", "超时", "数据库"],
}
TWO_REPO_PROJECT = {
    "enabled": True,
    "jql_extra": "issuetype = Bug",
    "severity_map": {"High": "S2"},
    "repositories": [FRONTEND, BACKEND],
    "auto_dispatch": False,
}
SINGLE_REPO_PROJECT = {
    "enabled": True,
    "repositories": [FRONTEND],
    "auto_dispatch": False,
}


def make_issue(key="APP-1", summary="按钮点击无响应", description="",
               components=None, labels=None, priority="High", issue_type="Bug"):
    return {
        "key": key,
        "fields": {
            "summary": summary,
            "description": description,
            "components": [{"name": c} for c in (components or [])],
            "labels": labels or [],
            "priority": {"name": priority},
            "issuetype": {"name": issue_type},
            "project": {"key": "APP", "name": "App"},
            "created": "2026-08-14T10:00:00.000+0800",
            "updated": "2026-08-14T11:00:00.000+0800",
            "comment": {"comments": []},
            "attachment": [],
        },
    }


class ConfigValidationTest(unittest.TestCase):
    def write_config(self, payload):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "jira-projects.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_rejects_wrong_schema_version(self):
        path = self.write_config({"schema_version": "nope", "projects": {}})
        with self.assertRaises(ValueError):
            jira_connector.load_config(path)

    def test_rejects_duplicate_repository_binding(self):
        payload = {
            "schema_version": "jira-projects/v1",
            "projects": {"APP": {"repositories": [FRONTEND, dict(FRONTEND)]}},
        }
        with self.assertRaises(ValueError):
            jira_connector.load_config(self.write_config(payload))

    def test_rejects_invalid_project_key(self):
        payload = {
            "schema_version": "jira-projects/v1",
            "projects": {"bad key": {"repositories": [FRONTEND]}},
        }
        with self.assertRaises(ValueError):
            jira_connector.load_config(self.write_config(payload))

    def test_rejects_invalid_repository_format(self):
        bad = dict(FRONTEND, repository="no-slash")
        payload = {
            "schema_version": "jira-projects/v1",
            "projects": {"APP": {"repositories": [bad]}},
        }
        with self.assertRaises(ValueError):
            jira_connector.load_config(self.write_config(payload))

    def test_accepts_valid_config(self):
        payload = {
            "schema_version": "jira-projects/v1",
            "projects": {"APP": dict(TWO_REPO_PROJECT)},
        }
        config = jira_connector.load_config(self.write_config(payload))
        self.assertIn("APP", config["projects"])


class RouteIssueTest(unittest.TestCase):
    def test_single_repository_binding_resolves_immediately(self):
        decision = jira_connector.route_issue(make_issue(), SINGLE_REPO_PROJECT)
        self.assertEqual(decision.status, "RESOLVED")
        self.assertEqual(decision.repository, "org/app-web")

    def test_strict_matching_disables_single_binding_shortcut(self):
        project = dict(SINGLE_REPO_PROJECT, strict_matching=True)
        # 标题不含任何关键词 → 严格模式下不再直通
        decision = jira_connector.route_issue(make_issue(summary="系统有问题"), project)
        self.assertEqual(decision.status, "NEEDS_CONTEXT")
        # 关键词命中两个以上（达阈值）仍可解析
        decision = jira_connector.route_issue(
            make_issue(summary="页面按钮样式异常"), project
        )
        self.assertEqual(decision.status, "RESOLVED")
        self.assertEqual(decision.repository, "org/app-web")

    def test_component_binding_wins(self):
        issue = make_issue(summary="随便什么标题", components=["服务端"])
        decision = jira_connector.route_issue(issue, TWO_REPO_PROJECT)
        self.assertEqual(decision.status, "RESOLVED")
        self.assertEqual(decision.repository, "org/app-server")
        self.assertIn("服务端", decision.basis)

    def test_label_binding_wins(self):
        issue = make_issue(summary="随便什么标题", labels=["fe"])
        decision = jira_connector.route_issue(issue, TWO_REPO_PROJECT)
        self.assertEqual(decision.status, "RESOLVED")
        self.assertEqual(decision.repository, "org/app-web")

    def test_conflicting_components_need_context(self):
        issue = make_issue(summary="随便", components=["前端", "服务端"])
        decision = jira_connector.route_issue(issue, TWO_REPO_PROJECT)
        self.assertEqual(decision.status, "NEEDS_CONTEXT")
        self.assertIn("conflicting", decision.basis)

    def test_binding_and_keyword_conflict_needs_context(self):
        issue = make_issue(
            summary="接口超时",
            description="数据库查询超时，接口返回 504",
            components=["前端"],
        )
        decision = jira_connector.route_issue(issue, TWO_REPO_PROJECT)
        self.assertEqual(decision.status, "NEEDS_CONTEXT")
        self.assertIn("keywords say", decision.basis)

    def test_keywords_resolve_with_margin(self):
        issue = make_issue(summary="接口超时", description="数据库慢查询导致接口超时")
        decision = jira_connector.route_issue(issue, TWO_REPO_PROJECT)
        self.assertEqual(decision.status, "RESOLVED")
        self.assertEqual(decision.repository, "org/app-server")
        self.assertGreaterEqual(decision.confidence, 50)

    def test_keywords_ambiguous_when_margin_too_small(self):
        issue = make_issue(summary="接口异常", description="页面调接口失败")
        decision = jira_connector.route_issue(issue, TWO_REPO_PROJECT)
        self.assertEqual(decision.status, "NEEDS_CONTEXT")

    def test_no_signals_needs_context(self):
        issue = make_issue(summary="系统有问题", description="请看看")
        decision = jira_connector.route_issue(issue, TWO_REPO_PROJECT)
        self.assertEqual(decision.status, "NEEDS_CONTEXT")


class IntakeMappingTest(unittest.TestCase):
    def test_maps_core_fields(self):
        intake = jira_connector.issue_to_intake(
            make_issue(), "https://jira.example", TWO_REPO_PROJECT
        )
        self.assertEqual(intake["source_type"], "jira")
        self.assertEqual(intake["source_reference"], "APP-1")
        self.assertEqual(intake["source_url"], "https://jira.example/browse/APP-1")
        self.assertEqual(intake["request_type"], "Bug")
        self.assertEqual(intake["severity"], "S2")
        self.assertEqual(intake["data_safety_status"], "unreviewed")

    def test_unknown_priority_maps_to_unknown_severity(self):
        intake = jira_connector.issue_to_intake(
            make_issue(priority="Trivial"), "https://jira.example", TWO_REPO_PROJECT
        )
        self.assertEqual(intake["severity"], "Unknown")

    def test_attachments_are_metadata_only(self):
        issue = make_issue()
        issue["fields"]["attachment"] = [
            {"filename": "screenshot.png", "mimeType": "image/png", "content": "https://x/y"}
        ]
        intake = jira_connector.issue_to_intake(issue, "https://jira.example", TWO_REPO_PROJECT)
        self.assertEqual(intake["attachments"], ["screenshot.png (image/png)"])
        self.assertNotIn("https://x/y", json.dumps(intake["attachments"]))

    def test_request_type_mapping(self):
        self.assertEqual(jira_connector.map_request_type("Bug"), "Bug")
        self.assertEqual(jira_connector.map_request_type("缺陷"), "Bug")
        self.assertEqual(jira_connector.map_request_type("Story"), "Feature")
        self.assertEqual(jira_connector.map_request_type("新需求"), "Feature")
        self.assertEqual(jira_connector.map_request_type("Epic"), "Unknown")


class JqlAndWatermarkTest(unittest.TestCase):
    def test_build_jql_without_watermark(self):
        jql = jira_connector.build_jql("APP", TWO_REPO_PROJECT)
        self.assertIn("project = APP", jql)
        self.assertIn("issuetype = Bug", jql)
        self.assertTrue(jql.endswith("ORDER BY updated ASC"))
        self.assertNotIn("updated >=", jql)

    def test_build_jql_with_watermark(self):
        jql = jira_connector.build_jql("APP", TWO_REPO_PROJECT, "2026-08-14 10:00")
        self.assertIn('updated >= "2026-08-14 10:00"', jql)

    def test_parse_jira_time(self):
        epoch = jira_connector._parse_jira_time("2026-08-14T11:00:00.000+0800")
        self.assertGreater(epoch, 0)
        with self.assertRaises(ValueError):
            jira_connector._parse_jira_time("not a time")

    def test_issue_type_client_side_filter(self):
        project = dict(TWO_REPO_PROJECT, issue_types=["缺陷"])
        self.assertTrue(jira_connector._issue_type_allowed(make_issue(issue_type="缺陷"), project))
        self.assertFalse(jira_connector._issue_type_allowed(make_issue(issue_type="新需求"), project))
        # 未配置 issue_types 时全部放行
        self.assertTrue(jira_connector._issue_type_allowed(make_issue(issue_type="新需求"), TWO_REPO_PROJECT))


class FallbackRoutingTest(unittest.TestCase):
    """route_issue_with_fallbacks：确定性失败后的锚点/AI 兜底链。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache_path = Path(self.tmp.name) / "routing-cache.json"

    def _route(self, issue, project):
        return jira_connector.route_issue_with_fallbacks(
            issue, project, cache_path=self.cache_path
        )

    def test_resolved_deterministically_skips_fallbacks(self):
        issue = make_issue(components=["前端"])
        project = dict(TWO_REPO_PROJECT, ai_routing=True)
        with mock.patch.object(
            jira_connector, "_anchor_fallback",
            side_effect=AssertionError("should not run anchor stage"),
        ):
            decision = self._route(issue, project)
        self.assertEqual(decision.status, "RESOLVED")
        self.assertEqual(decision.repository, "org/app-web")

    def test_ai_fallback_resolves_when_enabled(self):
        issue = make_issue(summary="系统有问题", description="请看看")
        project = dict(TWO_REPO_PROJECT, ai_routing=True)
        ai_result = jira_connector.RouteDecision(
            "RESOLVED", repository="org/app-server",
            basis="ai classification (confidence 80): x", confidence=80,
        )
        with mock.patch.object(
            jira_connector, "_anchor_fallback", return_value=None
        ), mock.patch.object(jira_connector, "_ai_fallback", return_value=ai_result):
            decision = self._route(issue, project)
        self.assertEqual(decision.status, "RESOLVED")
        self.assertEqual(decision.repository, "org/app-server")
        self.assertEqual(decision.confidence, 80)

    def test_no_fallback_signal_stays_needs_context(self):
        issue = make_issue(summary="系统有问题", description="请看看")
        project = dict(TWO_REPO_PROJECT, ai_routing=True)
        with mock.patch.object(
            jira_connector, "_anchor_fallback", return_value=None
        ), mock.patch.object(jira_connector, "_ai_fallback", return_value=None):
            decision = self._route(issue, project)
        self.assertEqual(decision.status, "NEEDS_CONTEXT")

    def test_ai_fallback_disabled_without_project_flag(self):
        issue = make_issue(summary="系统有问题", description="请看看")
        # TWO_REPO_PROJECT 没有 ai_routing 字段 -> _ai_fallback 直接返回 None
        with mock.patch.object(
            jira_connector, "_anchor_fallback", return_value=None
        ), mock.patch.dict("os.environ", {
            "AI_BASE_URL": "https://x", "AI_API_KEY": "k"
        }):
            decision = self._route(issue, TWO_REPO_PROJECT)
        self.assertEqual(decision.status, "NEEDS_CONTEXT")

    def test_fallback_exception_never_propagates(self):
        issue = make_issue(summary="系统有问题", description="请看看")
        project = dict(TWO_REPO_PROJECT, ai_routing=True)
        with mock.patch.object(
            jira_connector, "_anchor_fallback", side_effect=RuntimeError("boom")
        ):
            decision = self._route(issue, project)
        self.assertEqual(decision.status, "NEEDS_CONTEXT")

    def test_anchor_fallback_runs_before_ai(self):
        anchor_result = jira_connector.RouteDecision(
            "RESOLVED", repository="org/app-server",
            basis="code anchor 'x' found", confidence=95,
        )
        issue = make_issue(summary="系统有问题", description="请看看")
        project = dict(TWO_REPO_PROJECT, ai_routing=True)
        with mock.patch.object(
            jira_connector, "_anchor_fallback", return_value=anchor_result
        ), mock.patch.object(
            jira_connector, "_ai_fallback",
            side_effect=AssertionError("AI must not run after anchor hit"),
        ):
            decision = self._route(issue, project)
        self.assertEqual(decision.repository, "org/app-server")
        self.assertEqual(decision.confidence, 95)

    def _routed_once(self, tmp_dir, summary="系统有问题"):
        """跑第一次路由并写入缓存，返回 (issue, project, cache_path)。"""
        cache_path = Path(tmp_dir) / "routing-cache.json"
        project = dict(TWO_REPO_PROJECT, ai_routing=True)
        issue = make_issue(summary=summary, description="请看看")
        ai_result = jira_connector.RouteDecision(
            "RESOLVED", repository="org/app-server",
            basis="ai classification (confidence 80): x", confidence=80,
        )
        with mock.patch.object(
            jira_connector, "_anchor_fallback", return_value=None
        ), mock.patch.object(jira_connector, "_ai_fallback", return_value=ai_result):
            first = jira_connector.route_issue_with_fallbacks(
                issue, project, cache_path=cache_path
            )
        self.assertEqual(first.repository, "org/app-server")
        return issue, project, cache_path

    def test_second_run_uses_cache_without_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            issue, project, cache_path = self._routed_once(tmp)
            with mock.patch.object(
                jira_connector, "_anchor_fallback",
                side_effect=AssertionError("cached: must not re-run anchor"),
            ), mock.patch.object(
                jira_connector, "_ai_fallback",
                side_effect=AssertionError("cached: must not re-run AI"),
            ):
                second = jira_connector.route_issue_with_fallbacks(
                    issue, project, cache_path=cache_path
                )
            self.assertEqual(second.repository, "org/app-server")
            self.assertEqual(second.confidence, 80)

    def test_changed_summary_invalidates_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            _issue, project, cache_path = self._routed_once(tmp)
            changed = make_issue(summary="完全另一个问题", description="请看看")
            with mock.patch.object(
                jira_connector, "_anchor_fallback", return_value=None
            ), mock.patch.object(
                jira_connector, "_ai_fallback", return_value=None
            ) as ai_spy:
                decision = jira_connector.route_issue_with_fallbacks(
                    changed, project, cache_path=cache_path
                )
            self.assertEqual(decision.status, "NEEDS_CONTEXT")
            self.assertTrue(ai_spy.called)  # 重新走了兜底链而不是用旧缓存

    def test_cache_dropped_when_repo_unbound(self):
        with tempfile.TemporaryDirectory() as tmp:
            issue, _project, cache_path = self._routed_once(tmp)
            # 项目绑定变了：org/app-server 已不在候选里（strict_matching 防止
            # 单仓捷径直接拦截，确保走到缓存校验这一步）
            new_project = dict(TWO_REPO_PROJECT, ai_routing=True,
                               strict_matching=True, repositories=[FRONTEND])
            with mock.patch.object(
                jira_connector, "_anchor_fallback", return_value=None
            ), mock.patch.object(
                jira_connector, "_ai_fallback", return_value=None
            ) as ai_spy:
                decision = jira_connector.route_issue_with_fallbacks(
                    issue, new_project, cache_path=cache_path
                )
            # 旧缓存的仓库已解绑，结论作废，重新走兜底（无信号 -> 待人工）
            self.assertEqual(decision.status, "NEEDS_CONTEXT")
            self.assertTrue(ai_spy.called)


if __name__ == "__main__":
    unittest.main()
