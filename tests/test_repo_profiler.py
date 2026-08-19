"""Tests for repo_profiler.classify_issue（AI 画像分类，含多仓判定）。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import repo_profiler


AI = {"base_url": "https://ai.example", "api_key": "k", "model": "m", "safety_id": "s"}
WEB = "org/app-web"
SERVER = "org/app-server"
CANDIDATES = [WEB, SERVER]


def _profiles_file(tmp_dir: str) -> Path:
    path = Path(tmp_dir) / "profiles.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "profiles": {
                    WEB: {
                        "repository": WEB,
                        "summary": "前端仓库",
                        "keywords": ["页面"],
                        "modules": ["播放器"],
                    },
                    SERVER: {
                        "repository": SERVER,
                        "summary": "后端仓库",
                        "keywords": ["接口"],
                        "modules": ["API"],
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _classify(tmp_dir: str, ai_reply: dict, min_confidence: int = 70):
    with mock.patch.object(
        repo_profiler, "_chat", return_value=json.dumps(ai_reply, ensure_ascii=False)
    ):
        return repo_profiler.classify_issue(
            "APP",
            "标题",
            "描述",
            CANDIDATES,
            AI,
            profiles_path=_profiles_file(tmp_dir),
            min_confidence=min_confidence,
        )


class ClassifyIssueTest(unittest.TestCase):
    def test_single_match_keeps_legacy_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _classify(
                tmp,
                {
                    "repositories": [
                        {"repository": WEB, "confidence": 90, "reason": "界面改动"}
                    ]
                },
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["repository"], WEB)
        self.assertEqual(result["confidence"], 90)
        self.assertEqual([m["repository"] for m in result["matches"]], [WEB])
        self.assertIn("confidence 90", result["basis"])

    def test_multi_match_returns_all_sorted_by_confidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _classify(
                tmp,
                {
                    "repositories": [
                        {"repository": WEB, "confidence": 80, "reason": "播放界面"},
                        {"repository": SERVER, "confidence": 85, "reason": "列表接口"},
                    ]
                },
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["repository"], SERVER)  # 主仓取置信度最高
        self.assertEqual(
            [m["repository"] for m in result["matches"]], [SERVER, WEB]
        )
        self.assertIn("multi-repo", result["basis"])

    def test_below_threshold_matches_are_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _classify(
                tmp,
                {
                    "repositories": [
                        {"repository": WEB, "confidence": 85, "reason": "界面"},
                        {"repository": SERVER, "confidence": 50, "reason": "拿不准"},
                    ]
                },
            )
        self.assertIsNotNone(result)
        self.assertEqual([m["repository"] for m in result["matches"]], [WEB])

    def test_all_below_threshold_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _classify(
                tmp,
                {"repositories": [{"repository": WEB, "confidence": 40, "reason": "猜的"}]},
            )
        self.assertIsNone(result)

    def test_unknown_or_duplicate_repos_are_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _classify(
                tmp,
                {
                    "repositories": [
                        {"repository": "org/other", "confidence": 99, "reason": "不在候选"},
                        {"repository": WEB, "confidence": 88, "reason": "界面"},
                        {"repository": WEB, "confidence": 70, "reason": "重复"},
                    ]
                },
            )
        self.assertIsNotNone(result)
        self.assertEqual([m["repository"] for m in result["matches"]], [WEB])
        self.assertEqual(result["confidence"], 88)

    def test_legacy_single_repo_format_still_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _classify(
                tmp,
                {"repository": WEB, "confidence": 90, "reason": "旧格式"},
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["repository"], WEB)
        self.assertEqual([m["repository"] for m in result["matches"]], [WEB])

    def test_empty_repositories_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _classify(tmp, {"repositories": []})
        self.assertIsNone(result)

    def test_ai_failure_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(repo_profiler, "_chat", return_value=None):
                result = repo_profiler.classify_issue(
                    "APP", "标题", "描述", CANDIDATES, AI,
                    profiles_path=_profiles_file(tmp),
                )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
