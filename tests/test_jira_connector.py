"""Tests for the Jira connector's deterministic mapping and routing logic."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
