import json
import tempfile
import unittest
from pathlib import Path

from src.issue_draft import DuplicateInputError, generate_local_draft, render_markdown
from src.issue_intake import IntakeRecord, load_intake


ROOT = Path(__file__).resolve().parents[1]


class IssueDraftTest(unittest.TestCase):
    def test_all_example_sources_validate_and_render(self) -> None:
        for name in ("manual", "jira", "kibana"):
            with self.subTest(source=name):
                record = load_intake(ROOT / "examples" / f"{name}.json")
                self.assertTrue(record.validate().valid, record.validate().errors)
                markdown = render_markdown(record)
                self.assertIn("## 来源与分类", markdown)
                self.assertIn("## 目标对象", markdown)
                self.assertIn("## 接口与调用链", markdown)
                self.assertIn("## 报错与关键证据", markdown)
                self.assertIn("## 验收标准与处理权限", markdown)
                self.assertEqual(8, markdown.count("## "))
                self.assertNotIn("待确认", markdown)

    def test_missing_critical_field_stops_generation(self) -> None:
        payload = json.loads((ROOT / "examples" / "manual.json").read_text(encoding="utf-8"))
        payload["problem"]["expected_behavior"] = ""

        result = IntakeRecord.from_dict(payload).validate()

        self.assertFalse(result.valid)
        self.assertIn("problem.expected_behavior is required", result.errors)

    def test_log_excerpt_is_limited_to_fifty_lines(self) -> None:
        payload = json.loads((ROOT / "examples" / "kibana.json").read_text(encoding="utf-8"))
        payload["error"]["log_excerpt"] = "\n".join(f"line {index}" for index in range(51))

        result = IntakeRecord.from_dict(payload).validate()

        self.assertFalse(result.valid)
        self.assertIn("error.log_excerpt must not exceed 50 lines", result.errors)

    def test_interface_summary_has_a_context_limit(self) -> None:
        payload = json.loads((ROOT / "examples" / "jira.json").read_text(encoding="utf-8"))
        payload["interface"]["actual_response"] = "x" * 4001

        result = IntakeRecord.from_dict(payload).validate()

        self.assertFalse(result.valid)
        self.assertIn(
            "interface.actual_response must not exceed 4000 characters",
            result.errors,
        )

    def test_unreviewed_input_is_rejected(self) -> None:
        payload = json.loads((ROOT / "examples" / "jira.json").read_text(encoding="utf-8"))
        payload["data_safety_status"] = "unreviewed"

        result = IntakeRecord.from_dict(payload).validate()

        self.assertFalse(result.valid)
        self.assertIn("data_safety_status must be sanitized before draft generation", result.errors)

    def test_sensitive_value_is_reported_without_echoing_it(self) -> None:
        payload = json.loads((ROOT / "examples" / "kibana.json").read_text(encoding="utf-8"))
        secret = "token=company-secret-value"
        payload["error"]["log_excerpt"] = secret

        result = IntakeRecord.from_dict(payload).validate()

        self.assertFalse(result.valid)
        combined = " ".join(result.errors)
        self.assertIn("credential", combined)
        self.assertNotIn("company-secret-value", combined)

    def test_phone_number_is_rejected_without_false_positive_on_ids(self) -> None:
        payload = json.loads((ROOT / "examples" / "kibana.json").read_text(encoding="utf-8"))
        payload["error"]["log_excerpt"] = "caller=+8613812345678"

        result = IntakeRecord.from_dict(payload).validate()

        self.assertFalse(result.valid)
        self.assertTrue(any("phone number" in error for error in result.errors))

    def test_sensitive_value_in_unknown_field_is_still_rejected(self) -> None:
        payload = json.loads((ROOT / "examples" / "manual.json").read_text(encoding="utf-8"))
        payload["unexpected_metadata"] = {"authorization": "token=hidden-value"}

        result = IntakeRecord.from_dict(payload).validate()

        self.assertFalse(result.valid)
        self.assertTrue(any("credential" in error for error in result.errors))
        self.assertFalse(any("hidden-value" in error for error in result.errors))

    def test_same_source_record_cannot_generate_twice(self) -> None:
        record = load_intake(ROOT / "examples" / "manual.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.json"
            first_output = root / "first.md"
            second_output = root / "second.md"

            generate_local_draft(record, first_output, state)

            with self.assertRaises(DuplicateInputError):
                generate_local_draft(record, second_output, state)
            self.assertFalse(second_output.exists())

    def test_existing_output_is_not_overwritten(self) -> None:
        record = load_intake(ROOT / "examples" / "manual.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "existing.md"
            output.write_text("keep me", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                generate_local_draft(record, output, root / "state.json")
            self.assertEqual("keep me", output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
