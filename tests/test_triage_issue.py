import copy
import json
import unittest
from pathlib import Path

from src.kibana_sanitizer import sanitize_hit
from src.triage_issue import evaluate_event, render_triage_markdown


ROOT = Path(__file__).resolve().parents[1]
TEST_KEY = b"local-test-hmac-key-that-is-at-least-32-bytes"


def sanitized_error():
    payload = json.loads((ROOT / "examples" / "kibana_raw.json").read_text(encoding="utf-8"))
    payload["_source"]["message"] = payload["_source"]["message"].replace(" INFO ", " ERROR ")
    return sanitize_hit(payload, TEST_KEY)


class TriageIssueTest(unittest.TestCase):
    def test_info_event_is_filtered_before_draft_generation(self) -> None:
        payload = json.loads((ROOT / "examples" / "kibana_raw.json").read_text(encoding="utf-8"))
        result = sanitize_hit(payload, TEST_KEY)

        decision = evaluate_event(result)

        self.assertEqual("ignored_non_error", decision.state)
        self.assertFalse(decision.publication_allowed)

    def test_error_event_creates_guarded_draft_without_inventing_context(self) -> None:
        result = sanitized_error()

        markdown = render_triage_markdown(result)

        self.assertIn("## 对象", markdown)
        self.assertIn("## 接口", markdown)
        self.assertIn("## 报错", markdown)
        self.assertIn("接口路径或 Topic：未从日志获得", markdown)
        self.assertIn("- [ ] 期望行为", markdown)
        self.assertIn("允许发布 GitHub Issue：否", markdown)
        self.assertNotIn("synthetic-api-key-value-that-must-not-leak", markdown)

    def test_blocked_sanitization_stays_blocked(self) -> None:
        result = sanitized_error()
        result = copy.deepcopy(result)
        result["sanitization"]["ai_allowed"] = False

        decision = evaluate_event(result)

        self.assertEqual("blocked", decision.state)
        self.assertFalse(decision.publication_allowed)


if __name__ == "__main__":
    unittest.main()
