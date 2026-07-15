import json
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from src.phase_one import main


ROOT = Path(__file__).resolve().parents[1]
TEST_KEY = "local-test-hmac-key-that-is-at-least-32-bytes"


class PhaseOneTest(unittest.TestCase):
    def test_info_event_is_sanitized_then_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sanitized = root / "event.json"
            draft = root / "issue.md"
            with patch.dict(os.environ, {"LOG_SANITIZER_HMAC_KEY": TEST_KEY}):
                with redirect_stdout(io.StringIO()):
                    code = main(
                        [
                            "kibana",
                            str(ROOT / "examples" / "kibana_raw.json"),
                            "--sanitized-output",
                            str(sanitized),
                            "--draft-output",
                            str(draft),
                        ]
                    )

            self.assertEqual(3, code)
            self.assertTrue(sanitized.exists())
            self.assertFalse(draft.exists())

    def test_error_event_runs_from_raw_input_to_guarded_draft(self) -> None:
        payload = json.loads((ROOT / "examples" / "kibana_raw.json").read_text(encoding="utf-8"))
        payload["_source"]["message"] = payload["_source"]["message"].replace(" INFO ", " ERROR ")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw.json"
            raw.write_text(json.dumps(payload), encoding="utf-8")
            sanitized = root / "event.json"
            draft = root / "issue.md"
            with patch.dict(os.environ, {"LOG_SANITIZER_HMAC_KEY": TEST_KEY}):
                with redirect_stdout(io.StringIO()):
                    code = main(
                        [
                            "kibana",
                            str(raw),
                            "--sanitized-output",
                            str(sanitized),
                            "--draft-output",
                            str(draft),
                        ]
                    )

            self.assertEqual(0, code)
            self.assertTrue(sanitized.exists())
            self.assertIn("## 报错", draft.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
