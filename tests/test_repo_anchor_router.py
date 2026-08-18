import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import repo_anchor_router


class ExtractAnchorsTest(unittest.TestCase):
    def test_extracts_camel_path_segment_first(self):
        text = "Throws an exception when processing request : request_path=/v3/api/aiAssistant/chat"
        anchors = repo_anchor_router.extract_anchors(text, [], [])
        self.assertEqual(anchors[0], "aiAssistant")
        self.assertNotIn("v3", anchors)

    def test_skips_redacted_and_generic_segments(self):
        text = "request_path=/v1/api/pack/category/[REDACTED:path_segment]/resources"
        anchors = repo_anchor_router.extract_anchors(text, [], [])
        self.assertNotIn("[REDACTED:path_segment]", anchors)
        self.assertNotIn("api", anchors)

    def test_extracts_stack_frame_class(self):
        text = "java.net.SocketTimeoutException\n\tat com.kikatech.aiapp.module.cend.CendController.list(CendController.java:42)"
        anchors = repo_anchor_router.extract_anchors(text, [], [])
        self.assertIn("CendController", anchors)

    def test_extracts_service_name(self):
        anchors = repo_anchor_router.extract_anchors("boom", [], ["backend-wallpaper"])
        self.assertIn("backend-wallpaper", anchors)

    def test_caps_anchor_count(self):
        text = " ".join(f"anchorItem{i}X" for i in range(10))
        anchors = repo_anchor_router.extract_anchors(text, [], [])
        self.assertLessEqual(len(anchors), repo_anchor_router.MAX_ANCHORS)


class DecideTest(unittest.TestCase):
    def test_unique_winner_routes(self):
        hits = {"aiAssistant": {"KikaTech/backend-aicompanion": {"score": 9.0, "impl": 3}}}
        decision = repo_anchor_router.decide(hits, ["KikaTech/backend-aicompanion"])
        self.assertIsNotNone(decision)
        self.assertEqual(decision["repository"], "KikaTech/backend-aicompanion")
        self.assertEqual(decision["anchor"], "aiAssistant")

    def test_tie_returns_none(self):
        hits = {"wallpaper": {
            "KikaTech/a": {"score": 6.0, "impl": 2},
            "KikaTech/b": {"score": 6.0, "impl": 2},
        }}
        self.assertIsNone(repo_anchor_router.decide(hits, ["KikaTech/a", "KikaTech/b"]))

    def test_unauthorized_repo_ignored(self):
        hits = {"x": {"elsewhere/repo": {"score": 9.0, "impl": 3}}}
        self.assertIsNone(repo_anchor_router.decide(hits, ["KikaTech/a"]))

    def test_no_hits_returns_none(self):
        self.assertIsNone(repo_anchor_router.decide({"x": {}}, ["KikaTech/a"]))

    def test_config_only_evidence_rejected(self):
        # 只有配置/文档提到服务名（典型调用方），证据太弱不路由
        hits = {"backend-apple-iap": {"KikaTech/a": {"score": 2.0, "impl": 0}}}
        self.assertIsNone(repo_anchor_router.decide(hits, ["KikaTech/a"]))

    def test_low_score_below_threshold_rejected(self):
        # 只有一个实现文件命中，孤证不立
        hits = {"x": {"KikaTech/a": {"score": 3.0, "impl": 1}}}
        self.assertIsNone(repo_anchor_router.decide(hits, ["KikaTech/a"]))


class PathWeightTest(unittest.TestCase):
    def test_implementation_code_scores_highest(self):
        w = repo_anchor_router._path_weight("src/main/java/com/x/service/UserServiceImpl.java")
        self.assertEqual(w, repo_anchor_router.WEIGHT_IMPLEMENTATION)

    def test_plain_source_scores_middle(self):
        w = repo_anchor_router._path_weight("src/main/java/com/x/util/DateUtil.java")
        self.assertEqual(w, repo_anchor_router.WEIGHT_SOURCE)

    def test_resources_config_scores_low(self):
        w = repo_anchor_router._path_weight("src/main/resources/prod/application-prod.yml")
        self.assertEqual(w, repo_anchor_router.WEIGHT_CONFIG)

    def test_config_class_scores_low(self):
        w = repo_anchor_router._path_weight("src/main/java/com/x/config/XxlJobConfig.java")
        self.assertEqual(w, repo_anchor_router.WEIGHT_CONFIG)

    def test_markdown_doc_scores_lowest(self):
        w = repo_anchor_router._path_weight("docs/topic-backend-design.md")
        self.assertEqual(w, repo_anchor_router.WEIGHT_DOC)


class RouteIncidentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        scope = {
            "schema_version": "repository-search-scope/v1",
            "repositories": [
                {"repository": "KikaTech/backend-aicompanion", "enabled": True},
                {"repository": "KikaTech/kika-global-studio", "enabled": True},
                {"repository": "wzf12400/ai-pr-sandbox", "enabled": False},
            ],
        }
        self.scope_path = Path(self.tmp.name) / "scope.json"
        self.scope_path.write_text(json.dumps(scope), encoding="utf-8")
        self.cache_path = Path(self.tmp.name) / "cache.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _fake_response(self, items):
        # items: [(repo_full_name, file_path), ...]
        payload = {
            "items": [
                {"repository": {"full_name": repo}, "path": path}
                for repo, path in items
            ]
        }

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(payload).encode()

        return _Resp()

    IMPL_ITEMS = [
        ("KikaTech/backend-aicompanion", "src/main/java/com/x/module/ai/AiAssistantService.java"),
        ("KikaTech/backend-aicompanion", "src/main/java/com/x/module/ai/AiAssistantController.java"),
        ("KikaTech/backend-aicompanion", "src/main/java/com/x/module/ai/AiAssistantMapper.java"),
    ]

    def test_routes_by_code_search(self):
        with mock.patch.object(
            repo_anchor_router.urllib.request, "urlopen",
            return_value=self._fake_response(self.IMPL_ITEMS),
        ):
            decision = repo_anchor_router.route_incident(
                "Throws an exception : request_path=/v3/api/aiAssistant/chat",
                [], [], "token",
                scope_path=self.scope_path, cache_path=self.cache_path,
            )
        self.assertIsNotNone(decision)
        self.assertEqual(decision["repository"], "KikaTech/backend-aicompanion")

    def test_config_only_hits_do_not_route(self):
        items = [
            ("KikaTech/backend-aicompanion", "src/main/resources/application-prod.yml"),
            ("KikaTech/backend-aicompanion", "docs/design.md"),
        ]
        with mock.patch.object(
            repo_anchor_router.urllib.request, "urlopen",
            return_value=self._fake_response(items),
        ):
            decision = repo_anchor_router.route_incident(
                "Throws an exception : request_path=/v3/api/aiAssistant/chat",
                [], [], "token",
                scope_path=self.scope_path, cache_path=self.cache_path,
            )
        self.assertIsNone(decision)

    def test_second_call_uses_cache_without_http(self):
        with mock.patch.object(
            repo_anchor_router.urllib.request, "urlopen",
            return_value=self._fake_response(self.IMPL_ITEMS),
        ) as http:
            repo_anchor_router.route_incident(
                "request_path=/v3/api/aiAssistant/chat", [], [], "token",
                scope_path=self.scope_path, cache_path=self.cache_path,
            )
        with mock.patch.object(
            repo_anchor_router.urllib.request, "urlopen",
            side_effect=AssertionError("should not hit network"),
        ):
            decision = repo_anchor_router.route_incident(
                "request_path=/v3/api/aiAssistant/chat", [], [], "token",
                scope_path=self.scope_path, cache_path=self.cache_path,
            )
        self.assertIsNotNone(decision)

    def test_rate_limit_fails_open(self):
        import urllib.error
        err = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
        with mock.patch.object(
            repo_anchor_router.urllib.request, "urlopen", side_effect=err
        ):
            decision = repo_anchor_router.route_incident(
                "request_path=/v3/api/aiAssistant/chat", [], [], "token",
                scope_path=self.scope_path, cache_path=self.cache_path,
            )
        self.assertIsNone(decision)

    def test_empty_token_returns_none(self):
        self.assertIsNone(
            repo_anchor_router.route_incident("anything", [], [], "",
                                              scope_path=self.scope_path,
                                              cache_path=self.cache_path)
        )


if __name__ == "__main__":
    unittest.main()
