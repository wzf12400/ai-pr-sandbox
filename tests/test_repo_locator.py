import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from src.repo_locator import extract_terms, load_github_issue, locate_issue


class RepoLocatorTest(unittest.TestCase):
    def test_extracts_code_identifiers_without_whole_issue_context(self) -> None:
        code_terms, words = extract_terms(
            "WidgetController regression",
            "`WidgetController.pageResourcesNew` raises `ValueError` in the parent class.",
        )

        self.assertIn("WidgetController.pageResourcesNew", code_terms)
        self.assertIn("ValueError", code_terms)
        self.assertIn("widget", words)

    def test_python_parent_without_slots_is_ranked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "package").mkdir()
            (repo / "package" / "base.py").write_text(
                "class Printable:\n    def show(self):\n        return 'x'\n",
                encoding="utf-8",
            )
            (repo / "package" / "model.py").write_text(
                "from .base import Printable\n\n"
                "class Basic(Printable):\n    __slots__ = ()\n\n"
                "class Symbol(Basic):\n    __slots__ = ('name',)\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "init", str(repo)], check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)

            result = locate_issue(
                repo,
                "Symbol instances have __dict__",
                "`Symbol` has `__dict__`; some parent class stopped defining `__slots__`.",
            )

            self.assertEqual("package/base.py", result["candidates"][0]["path"])
            self.assertTrue(any("结构异常" in reason for reason in result["candidates"][0]["reasons"]))

    def test_rejects_pull_request_api_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "issue.json"
            path.write_text(
                json.dumps({"title": "x", "body": "y", "pull_request": {}}),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_github_issue(path)

    def test_secret_in_issue_text_is_redacted_without_echoing_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "module.py").write_text("def run():\n    return True\n", encoding="utf-8")
            secret = "quoted-secret-token-value"

            result = locate_issue(
                repo,
                "Authentication failure",
                f"Authorization: Bearer {secret}",
            )
            serialized = json.dumps(result)

            self.assertNotIn(secret, serialized)
            self.assertEqual("passed_with_redactions", result["query"]["safety"]["status"])

    def test_symlinked_source_file_is_not_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            outside = root / "outside.py"
            outside.write_text("class SecretTarget:\n    pass\n", encoding="utf-8")
            (repo / "linked.py").symlink_to(outside)

            result = locate_issue(repo, "SecretTarget failure", "`SecretTarget` fails")

            self.assertEqual(0, result["index"]["source_files"])

    def test_public_notebook_frame_is_normalized_not_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "module.py").write_text("class Symbol:\n    pass\n", encoding="utf-8")

            result = locate_issue(
                repo,
                "Symbol failure",
                "<ipython-input-3-e2060d5eec73> in <module>",
            )

            self.assertIn("public_notebook_frame", result["query"]["safety"]["handled_categories"])


if __name__ == "__main__":
    unittest.main()
