import contextlib
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.approved_issue_dispatcher import (
    GitHubCLIApprovedIssueClient,
    GitHubCLIDispatchStateInspector,
    GitRemoteBranchClaimer,
    dispatch_once,
    main,
)
from src.copilot_code_modifier import ApprovedIssue, ProcessOutput


REPOSITORY = "example-org/example-service"
ISSUE_URL = f"https://github.com/{REPOSITORY}/issues/17"
FINGERPRINT = hashlib.sha256(b"approved-dispatch-issue").hexdigest()


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
            [
                "python3",
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-v",
            ]
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


def approved_issue(**overrides):
    values = {
        "repository": REPOSITORY,
        "number": 17,
        "url": ISSUE_URL,
        "title": "Add multiply support",
        "body": (
            "Implement multiply support with deterministic tests.\n\n"
            f"<!-- repository-issue-fingerprint/v1:{FINGERPRINT} -->"
        ),
        "state": "OPEN",
        "labels": ("ai-code-approved",),
        "updated_at": "2026-07-24T00:00:00Z",
    }
    values.update(overrides)
    return ApprovedIssue(**values)


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
        "class CalculatorTest(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(3, add(1, 2))\n",
        encoding="utf-8",
    )
    (repo / ".github" / "issue-code-policy.json").write_text(
        json.dumps(policy_payload()), encoding="utf-8"
    )
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
        ["git", "-C", str(repo), "config", "user.name", "Test User"], check=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "add",
            "origin",
            f"https://github.com/{REPOSITORY}.git",
        ],
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


class FakeCandidateClient:
    def __init__(self, urls=None):
        self.urls = list(urls if urls is not None else [ISSUE_URL])
        self.calls = []

    def list_open_issues(self, repository, required_labels, limit):
        self.calls.append((repository, tuple(required_labels), limit))
        return self.urls


class FakeIssueClient:
    def __init__(self, issue=None):
        self.issue = issue or approved_issue()
        self.calls = []

    def fetch(self, issue_url):
        self.calls.append(issue_url)
        return self.issue


class SequencedIssueClient:
    def __init__(self, issues):
        self.issues = list(issues)
        self.calls = []

    def fetch(self, issue_url):
        self.calls.append(issue_url)
        if len(self.issues) > 1:
            return self.issues.pop(0)
        return self.issues[0]


class FakeStateInspector:
    def __init__(
        self,
        claimed=False,
        *,
        remote_claim_commit=None,
        local_work_branch_exists=None,
        remote_work_branch_exists=False,
        existing_pr_url=None,
    ):
        self.claimed = claimed
        self.remote_claim_commit = remote_claim_commit
        self.local_work_branch_exists = (
            claimed
            if local_work_branch_exists is None
            else local_work_branch_exists
        )
        self.remote_work_branch_exists = remote_work_branch_exists
        self.existing_pr_url = existing_pr_url
        self.calls = []

    def inspect(self, repo, repository, work_branch, claim_branch):
        self.calls.append((repo, repository, work_branch, claim_branch))
        return {
            "claimed": self.claimed,
            "local_work_branch_exists": self.local_work_branch_exists,
            "remote_work_branch_exists": self.remote_work_branch_exists,
            "remote_claim_branch_exists": self.remote_claim_commit is not None,
            "remote_claim_commit": self.remote_claim_commit,
            "existing_pr_url": self.existing_pr_url,
        }


class FakeClaimer:
    def __init__(self, claimed=True):
        self.claimed = claimed
        self.calls = []

    def claim(self, repo, base_commit, claim_branch):
        self.calls.append((repo, base_commit, claim_branch))
        return {
            "claimed": self.claimed,
            "claim_branch": claim_branch,
            "claim_commit": "a" * 40 if self.claimed else None,
            "remote_commit": "a" * 40 if self.claimed else "b" * 40,
            "conflict": not self.claimed,
            "retained_on_failure": self.claimed,
        }


class FakeModifier:
    pass


class FakeWritingModifier:
    def __init__(self):
        self.calls = []

    def version(self, repo):
        return "GitHub Copilot CLI synthetic"

    def modify(self, repo, prompt, model, timeout_seconds):
        self.calls.append((repo, prompt, model, timeout_seconds))
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
            "class CalculatorTest(unittest.TestCase):\n"
            "    def test_add(self):\n"
            "        self.assertEqual(3, add(1, 2))\n\n"
            "    def test_multiply(self):\n"
            "        self.assertEqual(6, multiply(2, 3))\n",
            encoding="utf-8",
        )
        return {
            "returncode": 0,
            "stdout_sha256": "b" * 64,
            "stderr_sha256": "c" * 64,
            "prompt_persisted": False,
            "full_transcript_persisted": False,
            "allow_all_used": False,
        }


class ApprovedIssueDispatcherTest(unittest.TestCase):
    def test_dispatches_one_approved_issue_into_modifier_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = initialize_repo(Path(directory))
            issue_client = FakeIssueClient()
            workflow_calls = []

            def workflow(
                issue_url, target, policy_path, client, modifier, execute
            ):
                workflow_calls.append(
                    (issue_url, target, policy_path, client, modifier, execute)
                )
                return {
                    "status": "ready",
                    "mode": "dry_run",
                    "source": {"snapshot_sha256": issue_client.issue.sha256},
                }

            report = dispatch_once(
                repo,
                repo / ".github" / "issue-code-policy.json",
                FakeCandidateClient(),
                issue_client,
                FakeStateInspector(),
                FakeModifier(),
                workflow_runner=workflow,
            )

        self.assertEqual("ready", report["status"])
        self.assertEqual("once_dry_run", report["mode"])
        self.assertEqual(1, len(workflow_calls))
        self.assertFalse(report["capabilities"]["copilot_execution"])
        self.assertFalse(report["capabilities"]["github_claim_write"])
        self.assertFalse(workflow_calls[0][-1])
        self.assertNotIn("title", report["candidates"][0])
        self.assertNotIn("body", report["candidates"][0])

    def test_missing_approval_label_never_reaches_modifier(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = initialize_repo(Path(directory))
            issue_client = FakeIssueClient(
                approved_issue(labels=("bug",))
            )
            workflow = mock.Mock()

            report = dispatch_once(
                repo,
                repo / ".github" / "issue-code-policy.json",
                FakeCandidateClient(),
                issue_client,
                FakeStateInspector(),
                FakeModifier(),
                workflow_runner=workflow,
            )

        self.assertEqual("blocked", report["status"])
        workflow.assert_not_called()
        self.assertFalse(report["candidates"][0]["approved"])

    def test_existing_work_branch_prevents_duplicate_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = initialize_repo(Path(directory))
            workflow = mock.Mock()

            report = dispatch_once(
                repo,
                repo / ".github" / "issue-code-policy.json",
                FakeCandidateClient(),
                FakeIssueClient(),
                FakeStateInspector(claimed=True),
                FakeModifier(),
                workflow_runner=workflow,
            )

        self.assertEqual("blocked", report["status"])
        workflow.assert_not_called()
        self.assertTrue(report["candidates"][0]["idempotency"]["claimed"])

    def test_issue_change_before_dispatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = initialize_repo(Path(directory))
            issue_client = FakeIssueClient()

            def changed_workflow(*_args):
                return {
                    "status": "ready",
                    "source": {"snapshot_sha256": "f" * 64},
                }

            report = dispatch_once(
                repo,
                repo / ".github" / "issue-code-policy.json",
                FakeCandidateClient(),
                issue_client,
                FakeStateInspector(),
                FakeModifier(),
                workflow_runner=changed_workflow,
            )

        self.assertEqual("blocked", report["status"])
        self.assertEqual(
            "issue_changed_before_dispatch",
            report["dispatch"]["failure_reason"],
        )

    def test_modifier_preflight_failure_has_a_safe_stage_category(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = initialize_repo(Path(directory))

            def blocked_workflow(*_args):
                raise ValueError(
                    "Issue text contains unclassified high-entropy data"
                )

            report = dispatch_once(
                repo,
                repo / ".github" / "issue-code-policy.json",
                FakeCandidateClient(),
                FakeIssueClient(),
                FakeStateInspector(),
                FakeModifier(),
                workflow_runner=blocked_workflow,
            )

        self.assertEqual("blocked", report["status"])
        self.assertEqual(
            "modifier_localization_safety_blocked",
            report["dispatch"]["failure_reason"],
        )
        self.assertNotIn("high-entropy", json.dumps(report))

    def test_execute_claims_then_invokes_copilot_workflow(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = initialize_repo(Path(directory))
            issue_client = FakeIssueClient()
            claimer = FakeClaimer()
            workflow_calls = []

            def workflow(
                issue_url, target, policy_path, client, modifier, execute
            ):
                workflow_calls.append(
                    (issue_url, target, policy_path, client, modifier, execute)
                )
                return {
                    "status": "tested",
                    "mode": "execute",
                    "source": {"snapshot_sha256": issue_client.issue.sha256},
                }

            report = dispatch_once(
                repo,
                repo / ".github" / "issue-code-policy.json",
                FakeCandidateClient(),
                issue_client,
                FakeStateInspector(),
                FakeModifier(),
                execute=True,
                claimer=claimer,
                workflow_runner=workflow,
            )

        self.assertEqual("tested", report["status"])
        self.assertEqual("once_execute", report["mode"])
        self.assertTrue(report["capabilities"]["copilot_execution"])
        self.assertTrue(report["dispatch"]["claim"]["claimed"])
        self.assertTrue(workflow_calls[0][-1])
        self.assertEqual(1, len(claimer.calls))

    def test_explicit_resume_reuses_only_the_exact_retained_claim(self):
        retained_commit = "d" * 40
        with tempfile.TemporaryDirectory() as directory:
            repo = initialize_repo(Path(directory))
            issue_client = FakeIssueClient()
            workflow_calls = []

            def workflow(
                issue_url, target, policy_path, client, modifier, execute
            ):
                workflow_calls.append(issue_url)
                return {
                    "status": "tested",
                    "source": {"snapshot_sha256": issue_client.issue.sha256},
                }

            report = dispatch_once(
                repo,
                repo / ".github" / "issue-code-policy.json",
                mock.Mock(),
                issue_client,
                FakeStateInspector(
                    claimed=True,
                    remote_claim_commit=retained_commit,
                    local_work_branch_exists=False,
                ),
                FakeModifier(),
                execute=True,
                target_issue_url=ISSUE_URL,
                retained_claim_commit=retained_commit,
                expected_issue_snapshot_sha256=issue_client.issue.sha256,
                workflow_runner=workflow,
            )

        self.assertEqual("tested", report["status"])
        self.assertEqual([ISSUE_URL], workflow_calls)
        self.assertTrue(report["dispatch"]["claim"]["resumed"])
        self.assertEqual(
            retained_commit,
            report["dispatch"]["claim"]["remote_commit"],
        )

    def test_retained_claim_mismatch_stops_before_modifier(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = initialize_repo(Path(directory))
            workflow = mock.Mock()

            report = dispatch_once(
                repo,
                repo / ".github" / "issue-code-policy.json",
                mock.Mock(),
                FakeIssueClient(),
                FakeStateInspector(
                    claimed=True,
                    remote_claim_commit="e" * 40,
                    local_work_branch_exists=False,
                ),
                FakeModifier(),
                execute=True,
                target_issue_url=ISSUE_URL,
                retained_claim_commit="d" * 40,
                expected_issue_snapshot_sha256=approved_issue().sha256,
                workflow_runner=workflow,
            )

        self.assertEqual("blocked", report["status"])
        self.assertFalse(
            report["candidates"][0]["idempotency"]["retained_claim_matches"]
        )
        workflow.assert_not_called()

    def test_retained_claim_resume_requires_the_full_issue_snapshot(self):
        retained_commit = "d" * 40
        with tempfile.TemporaryDirectory() as directory:
            repo = initialize_repo(Path(directory))
            workflow = mock.Mock()

            report = dispatch_once(
                repo,
                repo / ".github" / "issue-code-policy.json",
                mock.Mock(),
                FakeIssueClient(),
                FakeStateInspector(
                    claimed=True,
                    remote_claim_commit=retained_commit,
                    local_work_branch_exists=False,
                ),
                FakeModifier(),
                execute=True,
                target_issue_url=ISSUE_URL,
                retained_claim_commit=retained_commit,
                expected_issue_snapshot_sha256="f" * 64,
                workflow_runner=workflow,
            )

        self.assertEqual("blocked", report["status"])
        self.assertFalse(
            report["candidates"][0]["approval_rules"][
                "retained_snapshot_matches"
            ]
        )
        workflow.assert_not_called()

    def test_publish_pr_mode_is_reported_only_after_draft_pr_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = initialize_repo(Path(directory))
            issue_client = FakeIssueClient()

            def workflow(
                issue_url, target, policy_path, client, modifier, execute
            ):
                return {
                    "status": "draft_pr_created",
                    "mode": "publish_pr",
                    "source": {"snapshot_sha256": issue_client.issue.sha256},
                    "publication": {
                        "status": "created",
                        "draft_pr_url": "https://github.com/example-org/example-service/pull/3",
                    },
                }

            report = dispatch_once(
                repo,
                repo / ".github" / "issue-code-policy.json",
                FakeCandidateClient(),
                issue_client,
                FakeStateInspector(),
                FakeModifier(),
                execute=True,
                publish_pr=True,
                model="gpt-5.6-sol",
                claimer=FakeClaimer(),
                workflow_runner=workflow,
            )

        self.assertEqual("draft_pr_created", report["status"])
        self.assertEqual("once_publish_pr", report["mode"])
        self.assertTrue(report["capabilities"]["draft_pr_publication"])

    def test_explicit_target_bypasses_eventually_consistent_candidate_listing(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = initialize_repo(Path(directory))
            candidate_client = mock.Mock()
            issue_client = FakeIssueClient()

            def workflow(
                issue_url, target, policy_path, client, modifier, execute
            ):
                return {
                    "status": "ready",
                    "source": {"snapshot_sha256": issue_client.issue.sha256},
                }

            report = dispatch_once(
                repo,
                repo / ".github" / "issue-code-policy.json",
                candidate_client,
                issue_client,
                FakeStateInspector(),
                FakeModifier(),
                target_issue_url=ISSUE_URL,
                workflow_runner=workflow,
            )

        self.assertEqual("ready", report["status"])
        self.assertEqual("explicit_target", report["poll"]["candidate_source"])
        candidate_client.list_open_issues.assert_not_called()

    def test_explicit_target_still_requires_approval_label(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = initialize_repo(Path(directory))
            candidate_client = mock.Mock()
            workflow = mock.Mock()

            report = dispatch_once(
                repo,
                repo / ".github" / "issue-code-policy.json",
                candidate_client,
                FakeIssueClient(approved_issue(labels=("bug",))),
                FakeStateInspector(),
                FakeModifier(),
                target_issue_url=ISSUE_URL,
                workflow_runner=workflow,
            )

        self.assertEqual("blocked", report["status"])
        self.assertFalse(report["candidates"][0]["approved"])
        self.assertFalse(
            report["candidates"][0]["approval_rules"]["required_labels_present"]
        )
        candidate_client.list_open_issues.assert_not_called()
        workflow.assert_not_called()

    def test_explicit_target_must_be_canonical_for_policy_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = initialize_repo(Path(directory))
            with self.assertRaisesRegex(ValueError, "not canonical"):
                dispatch_once(
                    repo,
                    repo / ".github" / "issue-code-policy.json",
                    mock.Mock(),
                    FakeIssueClient(),
                    FakeStateInspector(),
                    FakeModifier(),
                    target_issue_url=(
                        "https://github.com/example-org/other-service/issues/17"
                    ),
                )

    def test_fetched_snapshot_must_match_requested_url(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = initialize_repo(Path(directory))
            workflow = mock.Mock()

            report = dispatch_once(
                repo,
                repo / ".github" / "issue-code-policy.json",
                FakeCandidateClient(),
                FakeIssueClient(approved_issue(url=f"{ISSUE_URL}0", number=170)),
                FakeStateInspector(),
                FakeModifier(),
                target_issue_url=ISSUE_URL,
                workflow_runner=workflow,
            )

        self.assertEqual("blocked", report["status"])
        self.assertFalse(
            report["candidates"][0]["approval_rules"][
                "requested_url_matches_snapshot"
            ]
        )
        workflow.assert_not_called()

    def test_execute_uses_real_guarded_modifier_and_policy_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = initialize_repo(Path(directory))
            modifier = FakeWritingModifier()

            report = dispatch_once(
                repo,
                repo / ".github" / "issue-code-policy.json",
                FakeCandidateClient(),
                FakeIssueClient(),
                FakeStateInspector(),
                modifier,
                execute=True,
                claimer=FakeClaimer(),
            )

            self.assertEqual("tested", report["status"])
            self.assertEqual(
                "tested", report["dispatch"]["modifier_report"]["status"]
            )
            self.assertEqual(
                ["src/calculator.py", "tests/test_calculator.py"],
                report["dispatch"]["modifier_report"]["changes"]["paths"],
            )
            self.assertEqual(
                0, report["dispatch"]["modifier_report"]["tests"][0]["returncode"]
            )
            self.assertEqual(1, len(modifier.calls))

    def test_claim_conflict_stops_before_copilot(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = initialize_repo(Path(directory))
            workflow = mock.Mock()

            report = dispatch_once(
                repo,
                repo / ".github" / "issue-code-policy.json",
                FakeCandidateClient(),
                FakeIssueClient(),
                FakeStateInspector(),
                FakeModifier(),
                execute=True,
                claimer=FakeClaimer(claimed=False),
                workflow_runner=workflow,
            )

        self.assertEqual("blocked", report["status"])
        self.assertEqual(
            "claim_conflict_or_failure",
            report["dispatch"]["failure_reason"],
        )
        workflow.assert_not_called()

    def test_issue_change_after_claim_stops_before_copilot(self):
        changed = approved_issue(
            body=approved_issue().body + "\nnew instruction",
            updated_at="2026-07-24T00:01:00Z",
        )
        with tempfile.TemporaryDirectory() as directory:
            repo = initialize_repo(Path(directory))
            issue_client = SequencedIssueClient(
                [approved_issue(), approved_issue(), changed]
            )
            workflow = mock.Mock()

            report = dispatch_once(
                repo,
                repo / ".github" / "issue-code-policy.json",
                FakeCandidateClient(),
                issue_client,
                FakeStateInspector(),
                FakeModifier(),
                execute=True,
                claimer=FakeClaimer(),
                workflow_runner=workflow,
            )

        self.assertEqual("blocked", report["status"])
        self.assertEqual(
            "issue_changed_after_claim",
            report["dispatch"]["failure_reason"],
        )
        self.assertTrue(report["dispatch"]["claim"]["retained_on_failure"])
        workflow.assert_not_called()

    def test_unknown_repository_never_reaches_modifier(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = initialize_repo(Path(directory))
            workflow = mock.Mock()

            report = dispatch_once(
                repo,
                repo / ".github" / "issue-code-policy.json",
                FakeCandidateClient(),
                FakeIssueClient(
                    approved_issue(repository="example-org/other-service")
                ),
                FakeStateInspector(),
                FakeModifier(),
                workflow_runner=workflow,
            )

        self.assertEqual("blocked", report["status"])
        workflow.assert_not_called()
        self.assertFalse(
            report["candidates"][0]["approval_rules"][
                "repository_matches_policy"
            ]
        )

    def test_candidate_list_is_bounded_and_unique(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = initialize_repo(Path(directory))
            with self.assertRaisesRegex(ValueError, "deterministic bounds"):
                dispatch_once(
                    repo,
                    repo / ".github" / "issue-code-policy.json",
                    FakeCandidateClient([ISSUE_URL, ISSUE_URL]),
                    FakeIssueClient(),
                    FakeStateInspector(),
                    FakeModifier(),
                    max_candidates=2,
                )

    def test_github_lister_uses_required_labels_and_sorts_issue_numbers(self):
        calls = []

        def fake_run(args, cwd, timeout_seconds, input_text=None, env=None):
            calls.append(args)
            return ProcessOutput(
                0,
                json.dumps(
                    [
                        {
                            "number": 19,
                            "url": f"https://github.com/{REPOSITORY}/issues/19",
                        },
                        {
                            "number": 17,
                            "url": ISSUE_URL,
                        },
                    ]
                ),
                "",
            )

        with mock.patch(
            "src.approved_issue_dispatcher._run_process", side_effect=fake_run
        ):
            urls = GitHubCLIApprovedIssueClient().list_open_issues(
                REPOSITORY, ("ai-code-approved", "triaged"), 10
            )

        self.assertEqual(
            [ISSUE_URL, f"https://github.com/{REPOSITORY}/issues/19"], urls
        )
        self.assertEqual(1, calls[0].count("ai-code-approved"))
        self.assertEqual(1, calls[0].count("triaged"))
        self.assertIn("sort:created-asc", calls[0])

    def test_state_inspector_accepts_pr_number_ending_in_zero_as_claimed(self):
        responses = [
            ProcessOutput(1, "", ""),
            ProcessOutput(2, "", ""),
            ProcessOutput(2, "", ""),
            ProcessOutput(
                0,
                json.dumps(
                    [
                        {
                            "number": 10,
                            "url": f"https://github.com/{REPOSITORY}/pull/10",
                            "state": "MERGED",
                            "isDraft": False,
                        }
                    ]
                ),
                "",
            ),
        ]
        with mock.patch(
            "src.approved_issue_dispatcher._run_process",
            side_effect=responses,
        ):
            result = GitHubCLIDispatchStateInspector().inspect(
                Path("/tmp/repo"),
                REPOSITORY,
                "codex/copilot/issue-17-abcd1234",
                "codex/copilot/claims/issue-17-abcd1234",
            )

        self.assertTrue(result["claimed"])
        self.assertEqual(
            f"https://github.com/{REPOSITORY}/pull/10",
            result["existing_pr_url"],
        )

    def test_unique_claim_commit_allows_only_one_remote_winner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = initialize_repo(root)
            remote = root / "remote.git"
            subprocess.run(
                ["git", "init", "--bare", str(remote)],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            subprocess.run(
                ["git", "-C", str(repo), "remote", "set-url", "origin", str(remote)],
                check=True,
            )
            base_commit = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            claimer = GitRemoteBranchClaimer()
            claim_branch = "codex/copilot/claims/issue-17-abcd1234"

            first = claimer.claim(repo, base_commit, claim_branch)
            second = claimer.claim(repo, base_commit, claim_branch)

        self.assertTrue(first["claimed"])
        self.assertFalse(second["claimed"])
        self.assertTrue(second["conflict"])
        self.assertIsNone(second["claim_commit"])
        self.assertEqual(first["claim_commit"], second["remote_commit"])

    def test_cli_requires_explicit_dispatch_mode(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(
            SystemExit
        ) as raised:
            main(
                [
                    "--repo",
                    "/tmp/repo",
                    "--output",
                    "/tmp/report.json",
                ]
            )
        self.assertEqual(2, raised.exception.code)


if __name__ == "__main__":
    unittest.main()
