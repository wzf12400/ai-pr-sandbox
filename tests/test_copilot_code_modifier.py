import json
import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.copilot_code_modifier import (
    ApprovedIssue,
    CopilotCLICodeModifier,
    IssueCodePolicy,
    ProcessOutput,
    evaluate_issue_approval,
    execute_issue_code_workflow,
    load_issue_code_policy,
    validate_changes,
)


REPOSITORY = "example-org/example-service"
ISSUE_URL = f"https://github.com/{REPOSITORY}/issues/17"
FINGERPRINT = hashlib.sha256(b"synthetic-approved-issue").hexdigest()


def policy_payload():
    return {
        "schema_version": "issue-code-policy/v1",
        "policy_id": "example-copilot-v1",
        "repository": REPOSITORY,
        "provider": "github-copilot-cli",
        "base_branch": "main",
        "branch_prefix": "codex/copilot",
        "required_labels": ["ai-code-approved"],
        "allowed_models": ["gpt-5.6-sol"],
        "default_model": "gpt-5.6-sol",
        "allowed_write_paths": ["src/**", "tests/**"],
        "blocked_write_paths": [".github/**", ".git/**", "deploy/**"],
        "test_commands": [
            ["python3", "-m", "unittest", "discover", "-s", "tests", "-v"]
        ],
        "limits": {
            "max_changed_files": 5,
            "max_added_lines": 100,
            "max_deleted_lines": 50,
            "copilot_timeout_seconds": 120,
            "test_timeout_seconds": 60,
        },
        "draft_pr_only": True,
        "auto_merge": False,
    }


def approved_issue(body_suffix=""):
    return ApprovedIssue(
        repository=REPOSITORY,
        number=17,
        url=ISSUE_URL,
        title="Add multiply support",
        body=(
            "Implement `multiply(left, right)` in `src/calculator.py` and add tests.\n\n"
            f"<!-- repository-issue-fingerprint/v1:{FINGERPRINT} -->"
            f"{body_suffix}"
        ),
        state="OPEN",
        labels=("ai-code-approved", "bug"),
        updated_at="2026-07-24T00:00:00Z",
    )


def initialize_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / ".github").mkdir()
    (repo / "src" / "calculator.py").write_text(
        "def add(left, right):\n    return left + right\n", encoding="utf-8"
    )
    (repo / "tests" / "test_calculator.py").write_text(
        "import unittest\n\n"
        "from src.calculator import add\n\n"
        "class TestCalculator(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(3, add(1, 2))\n",
        encoding="utf-8",
    )
    policy_path = repo / ".github" / "issue-code-policy.json"
    policy_path.write_text(json.dumps(policy_payload()), encoding="utf-8")
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test User"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", f"https://github.com/{REPOSITORY}.git"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", head],
        check=True,
    )
    return repo


class FakeIssueClient:
    def __init__(self, issues=None):
        self.issues = list(issues or [approved_issue()])
        self.calls = []

    def fetch(self, issue_url):
        self.calls.append(issue_url)
        if len(self.issues) > 1:
            return self.issues.pop(0)
        return self.issues[0]


class FakeModifier:
    def __init__(self, write_change=False, returncode=0, raise_error=False):
        self.write_change = write_change
        self.returncode = returncode
        self.raise_error = raise_error
        self.calls = []

    def version(self, repo):
        return "GitHub Copilot CLI test"

    def modify(self, repo, prompt, model, timeout_seconds):
        self.calls.append((repo, prompt, model, timeout_seconds))
        if self.raise_error:
            raise ValueError("synthetic Copilot failure")
        if self.write_change:
            (repo / "src" / "calculator.py").write_text(
                "def add(left, right):\n"
                "    return left + right\n\n"
                "def multiply(left, right):\n"
                "    return left * right\n",
                encoding="utf-8",
            )
            (repo / "tests" / "test_calculator.py").write_text(
                "import unittest\n\n"
                "from src.calculator import add, multiply\n\n"
                "class TestCalculator(unittest.TestCase):\n"
                "    def test_add(self):\n"
                "        self.assertEqual(3, add(1, 2))\n\n"
                "    def test_multiply(self):\n"
                "        self.assertEqual(6, multiply(2, 3))\n",
                encoding="utf-8",
            )
        return {
            "returncode": self.returncode,
            "stdout_sha256": "b" * 64,
            "stderr_sha256": "c" * 64,
            "prompt_persisted": False,
            "full_transcript_persisted": False,
            "allow_all_used": False,
        }


class FakePublisher:
    def __init__(self):
        self.calls = []

    def publish(self, repo, issue, policy, branch, changed_paths, test_results):
        self.calls.append((repo, issue, policy, branch, changed_paths, test_results))
        return f"https://github.com/{REPOSITORY}/pull/9"


class CopilotCodeModifierTest(unittest.TestCase):
    def test_policy_is_strict_and_model_is_pinned(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text(json.dumps(policy_payload()), encoding="utf-8")

            policy = load_issue_code_policy(path)

            self.assertEqual("gpt-5.6-sol", policy.default_model)
            self.assertTrue(policy.draft_pr_only)
            self.assertFalse(policy.auto_merge)

            invalid = policy_payload()
            invalid["allow_all"] = True
            path.write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unsupported fields"):
                load_issue_code_policy(path)

    def test_issue_requires_open_state_label_and_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text(json.dumps(policy_payload()), encoding="utf-8")
            policy = load_issue_code_policy(path)

            result = evaluate_issue_approval(approved_issue(), policy)
            self.assertTrue(result["approved"])

            missing_label = ApprovedIssue(
                **{**approved_issue().__dict__, "labels": ("bug",)}
            )
            self.assertFalse(evaluate_issue_approval(missing_label, policy)["approved"])

            duplicate_marker = approved_issue(
                f"\n<!-- repository-issue-fingerprint/v1:{'b' * 64} -->"
            )
            self.assertFalse(
                evaluate_issue_approval(duplicate_marker, policy)["approved"]
            )

            missing_timestamp = ApprovedIssue(
                **{**approved_issue().__dict__, "updated_at": ""}
            )
            self.assertFalse(
                evaluate_issue_approval(missing_timestamp, policy)["approved"]
            )

    def test_cli_uses_stdin_minimal_permissions_and_no_allow_all(self):
        modifier = CopilotCLICodeModifier()
        calls = []

        def fake_run(args, cwd, timeout_seconds, input_text=None, env=None):
            calls.append((args, input_text, env))
            return ProcessOutput(0, "done", "")

        with mock.patch(
            "src.copilot_code_modifier._run_process", side_effect=fake_run
        ), mock.patch.dict(
            "src.copilot_code_modifier.os.environ",
            {
                "HOME": "/tmp/home",
                "PATH": "/usr/bin",
                "AI_API_KEY": "must-not-reach-copilot",
                "COPILOT_ALLOW_ALL": "true",
                "COPILOT_HOME": "/shared/copilot-home",
            },
            clear=True,
        ):
            result = modifier.modify(
                Path("/tmp"),
                "private approved Issue prompt",
                "gpt-5.6-sol",
                60,
            )

        args, prompt, environment = calls[0]
        joined = " ".join(args)
        self.assertEqual("private approved Issue prompt", prompt)
        self.assertNotIn("private approved Issue prompt", joined)
        self.assertIn("--no-ask-user", args)
        self.assertIn("--disable-builtin-mcps", args)
        self.assertIn("--no-custom-instructions", args)
        self.assertIn("--available-tools=view,grep,glob,edit", args)
        self.assertIn("--deny-tool=shell", args)
        self.assertIn("--deny-url", args)
        self.assertIn("--allow-tool=write", args)
        self.assertFalse(any("allow-all" in item or "yolo" in item for item in args))
        self.assertNotIn("COPILOT_ALLOW_ALL", environment)
        self.assertNotIn("AI_API_KEY", environment)
        self.assertIn("COPILOT_HOME", environment)
        self.assertNotEqual("/shared/copilot-home", environment["COPILOT_HOME"])
        self.assertFalse(result["allow_all_used"])

    def test_blocked_path_change_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = initialize_repo(Path(directory))
            policy = load_issue_code_policy(repo / ".github" / "issue-code-policy.json")
            (repo / ".github" / "workflow.yml").write_text("name: unsafe\n", encoding="utf-8")

            result = validate_changes(repo, policy)

            self.assertFalse(result["valid"])
            self.assertIn("path is not allowed: .github/workflow.yml", result["reasons"])

    def test_dry_run_locates_without_modifying(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = initialize_repo(Path(directory))
            modifier = FakeModifier(write_change=True)

            result = execute_issue_code_workflow(
                ISSUE_URL,
                repo,
                repo / ".github" / "issue-code-policy.json",
                FakeIssueClient(),
                modifier,
            )

            self.assertEqual("ready", result["status"])
            self.assertEqual("dry_run", result["mode"])
            self.assertEqual([], modifier.calls)
            self.assertEqual("main", result["repository"]["base_branch"])
            self.assertEqual([], result["changes"]["paths"])

    def test_execute_modifies_and_runs_only_policy_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = initialize_repo(Path(directory))
            modifier = FakeModifier(write_change=True)

            result = execute_issue_code_workflow(
                ISSUE_URL,
                repo,
                repo / ".github" / "issue-code-policy.json",
                FakeIssueClient(),
                modifier,
                execute=True,
            )

            self.assertEqual("tested", result["status"])
            self.assertTrue(result["changes"]["valid"])
            self.assertEqual(
                ["src/calculator.py", "tests/test_calculator.py"],
                result["changes"]["paths"],
            )
            self.assertEqual(0, result["tests"][0]["returncode"])
            self.assertIn("Canonical Issue URL", modifier.calls[0][1])
            self.assertEqual("gpt-5.6-sol", modifier.calls[0][2])

    def test_copilot_invocation_failure_removes_empty_work_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = initialize_repo(Path(directory))

            result = execute_issue_code_workflow(
                ISSUE_URL,
                repo,
                repo / ".github" / "issue-code-policy.json",
                FakeIssueClient(),
                FakeModifier(raise_error=True),
                execute=True,
            )

            self.assertEqual("blocked", result["status"])
            self.assertEqual(
                "copilot_cli_invocation_failed",
                result["modification"]["failure_reason"],
            )
            self.assertTrue(result["modification"]["work_branch_removed"])
            current_branch = subprocess.run(
                ["git", "-C", str(repo), "branch", "--show-current"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual("main", current_branch)

    def test_test_command_cannot_change_the_validated_patch(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = initialize_repo(Path(directory))
            mutate = repo / "tests" / "mutate.py"
            mutate.write_text(
                "from pathlib import Path\n"
                "path = Path('src/calculator.py')\n"
                "path.write_text(path.read_text() + '\\n# changed during tests\\n')\n",
                encoding="utf-8",
            )
            policy = policy_payload()
            policy["test_commands"] = [["python3", "tests/mutate.py"]]
            (repo / ".github" / "issue-code-policy.json").write_text(
                json.dumps(policy), encoding="utf-8"
            )
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-m", "add mutating test"],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            head = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", head],
                check=True,
            )

            result = execute_issue_code_workflow(
                ISSUE_URL,
                repo,
                repo / ".github" / "issue-code-policy.json",
                FakeIssueClient(),
                FakeModifier(write_change=True),
                execute=True,
            )

            self.assertEqual("blocked", result["status"])
            self.assertFalse(result["changes"]["valid"])
            self.assertIn(
                "worktree changed while policy tests were running",
                result["changes"]["reasons"],
            )

    def test_issue_change_after_modification_blocks_tests_and_pr(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = initialize_repo(Path(directory))
            changed_issue = ApprovedIssue(
                **{
                    **approved_issue().__dict__,
                    "body": approved_issue().body + "\nnew instruction",
                    "updated_at": "2026-07-24T00:01:00Z",
                }
            )
            publisher = FakePublisher()

            result = execute_issue_code_workflow(
                ISSUE_URL,
                repo,
                repo / ".github" / "issue-code-policy.json",
                FakeIssueClient([approved_issue(), changed_issue]),
                FakeModifier(write_change=True),
                publish_pr=True,
                publisher=publisher,
            )

            self.assertEqual("blocked", result["status"])
            self.assertFalse(
                result["approval"]["rules"]["issue_unchanged_after_modification"]
            )
            self.assertEqual([], result["tests"])
            self.assertEqual([], publisher.calls)

    def test_publish_pr_only_after_tests_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = initialize_repo(Path(directory))
            publisher = FakePublisher()

            result = execute_issue_code_workflow(
                ISSUE_URL,
                repo,
                repo / ".github" / "issue-code-policy.json",
                FakeIssueClient(),
                FakeModifier(write_change=True),
                publish_pr=True,
                publisher=publisher,
            )

            self.assertEqual("draft_pr_created", result["status"])
            self.assertEqual(
                f"https://github.com/{REPOSITORY}/pull/9",
                result["publication"]["draft_pr_url"],
            )
            self.assertEqual(1, len(publisher.calls))
            self.assertFalse(result["publication"]["auto_merge"])


if __name__ == "__main__":
    unittest.main()
