import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.local_control_center import (
    CONFIG_SCHEMA_VERSION,
    ControlCenterWorkflow,
    LocalConfigStore,
    _compose_managed_evidence,
    _generation_failure_code,
    _is_resumable_empty_modification,
)
from tests.test_approved_issue_dispatcher import REPOSITORY, initialize_repo
from tests.test_repository_issue_automation import generation


def config_payload(repo: Path, model: str = "gpt-5.6-sol"):
    return {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "github": {"login": "example-user"},
        "copilot": {"model": model},
        "repositories": [
            {
                "repository": REPOSITORY,
                "local_path": str(repo),
                "enabled": True,
            }
        ],
    }


IDENTITY = {
    "github": {
        "available": True,
        "authenticated": True,
        "login": "example-user",
        "accounts": [{"login": "example-user", "active": True}],
    },
    "copilot": {
        "available": True,
        "version": "GitHub Copilot CLI synthetic",
        "authentication": "current_local_user",
    },
}


class LocalConfigStoreTest(unittest.TestCase):
    def test_loads_repository_policy_and_keeps_only_nonsecret_preferences(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = initialize_repo(root)
            path = root / "state" / "control-center.json"
            store = LocalConfigStore(path)

            with mock.patch(
                "src.local_control_center.inspect_identity",
                return_value=IDENTITY,
            ):
                config = store.save(config_payload(repo))

            persisted = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual("example-user", config.github_login)
        self.assertEqual(("gpt-5.6-sol",), config.repositories[0].allowed_models)
        self.assertEqual(
            {
                "schema_version",
                "github",
                "copilot",
                "repositories",
            },
            set(persisted),
        )
        self.assertNotIn("token", json.dumps(persisted).lower())
        self.assertNotIn("api_key", json.dumps(persisted).lower())

    def test_empty_model_uses_tracked_repository_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = initialize_repo(root)
            store = LocalConfigStore(root / "config.json")

            config = store.parse(config_payload(repo, model=""))

        self.assertEqual("gpt-5.6-sol", config.copilot_model)

    def test_rejects_repository_name_that_disagrees_with_tracked_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = initialize_repo(root)
            payload = config_payload(repo)
            payload["repositories"][0]["repository"] = "example-org/other-service"

            with self.assertRaisesRegex(ValueError, "tracked Issue code policy"):
                LocalConfigStore(root / "config.json").parse(payload)

    def test_rejects_model_outside_repository_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = initialize_repo(root)

            with self.assertRaisesRegex(ValueError, "not allowed"):
                LocalConfigStore(root / "config.json").parse(
                    config_payload(repo, model="unsupported-model")
                )

    def test_saved_configuration_is_owner_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = initialize_repo(root)
            path = root / "config.json"
            with mock.patch(
                "src.local_control_center.inspect_identity",
                return_value=IDENTITY,
            ):
                LocalConfigStore(path).save(config_payload(repo))
            mode = os.stat(path).st_mode & 0o777

        self.assertEqual(0o600, mode)


class FakeApprovalClient:
    def __init__(self):
        self.calls = []

    def ensure_and_apply(self, repo_path, repository, issue_url, labels):
        self.calls.append((repo_path, repository, issue_url, tuple(labels)))


class ControlCenterWorkflowTest(unittest.TestCase):
    def test_generation_failure_message_distinguishes_validation_and_review(self):
        self.assertEqual(
            "issue_draft_validation_failed",
            _generation_failure_code(
                {
                    "validation": {"errors": ["unknown claim path"]},
                    "review": {"verdict": "needs_clarification"},
                }
            ),
        )
        self.assertEqual(
            "issue_review_rejected",
            _generation_failure_code(
                {
                    "validation": {"errors": []},
                    "review": {"verdict": "reject"},
                }
            ),
        )

    def _configured_workflow(self, root):
        repo = initialize_repo(root)
        config_path = root / "config.json"
        with mock.patch(
            "src.local_control_center.inspect_identity",
            return_value=IDENTITY,
        ):
            store = LocalConfigStore(config_path)
            store.save(config_payload(repo))
        approval = FakeApprovalClient()
        return (
            repo,
            ControlCenterWorkflow(
                store,
                root / "runs",
                approval_client=approval,
                issue_provider_factory=lambda _model: mock.Mock(),
            ),
            approval,
        )

    def _prepared_run(self, root):
        repo, workflow, approval = self._configured_workflow(root)
        with mock.patch(
            "src.local_control_center.threading.Thread"
        ), mock.patch(
            "src.local_control_center.ai_issue_generator.generate_issue",
            return_value=generation(),
        ), mock.patch(
            "src.local_control_center.automate_repository_issue",
            return_value={
                "publication": {
                    "status": "approved_not_published",
                    "repository": REPOSITORY,
                }
            },
        ), mock.patch(
            "src.local_control_center.render_automated_issue_body",
            return_value=(
                "# Synthetic reviewed Issue\n\nSafe body.",
                "a" * 64,
            ),
        ):
            record = workflow.create(
                "com.example.routing.SyntheticRoutingController.routeIssue fails"
            )
            workflow._prepare(
                record["run_id"],
                "com.example.routing.SyntheticRoutingController.routeIssue fails",
            )
        return repo, workflow, approval, workflow.read(record["run_id"])

    def _retained_claim_run(self, root):
        repo, workflow, approval, record = self._prepared_run(root)
        run_id = record["run_id"]
        issue_url = f"https://github.com/{REPOSITORY}/issues/17"
        claim_branch = "codex/copilot/claims/issue-17-abcd1234"
        claim_commit = "d" * 40
        record.update(
            {
                "status": "blocked",
                "result": {"issue_url": issue_url, "draft_pr_url": None},
                "failure": {
                    "code": "code_dispatch_failed",
                    "message": "blocked",
                },
            }
        )
        workflow._write_record(run_id, record)
        dispatch = {
            "status": "blocked",
            "repository": {"name": REPOSITORY},
            "candidates": [
                {
                    "url": issue_url,
                    "snapshot_sha256": "c" * 64,
                    "approved": True,
                    "claim_branch": claim_branch,
                    "idempotency": {
                        "local_work_branch_exists": False,
                        "remote_work_branch_exists": False,
                        "existing_pr_url": None,
                    },
                }
            ],
            "dispatch": {
                "issue_url": issue_url,
                "modifier_report": None,
                "failure_reason": "modifier_execution_failed",
                "claim": {
                    "claimed": True,
                    "retained_on_failure": True,
                    "claim_branch": claim_branch,
                    "claim_commit": claim_commit,
                    "remote_commit": claim_commit,
                },
            },
        }
        (workflow._run_dir(run_id) / "dispatch.json").write_text(
            json.dumps(dispatch),
            encoding="utf-8",
        )
        return repo, workflow, approval, run_id

    def _empty_modification_run(self, root):
        repo, workflow, approval, run_id = self._retained_claim_run(root)
        dispatch_path = workflow._run_dir(run_id) / "dispatch.json"
        dispatch = json.loads(dispatch_path.read_text(encoding="utf-8"))
        issue_url = dispatch["dispatch"]["issue_url"]
        snapshot_sha256 = dispatch["candidates"][0]["snapshot_sha256"]
        base_commit = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        work_branch = "codex/copilot/issue-17-abcd1234"
        subprocess.run(
            ["git", "-C", str(repo), "switch", "-c", work_branch],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        dispatch["repository"].update(
            {
                "base_branch": "main",
                "base_commit": base_commit,
            }
        )
        dispatch["candidates"][0]["work_branch"] = work_branch
        dispatch["dispatch"].update(
            {
                "failure_reason": "modifier_execution_blocked",
                "modifier_report": {
                    "status": "blocked",
                    "source": {
                        "repository": REPOSITORY,
                        "url": issue_url,
                        "snapshot_sha256": snapshot_sha256,
                    },
                    "repository": {
                        "repository": REPOSITORY,
                        "base_commit": base_commit,
                        "work_branch": work_branch,
                    },
                    "modification": {
                        "status": "completed",
                        "returncode": 0,
                    },
                    "changes": {
                        "valid": False,
                        "paths": [],
                        "added_lines": 0,
                        "deleted_lines": 0,
                        "diff_sha256": (
                            "e3b0c44298fc1c149afbf4c8996fb924"
                            "27ae41e4649b934ca495991b7852b855"
                        ),
                        "reasons": ["Copilot produced no file changes"],
                    },
                    "tests": [],
                    "publication": {
                        "status": "blocked",
                        "draft_pr_url": None,
                    },
                },
            }
        )
        dispatch_path.write_text(json.dumps(dispatch), encoding="utf-8")
        return repo, workflow, approval, run_id

    def test_prepare_stops_at_one_combined_approval_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, _, record = self._prepared_run(Path(directory))

        self.assertEqual("awaiting_approval", record["status"])
        self.assertEqual(REPOSITORY, record["preview"]["repository"])
        self.assertEqual("gpt-5.6-sol", record["preview"]["copilot_model"])
        self.assertIn("create_draft_pr", record["preview"]["actions"])
        self.assertFalse(record["preview"]["auto_merge"])
        self.assertFalse(record["preview"]["deploy"])
        self.assertEqual(64, len(record["preview"]["approval_digest"]))

    def test_prepare_accepts_exact_existing_issue_for_fresh_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, workflow, _ = self._configured_workflow(root)
            issue_url = f"https://github.com/{REPOSITORY}/issues/17"
            with mock.patch(
                "src.local_control_center.threading.Thread"
            ), mock.patch(
                "src.local_control_center.ai_issue_generator.generate_issue",
                return_value=generation(),
            ), mock.patch(
                "src.local_control_center.automate_repository_issue",
                return_value={
                    "publication": {
                        "status": "deduplicated",
                        "repository": REPOSITORY,
                        "issue_url": issue_url,
                        "issue_number": 17,
                    }
                },
            ), mock.patch(
                "src.local_control_center.render_automated_issue_body",
                return_value=(
                    "# Synthetic reviewed Issue\n\nSafe body.",
                    "a" * 64,
                ),
            ):
                record = workflow.create("add multiplication and tests")
                workflow._prepare(record["run_id"], "add multiplication and tests")

            prepared = workflow.read(record["run_id"])

        self.assertEqual("awaiting_approval", prepared["status"])
        self.assertEqual("reuse_existing", prepared["preview"]["issue_mode"])
        self.assertEqual(issue_url, prepared["preview"]["existing_issue_url"])
        self.assertIn(
            "reuse_existing_github_issue",
            prepared["preview"]["actions"],
        )
        self.assertNotIn("publish_github_issue", prepared["preview"]["actions"])

    def test_high_entropy_input_has_actionable_safe_failure_audit(self):
        opaque = "QWxhZGRpbjpvcGVuIHNlc2FtZV9yYW5kb21WYWx1ZQ=="
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, workflow, _ = self._configured_workflow(root)
            description = f"请修复计算器，诊断片段为 {opaque}"
            with mock.patch("src.local_control_center.threading.Thread"):
                record = workflow.create(description)
            workflow._prepare(record["run_id"], description)

            blocked = workflow.read(record["run_id"])
            audit_path = (
                root / "runs" / record["run_id"] / "failure-audit.json"
            )
            audit_text = audit_path.read_text(encoding="utf-8")
            audit = json.loads(audit_text)

        self.assertEqual("blocked", blocked["status"])
        self.assertEqual("input_safety_blocked", blocked["failure"]["code"])
        self.assertIn("请删除该片段", blocked["failure"]["message"])
        self.assertEqual("input_sanitization", audit["stage"])
        self.assertEqual("unclassified_high_entropy", audit["reason_category"])
        self.assertFalse(audit["raw_input_recorded"])
        self.assertNotIn(opaque, audit_text)

    def test_configured_repository_slug_is_narrowly_allowlisted_as_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, workflow, _ = self._configured_workflow(root)
            config = workflow.config_store.load()
            evidence = _compose_managed_evidence(
                (
                    f"目标仓库是 {REPOSITORY}。请在 calculator 模块新增 "
                    "multiply 函数，并为正数、负数和零增加单元测试。"
                ),
                config,
            )

        self.assertEqual(REPOSITORY, evidence["facts"]["repository"])
        self.assertNotIn(
            str(repo),
            evidence["facts"]["reported_description"],
        )
        self.assertNotIn(
            REPOSITORY,
            evidence["facts"]["reported_description"],
        )
        self.assertTrue(evidence["safety"]["ai_allowed"])

    def test_single_enabled_repository_is_bound_without_user_naming_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, workflow, _ = self._configured_workflow(root)
            config = workflow.config_store.load()
            evidence = _compose_managed_evidence(
                "在计算器模块新增乘法功能，并添加正数、负数和零的测试。",
                config,
            )

        self.assertEqual(REPOSITORY, evidence["facts"]["repository"])

    def test_sanitized_log_evidence_enters_the_same_preparation_flow(self):
        evidence = {
            "schema_version": "ai-issue-evidence/v1",
            "source": {
                "type": "natural_language",
                "reference": "local_ref:sanitized",
                "url": "",
            },
            "safety": {"status": "sanitized", "ai_allowed": True},
            "facts": {
                "reported_description": "calculator add returns an incorrect result",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, workflow, _ = self._configured_workflow(root)
            with mock.patch("src.local_control_center.threading.Thread"):
                record = workflow.create_from_evidence(evidence)

        self.assertEqual("sanitized_evidence", record["input_type"])
        self.assertEqual(64, len(record["input_sha256"]))

    def test_approval_publishes_exact_issue_then_runs_claimed_draft_pr_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, workflow, approval, prepared = self._prepared_run(Path(directory))
            with mock.patch("src.local_control_center.threading.Thread"):
                workflow.approve(
                    prepared["run_id"],
                    prepared["preview"]["approval_digest"],
                )
            with mock.patch(
                "src.local_control_center.automate_repository_issue",
                return_value={
                    "publication": {
                        "status": "created",
                        "repository": REPOSITORY,
                        "issue_url": f"https://github.com/{REPOSITORY}/issues/17",
                    }
                },
            ), mock.patch(
                "src.local_control_center.dispatch_once",
                return_value={
                    "status": "draft_pr_created",
                    "dispatch": {
                        "modifier_report": {
                            "publication": {
                                "draft_pr_url": (
                                    f"https://github.com/{REPOSITORY}/pull/18"
                                )
                            }
                        }
                    },
                },
            ) as dispatch:
                workflow._execute(prepared["run_id"])

            completed = workflow.read(prepared["run_id"])

        self.assertEqual("completed", completed["status"])
        self.assertEqual(
            f"https://github.com/{REPOSITORY}/pull/18",
            completed["result"]["draft_pr_url"],
        )
        self.assertEqual(1, len(approval.calls))
        self.assertEqual(repo.resolve(), approval.calls[0][0].resolve())
        self.assertTrue(dispatch.call_args.kwargs["publish_pr"])
        self.assertEqual(
            f"https://github.com/{REPOSITORY}/issues/17",
            dispatch.call_args.kwargs["target_issue_url"],
        )
        self.assertEqual("gpt-5.6-sol", dispatch.call_args.kwargs["model"])

    def test_fresh_approval_can_reuse_exact_deduplicated_issue(self):
        with tempfile.TemporaryDirectory() as directory:
            _, workflow, approval, prepared = self._prepared_run(Path(directory))
            with mock.patch("src.local_control_center.threading.Thread"):
                workflow.approve(
                    prepared["run_id"],
                    prepared["preview"]["approval_digest"],
                )
            with mock.patch(
                "src.local_control_center.automate_repository_issue",
                return_value={
                    "publication": {
                        "status": "deduplicated",
                        "repository": REPOSITORY,
                        "issue_url": f"https://github.com/{REPOSITORY}/issues/17",
                    }
                },
            ), mock.patch(
                "src.local_control_center.dispatch_once",
                return_value={
                    "status": "draft_pr_created",
                    "dispatch": {
                        "modifier_report": {
                            "publication": {
                                "draft_pr_url": (
                                    f"https://github.com/{REPOSITORY}/pull/18"
                                )
                            }
                        }
                    },
                },
            ) as dispatch:
                workflow._execute(prepared["run_id"])

            completed = workflow.read(prepared["run_id"])

        self.assertEqual("completed", completed["status"])
        self.assertEqual(1, len(approval.calls))
        self.assertEqual(
            f"https://github.com/{REPOSITORY}/issues/17",
            dispatch.call_args.kwargs["target_issue_url"],
        )

    def test_retained_claim_resume_is_digest_bound_and_skips_republication(self):
        with tempfile.TemporaryDirectory() as directory:
            _, workflow, approval, run_id = self._retained_claim_run(
                Path(directory)
            )
            prepared = workflow.prepare_resume(run_id)
            preview = prepared["resume_preview"]
            with mock.patch("src.local_control_center.threading.Thread"):
                workflow.approve_resume(run_id, preview["approval_digest"])
            with mock.patch(
                "src.local_control_center.dispatch_once",
                return_value={
                    "status": "draft_pr_created",
                    "dispatch": {
                        "modifier_report": {
                            "publication": {
                                "draft_pr_url": (
                                    f"https://github.com/{REPOSITORY}/pull/18"
                                )
                            }
                        }
                    },
                },
            ) as dispatch:
                workflow._resume_execute(run_id)

            completed = workflow.read(run_id)

        self.assertEqual("completed", completed["status"])
        self.assertEqual([], approval.calls)
        self.assertEqual(
            "d" * 40,
            dispatch.call_args.kwargs["retained_claim_commit"],
        )
        self.assertEqual(
            "c" * 64,
            dispatch.call_args.kwargs["expected_issue_snapshot_sha256"],
        )
        self.assertNotIn("claimer", dispatch.call_args.kwargs)
        self.assertEqual(
            f"https://github.com/{REPOSITORY}/issues/17",
            dispatch.call_args.kwargs["target_issue_url"],
        )

    def test_retained_claim_resume_rejects_changed_claim_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            _, workflow, _, run_id = self._retained_claim_run(Path(directory))
            dispatch_path = workflow._run_dir(run_id) / "dispatch.json"
            dispatch = json.loads(dispatch_path.read_text(encoding="utf-8"))
            dispatch["dispatch"]["claim"]["remote_commit"] = "invalid"
            dispatch_path.write_text(json.dumps(dispatch), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "不允许恢复"):
                workflow.prepare_resume(run_id)

    def test_empty_modification_resume_removes_only_the_audited_empty_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, workflow, approval, run_id = self._empty_modification_run(
                Path(directory)
            )
            prepared = workflow.prepare_resume(run_id)
            preview = prepared["resume_preview"]
            self.assertTrue(preview["remove_empty_work_branch"])
            self.assertIn(
                "remove_empty_local_work_branch",
                preview["actions"],
            )
            with mock.patch("src.local_control_center.threading.Thread"):
                workflow.approve_resume(run_id, preview["approval_digest"])
            with mock.patch(
                "src.local_control_center.dispatch_once",
                return_value={
                    "status": "draft_pr_created",
                    "dispatch": {
                        "modifier_report": {
                            "publication": {
                                "draft_pr_url": (
                                    f"https://github.com/{REPOSITORY}/pull/18"
                                )
                            }
                        }
                    },
                },
            ):
                workflow._resume_execute(run_id)

            current_branch = subprocess.run(
                ["git", "-C", str(repo), "branch", "--show-current"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            work_branch_exists = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "show-ref",
                    "--verify",
                    "--quiet",
                    f"refs/heads/{preview['work_branch']}",
                ],
                check=False,
            ).returncode
            completed = workflow.read(run_id)

        self.assertEqual("completed", completed["status"])
        self.assertEqual([], approval.calls)
        self.assertEqual("main", current_branch)
        self.assertEqual(1, work_branch_exists)

    def test_empty_modification_resume_rejects_nonempty_change_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            _, workflow, _, run_id = self._empty_modification_run(
                Path(directory)
            )
            dispatch_path = workflow._run_dir(run_id) / "dispatch.json"
            dispatch = json.loads(dispatch_path.read_text(encoding="utf-8"))
            dispatch["dispatch"]["modifier_report"]["changes"]["paths"] = [
                "src/calculator.py"
            ]
            dispatch_path.write_text(json.dumps(dispatch), encoding="utf-8")

            self.assertFalse(
                _is_resumable_empty_modification(dispatch["dispatch"])
            )
            with self.assertRaisesRegex(ValueError, "不允许自动恢复"):
                workflow.prepare_resume(run_id)

    def test_second_empty_modification_resume_appends_a_new_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, workflow, _, run_id = self._empty_modification_run(
                Path(directory)
            )
            run_dir = workflow._run_dir(run_id)
            first_resume = json.loads(
                (run_dir / "dispatch.json").read_text(encoding="utf-8")
            )
            first_resume["dispatch"]["modifier_report"]["modification"][
                "work_branch_removed"
            ] = True
            (run_dir / "dispatch-resume.json").write_text(
                json.dumps(first_resume),
                encoding="utf-8",
            )
            work_branch = first_resume["candidates"][0]["work_branch"]
            subprocess.run(
                ["git", "-C", str(repo), "switch", "main"],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            subprocess.run(
                ["git", "-C", str(repo), "branch", "-D", work_branch],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            record = workflow.read(run_id)
            record.update(
                {
                    "status": "blocked",
                    "failure": {
                        "code": "resume_dispatch_failed",
                        "message": "blocked",
                    },
                }
            )
            workflow._write_record(run_id, record)

            prepared = workflow.prepare_resume(run_id)
            preview = prepared["resume_preview"]
            self.assertEqual(2, preview["resume_attempt"])
            self.assertEqual("dispatch-resume.json", preview["source_audit"])
            self.assertEqual("dispatch-resume-2.json", preview["resume_output"])
            self.assertFalse(preview["remove_empty_work_branch"])
            with mock.patch("src.local_control_center.threading.Thread"):
                workflow.approve_resume(run_id, preview["approval_digest"])
            with mock.patch(
                "src.local_control_center.dispatch_once",
                return_value={
                    "status": "draft_pr_created",
                    "dispatch": {
                        "modifier_report": {
                            "publication": {
                                "draft_pr_url": (
                                    f"https://github.com/{REPOSITORY}/pull/18"
                                )
                            }
                        }
                    },
                },
            ):
                workflow._resume_execute(run_id)

            first_audit = run_dir / "dispatch-resume.json"
            second_audit = run_dir / "dispatch-resume-2.json"
            completed = workflow.read(run_id)
            first_audit_exists = first_audit.exists()
            second_audit_exists = second_audit.exists()

        self.assertEqual("completed", completed["status"])
        self.assertTrue(first_audit_exists)
        self.assertTrue(second_audit_exists)

    def test_approval_digest_must_match_displayed_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            _, workflow, _, prepared = self._prepared_run(Path(directory))

            with self.assertRaisesRegex(ValueError, "displayed plan"):
                workflow.approve(prepared["run_id"], "f" * 64)


if __name__ == "__main__":
    unittest.main()
