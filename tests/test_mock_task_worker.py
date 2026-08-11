import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.mock_task_worker import (
    ApprovedIssueDispatchExecutionEngine,
    CodeExecutionPreapprovalEngine,
    ControlPlaneClient,
    ExecutionResult,
    LocalRepositoryExecutionEngine,
    NaturalLanguageIssueExecutionEngine,
    PublishedIssueWorkerError,
    StaleTaskError,
    WorkerConfig,
    WorkerError,
    process_task,
    run_once,
    validate_task_id,
)


TASK_ID = "3f08ea61-71b4-42de-bc8e-608a18bba522"


class FakeQueue:
    def __init__(self, task_id):
        self.task_id = task_id
        self.timeouts = []

    def next_task_id(self, timeout_seconds):
        self.timeouts.append(timeout_seconds)
        return self.task_id


class FakeClient:
    def __init__(self, claim_error=None, failing_status=None):
        self.claim_error = claim_error
        self.failing_status = failing_status
        self.calls = []

    def claim(self, task_id):
        self.calls.append(("claim", task_id))
        if self.claim_error:
            raise self.claim_error
        return {
            "taskId": task_id,
            "executionMode": "MOCK",
            "sourceType": "NATURAL_LANGUAGE",
            "issueProfile": "NATURAL_LANGUAGE",
            "normalizedRequirement": "calculator divide in src/calculator.py",
            "matchedRepository": "wzf12400/ai-pr-sandbox",
        }

    def transition(self, task_id, target_status, detail):
        self.calls.append(("transition", task_id, target_status, detail))
        if target_status == self.failing_status:
            raise WorkerError("synthetic failure")

    def attach_issue(self, task_id, issue_number, issue_url):
        self.calls.append(("attach_issue", task_id, issue_number, issue_url))

    def attach_pull_request(self, task_id, pr_number, pr_url, test_summary):
        self.calls.append(
            ("attach_pull_request", task_id, pr_number, pr_url, test_summary)
        )


class FakeEngine:
    def __init__(self, result=None, error=None):
        self.result = result or ExecutionResult(
            "COMPLETED",
            "只读定位完成；未修改仓库",
            2,
        )
        self.error = error
        self.claims = []

    def execute(self, claim):
        self.claims.append(claim)
        if self.error:
            raise self.error
        return self.result


class MockTaskWorkerTest(unittest.TestCase):
    def test_processes_one_task_through_testing_and_completed(self):
        client = FakeClient()
        engine = FakeEngine()

        result = process_task(TASK_ID, client, engine)

        self.assertEqual("completed", result)
        self.assertEqual("claim", client.calls[0][0])
        self.assertEqual("TESTING", client.calls[1][2])
        self.assertEqual("COMPLETED", client.calls[2][2])
        self.assertEqual(1, len(engine.claims))
        self.assertIn("候选文件 2 个", client.calls[1][3])
        self.assertEqual("只读定位完成；未修改仓库", client.calls[2][3])

    def test_moves_to_needs_context_when_locator_has_no_candidates(self):
        client = FakeClient()
        engine = FakeEngine(ExecutionResult("NEEDS_CONTEXT", "请补充函数名"))

        result = process_task(TASK_ID, client, engine)

        self.assertEqual("needs_context", result)
        self.assertEqual(["NEEDS_CONTEXT"], [call[2] for call in client.calls[1:]])

    def test_records_issue_reference_before_testing(self):
        client = FakeClient()
        issue_url = "https://github.com/wzf12400/ai-pr-sandbox/issues/42"
        engine = FakeEngine(
            ExecutionResult(
                "COMPLETED",
                "Issue 已创建并完成定位",
                2,
                issue_number=42,
                issue_url=issue_url,
            )
        )

        result = process_task(TASK_ID, client, engine)

        self.assertEqual("completed", result)
        self.assertEqual("attach_issue", client.calls[1][0])
        self.assertEqual((42, issue_url), client.calls[1][2:])
        self.assertEqual("TESTING", client.calls[2][2])

    def test_records_tested_draft_pr_before_waiting_for_review(self):
        client = FakeClient()
        issue_url = "https://github.com/wzf12400/ai-pr-sandbox/issues/42"
        pr_url = "https://github.com/wzf12400/ai-pr-sandbox/pull/9"
        engine = FakeEngine(
            ExecutionResult(
                "AWAITING_PR_REVIEW",
                "Draft PR 等待人工审核",
                issue_number=42,
                issue_url=issue_url,
                pr_number=9,
                pr_url=pr_url,
                test_summary="策略测试 1 项通过",
            )
        )

        result = process_task(TASK_ID, client, engine)

        self.assertEqual("awaiting_pr_review", result)
        self.assertEqual(
            ["attach_issue", "transition", "attach_pull_request", "transition"],
            [call[0] for call in client.calls[1:]],
        )
        self.assertEqual("TESTING", client.calls[2][2])
        self.assertEqual((9, pr_url), client.calls[3][2:4])
        self.assertEqual("AWAITING_PR_REVIEW", client.calls[4][2])

    def test_skips_a_stale_duplicate_queue_item(self):
        client = FakeClient(claim_error=StaleTaskError("already claimed"))

        result = process_task(TASK_ID, client)

        self.assertEqual("stale", result)
        self.assertEqual([("claim", TASK_ID)], client.calls)

    def test_marks_task_failed_when_claim_contract_cannot_be_validated(self):
        client = FakeClient(claim_error=WorkerError("invalid claim"))

        result = process_task(TASK_ID, client)

        self.assertEqual("failed", result)
        self.assertEqual("FAILED", client.calls[1][2])

    def test_marks_a_claimed_task_failed_when_mock_execution_fails(self):
        client = FakeClient(failing_status="TESTING")

        result = process_task(TASK_ID, client)

        self.assertEqual("failed", result)
        self.assertEqual(["TESTING", "FAILED"], [call[2] for call in client.calls[1:]])

    def test_marks_a_claimed_task_failed_when_local_engine_rejects_checkout(self):
        client = FakeClient()
        engine = FakeEngine(error=WorkerError("unsafe checkout"))

        result = process_task(TASK_ID, client, engine)

        self.assertEqual("failed", result)
        self.assertEqual(["FAILED"], [call[2] for call in client.calls[1:]])

    def test_preserves_issue_reference_when_post_issue_flow_fails(self):
        client = FakeClient()
        issue_url = "https://github.com/wzf12400/ai-pr-sandbox/issues/42"
        engine = FakeEngine(error=PublishedIssueWorkerError(42, issue_url))

        result = process_task(TASK_ID, client, engine)

        self.assertEqual("failed", result)
        self.assertEqual("attach_issue", client.calls[1][0])
        self.assertEqual((42, issue_url), client.calls[1][2:])
        self.assertEqual("FAILED", client.calls[2][2])

    def test_run_once_returns_without_processing_when_queue_is_empty(self):
        queue = FakeQueue(None)
        client = FakeClient()

        result = run_once(queue, client, 7)

        self.assertEqual("no_task", result)
        self.assertEqual([7], queue.timeouts)
        self.assertEqual([], client.calls)

    def test_rejects_non_local_control_plane_configuration(self):
        with patch.dict(
            os.environ,
            {
                "CONTROL_PLANE_URL": "https://example.com",
                "REDIS_URL": "redis://127.0.0.1:6379/0",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(WorkerError, "CONTROL_PLANE_URL"):
                WorkerConfig.from_environment(5)

    def test_rejects_invalid_queue_identifier(self):
        with self.assertRaisesRegex(WorkerError, "valid task identifier"):
            validate_task_id("not-a-task-id")

    def test_rejects_non_finite_request_timeout(self):
        with patch.dict(
            os.environ,
            {
                "CONTROL_PLANE_URL": "http://127.0.0.1:8080",
                "REDIS_URL": "redis://127.0.0.1:6379/0",
                "WORKER_REQUEST_TIMEOUT_SECONDS": "nan",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(WorkerError, "between 0 and 30"):
                WorkerConfig.from_environment(5)

    def test_issue_publication_requires_confirmed_policy_digest(self):
        with patch.dict(
            os.environ,
            {
                "CONTROL_PLANE_URL": "http://127.0.0.1:8080",
                "REDIS_URL": "redis://127.0.0.1:6379/0",
                "WORKER_ISSUE_PUBLICATION_ENABLED": "true",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(WorkerError, "POLICY_SHA256"):
                WorkerConfig.from_environment(5)

    def test_code_mode_requires_policy_pinned_issue_publication(self):
        with patch.dict(
            os.environ,
            {
                "CONTROL_PLANE_URL": "http://127.0.0.1:8080",
                "REDIS_URL": "redis://127.0.0.1:6379/0",
                "WORKER_CODE_MODE": "publish_pr",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(WorkerError, "ISSUE_PUBLICATION_ENABLED"):
                WorkerConfig.from_environment(5)

    def test_code_mode_configuration_is_explicit_and_default_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repo"
            with patch.dict(
                os.environ,
                {
                    "CONTROL_PLANE_URL": "http://127.0.0.1:8080",
                    "REDIS_URL": "redis://127.0.0.1:6379/0",
                    "WORKER_REPOSITORY_PATH": str(repository),
                },
                clear=True,
            ):
                config = WorkerConfig.from_environment(5)

        self.assertEqual("disabled", config.code_mode)
        self.assertEqual(
            repository.resolve() / ".github" / "issue-code-policy.json",
            config.code_policy_path,
        )
        self.assertFalse(config.code_auto_approval_enabled)

    def test_automatic_code_approval_requires_digest_and_active_flow(self):
        base = {
            "CONTROL_PLANE_URL": "http://127.0.0.1:8080",
            "REDIS_URL": "redis://127.0.0.1:6379/0",
            "WORKER_ISSUE_PUBLICATION_ENABLED": "true",
            "WORKER_ISSUE_POLICY_SHA256": "a" * 64,
            "WORKER_CODE_MODE": "publish_pr",
            "WORKER_CODE_AUTO_APPROVAL_ENABLED": "true",
        }
        with patch.dict(os.environ, base, clear=True):
            with self.assertRaisesRegex(WorkerError, "AUTO_APPROVAL_POLICY_SHA256"):
                WorkerConfig.from_environment(5)

        with patch.dict(
            os.environ,
            {
                **base,
                "WORKER_CODE_MODE": "disabled",
                "WORKER_CODE_AUTO_APPROVAL_POLICY_SHA256": "b" * 64,
            },
            clear=True,
        ):
            with self.assertRaisesRegex(WorkerError, "non-disabled code mode"):
                WorkerConfig.from_environment(5)

    def test_company_preapproval_applies_labels_only_for_new_log_issue(self):
        issue_client = Mock()
        issue_client.add_labels.return_value = ("ai-code-approved",)
        engine = CodeExecutionPreapprovalEngine(
            issue_client,
            Path("preapproval.json"),
            "a" * 64,
            Path("issue-policy.json"),
            Path("code-policy.json"),
        )
        policy = Mock(
            repository="wzf12400/ai-pr-sandbox",
        )
        policy.labels_for.side_effect = lambda source, status: (
            ("ai-code-approved",)
            if source == "LOG" and status == "created"
            else ()
        )
        with patch(
            "src.code_execution_preapproval.load_code_execution_preapproval_policy",
            return_value=policy,
        ):
            applied = engine.apply(
                {"sourceType": "LOG"},
                "wzf12400/ai-pr-sandbox",
                42,
                "https://github.com/wzf12400/ai-pr-sandbox/issues/42",
                "created",
            )
            skipped = engine.apply(
                {"sourceType": "NATURAL_LANGUAGE"},
                "wzf12400/ai-pr-sandbox",
                43,
                "https://github.com/wzf12400/ai-pr-sandbox/issues/43",
                "created",
            )

        self.assertEqual(("ai-code-approved",), applied)
        self.assertEqual((), skipped)
        issue_client.add_labels.assert_called_once_with(
            "wzf12400/ai-pr-sandbox", 42, ("ai-code-approved",)
        )

    def test_claim_contract_rejects_production_mode(self):
        claim = {
            "taskId": TASK_ID,
            "executionMode": "PRODUCTION",
            "sourceType": "NATURAL_LANGUAGE",
            "issueProfile": "NATURAL_LANGUAGE",
            "normalizedRequirement": "支付订单分页",
            "matchedRepository": "demo-company/payment-service",
        }

        with self.assertRaisesRegex(WorkerError, "MOCK tasks only"):
            ControlPlaneClient._validate_claim(TASK_ID, claim)

    def test_local_repository_engine_reuses_locator_without_modifying_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            (repository / "src").mkdir()
            (repository / "tests").mkdir()
            (repository / "src" / "calculator.py").write_text(
                "def divide(left, right):\n    return left / right\n",
                encoding="utf-8",
            )
            (repository / "tests" / "test_calculator.py").write_text(
                "from src.calculator import divide\n",
                encoding="utf-8",
            )
            self._git(repository, "init", "-b", "main")
            self._git(repository, "config", "user.name", "Worker Test")
            self._git(repository, "config", "user.email", "worker@example.invalid")
            self._git(
                repository,
                "remote",
                "add",
                "origin",
                "https://github.com/wzf12400/ai-pr-sandbox.git",
            )
            self._git(repository, "add", ".")
            self._git(repository, "commit", "-m", "fixture")
            before = self._git(repository, "status", "--porcelain")

            engine = LocalRepositoryExecutionEngine(
                "wzf12400/ai-pr-sandbox",
                repository,
            )
            result = engine.execute(
                {
                    "matchedRepository": "wzf12400/ai-pr-sandbox",
                    "normalizedRequirement": (
                        "calculator divide 遇到零时返回错误，检查 src/calculator.py"
                    ),
                }
            )

            self.assertEqual("COMPLETED", result.target_status)
            self.assertIn("src/calculator.py", result.detail)
            self.assertEqual(before, self._git(repository, "status", "--porcelain"))

    def test_log_claim_builds_expanded_deterministic_observability(self):
        claim = {
            "sourceType": "LOG",
            "logIncident": {
                "sourceReference": "incident_ref:0123456789abcdefabcd",
                "firstSeenAt": "2026-08-04T01:00:00Z",
                "lastSeenAt": "2026-08-04T02:00:00Z",
                "currentScanEventCount": 5,
                "historicalEventCount": 18,
                "incidentGroupCount": 2,
                "affectedEndpoints": ["/api/orders"],
                "affectedUserCountMin": 3,
                "affectedUserCountMax": 7,
                "userIdentifierEventCount": 12,
                "historicalCountComplete": True,
                "aggregationBasis": "service=payment; exception=NullPointerException",
            },
        }

        evidence = NaturalLanguageIssueExecutionEngine._compose_log_evidence(
            claim,
            "payment order NullPointerException",
        )

        self.assertEqual("kibana", evidence["source"]["type"])
        self.assertEqual(
            "2026-08-04T01:00:00Z",
            evidence["runtime"]["first_seen_at"],
        )
        statistics = evidence["event"]["statistics"]
        self.assertEqual(5, statistics["batch_event_count"])
        self.assertEqual(18, statistics["total_event_count"])
        self.assertEqual(["/api/orders"], statistics["affected_endpoints"])

    def test_log_claim_preserves_explicit_current_and_expected_behavior(self):
        claim = {
            "sourceType": "LOG",
            "logIncident": {
                "sourceReference": "incident_ref:0123456789abcdefabcd",
                "firstSeenAt": "2026-08-04T01:00:00Z",
                "lastSeenAt": "2026-08-04T02:00:00Z",
                "currentScanEventCount": 1,
                "historicalEventCount": 1,
                "incidentGroupCount": 1,
                "affectedEndpoints": ["calculator.add"],
                "affectedUserCountMin": 0,
                "affectedUserCountMax": 0,
                "userIdentifierEventCount": 0,
                "historicalCountComplete": True,
                "aggregationBasis": "module=calculator; operation=add",
            },
        }

        evidence = NaturalLanguageIssueExecutionEngine._compose_log_evidence(
            claim,
            "实际行为：add(-51, 1) 返回 -50；期望行为：绝对值超过 50 时抛出 ValueError",
        )

        self.assertEqual(
            "add(-51, 1) 返回 -50",
            evidence["facts"]["current_behavior"],
        )
        self.assertEqual(
            "绝对值超过 50 时抛出 ValueError",
            evidence["facts"]["expected_behavior"],
        )
        self.assertEqual(
            evidence["facts"]["current_behavior"],
            evidence["event"]["summary"],
        )

    def test_local_repository_engine_rejects_mismatched_repository(self):
        engine = LocalRepositoryExecutionEngine(
            "wzf12400/ai-pr-sandbox",
            Path("/does/not/matter"),
        )

        with self.assertRaisesRegex(WorkerError, "not authorized"):
            engine.execute(
                {
                    "matchedRepository": "someone/other-repository",
                    "normalizedRequirement": "change calculator",
                }
            )

    def test_local_repository_engine_rejects_dirty_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            self._git(repository, "init", "-b", "main")
            self._git(
                repository,
                "remote",
                "add",
                "origin",
                "https://github.com/wzf12400/ai-pr-sandbox.git",
            )
            (repository / "untracked.py").write_text("value = 1\n", encoding="utf-8")
            engine = LocalRepositoryExecutionEngine(
                "wzf12400/ai-pr-sandbox",
                repository,
            )

            with self.assertRaisesRegex(WorkerError, "must remain clean"):
                engine.execute(
                    {
                        "matchedRepository": "wzf12400/ai-pr-sandbox",
                        "normalizedRequirement": "change calculator",
                    }
                )

    def test_issue_engine_publishes_then_returns_reference_for_control_plane(self):
        location_engine = FakeEngine(
            ExecutionResult("COMPLETED", "只读定位完成", 2)
        )
        issue_client = Mock()
        issue_client.get_issue.return_value = {
            "number": 42,
            "title": "Calculator divide validation",
            "body": "Sanitized approved Issue body.",
            "url": "https://github.com/wzf12400/ai-pr-sandbox/issues/42",
            "state": "open",
            "repository_url": "https://api.github.com/repos/wzf12400/ai-pr-sandbox",
        }
        issue_engine = NaturalLanguageIssueExecutionEngine(
            location_engine,
            issue_client,
            Path("scope.json"),
            Path("policy.json"),
            "a" * 64,
        )
        gateway = Mock(model="generator-model", review_model="review-model")
        publication = {
            "publication": {
                "status": "created",
                "issue_number": 42,
                "issue_url": "https://github.com/wzf12400/ai-pr-sandbox/issues/42",
            }
        }

        with patch(
            "src.issue_entry.compose_evidence",
            return_value={"safety": {"ai_allowed": True}},
        ), patch(
            "src.ai_issue_generator.GatewayConfig.from_env",
            return_value=gateway,
        ), patch(
            "src.ai_issue_generator.generate_issue",
            return_value={"state": "ready_for_human_review"},
        ), patch(
            "src.repository_resolver.load_search_scope",
            return_value=Mock(),
        ), patch(
            "src.repository_issue_automation.load_auto_publish_policy",
            return_value=Mock(provider="github_rest_api"),
        ), patch(
            "src.repository_issue_automation.automate_repository_issue",
            return_value=publication,
        ):
            result = issue_engine.execute(
                {
                    "matchedRepository": "wzf12400/ai-pr-sandbox",
                    "normalizedRequirement": "calculator divide validation",
                }
            )

        self.assertEqual(42, result.issue_number)
        self.assertEqual(
            "https://github.com/wzf12400/ai-pr-sandbox/issues/42",
            result.issue_url,
        )
        self.assertIn("Issue #42 已创建并重新读取", result.detail)
        self.assertEqual(1, len(location_engine.claims))
        self.assertEqual(
            "Calculator divide validation",
            location_engine.claims[0]["approvedIssue"]["title"],
        )
        issue_client.get_issue.assert_called_once_with(
            "wzf12400/ai-pr-sandbox",
            42,
        )

    def test_downstream_engine_reuses_original_dispatcher_for_exact_issue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit_dir = root / "audit"
            engine = ApprovedIssueDispatchExecutionEngine(
                root,
                root / ".github" / "issue-code-policy.json",
                "publish_pr",
                "",
                30,
                audit_dir,
            )
            report = {
                "status": "draft_pr_created",
                "dispatch": {
                    "modifier_report": {
                        "changes": {"paths": ["src/calculator.py"]},
                        "tests": [{"returncode": 0}],
                        "publication": {
                            "draft_pr_url": (
                                "https://github.com/wzf12400/ai-pr-sandbox/pull/9"
                            )
                        },
                    }
                },
            }

            with patch(
                "src.approved_issue_dispatcher.dispatch_once",
                return_value=report,
            ) as dispatcher:
                result = engine.execute(
                    {"taskId": TASK_ID},
                    "https://github.com/wzf12400/ai-pr-sandbox/issues/42",
                )

            self.assertEqual("AWAITING_PR_REVIEW", result.target_status)
            self.assertEqual(9, result.pr_number)
            self.assertIn("策略测试 1 项通过", result.test_summary)
            self.assertTrue((audit_dir / f"task-{TASK_ID}-publish_pr.json").exists())
            call = dispatcher.call_args
            self.assertEqual(
                "https://github.com/wzf12400/ai-pr-sandbox/issues/42",
                call.kwargs["target_issue_url"],
            )
            self.assertTrue(call.kwargs["execute"])
            self.assertTrue(call.kwargs["publish_pr"])

    @staticmethod
    def _git(repository, *args):
        return subprocess.run(
            ["git", "-C", str(repository), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.strip()


if __name__ == "__main__":
    unittest.main()
