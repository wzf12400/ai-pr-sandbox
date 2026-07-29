import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from src.repository_routing_benchmark import BLOCKED_STATEMENT
from src.swebench_codefix_benchmark import (
    LABEL_SCHEMA_VERSION,
    RESULT_SCHEMA_VERSION,
    TASK_SCHEMA_VERSION,
    evaluate_main,
    evaluate_swebench_codefix_results,
    import_harness_main,
    import_swebench_harness_reports,
    prepare_main,
    prepare_swebench_codefix_records,
    render_evaluation_markdown,
    select_deterministic_rows,
)


def swebench_row(
    instance_id="example__project-101",
    problem_statement="WidgetParser fails when the input is empty.",
    fail_to_pass=None,
    pass_to_pass=None,
):
    return {
        "instance_id": instance_id,
        "repo": "example/project",
        "problem_statement": problem_statement,
        "base_commit": "a" * 40,
        "created_at": "2024-01-02T03:04:05Z",
        "version": "1.2.3",
        "environment_setup_commit": "b" * 40,
        "patch": "gold solution must remain private",
        "test_patch": "gold tests must remain private",
        "FAIL_TO_PASS": json.dumps(
            fail_to_pass
            if fail_to_pass is not None
            else ["tests/test_widget.py::test_empty"]
        ),
        "PASS_TO_PASS": json.dumps(
            pass_to_pass
            if pass_to_pass is not None
            else ["tests/test_widget.py::test_value"]
        ),
        "issue_url": "https://github.com/example/project/issues/100",
        "pr_url": "https://github.com/example/project/pull/101",
    }


def completed_result(label, fail_passed=None, fail_failed=None, pass_passed=None):
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "case_ref": label["case_ref"],
        "status": "completed",
        "fail_to_pass_passed": (
            list(label["fail_to_pass"]) if fail_passed is None else fail_passed
        ),
        "fail_to_pass_failed": [] if fail_failed is None else fail_failed,
        "pass_to_pass_passed": (
            list(label["pass_to_pass"]) if pass_passed is None else pass_passed
        ),
        "pass_to_pass_failed": [],
    }


class SwebenchCodefixBenchmarkTest(unittest.TestCase):
    def test_prepares_agent_tasks_and_private_labels_without_answer_leakage(self):
        tasks, labels, summary = prepare_swebench_codefix_records(
            [swebench_row()],
            "SWE-bench/SWE-bench_Verified",
            "fixture-revision",
        )

        self.assertEqual(1, len(tasks))
        self.assertEqual(1, len(labels))
        task = tasks[0]
        label = labels[0]
        self.assertEqual(TASK_SCHEMA_VERSION, task["schema_version"])
        self.assertEqual(LABEL_SCHEMA_VERSION, label["schema_version"])
        self.assertEqual("example/project", task["repository"])
        self.assertEqual("a" * 40, task["base_commit"])
        self.assertEqual("eligible", task["preflight"]["status"])
        self.assertFalse(task["answer_fields_present"])
        self.assertEqual(
            ["tests/test_widget.py::test_empty"],
            label["fail_to_pass"],
        )
        serialized_task = json.dumps(task, sort_keys=True)
        for secret_value in (
            "example__project-101",
            "gold solution must remain private",
            "gold tests must remain private",
            "tests/test_widget.py::test_empty",
            "tests/test_widget.py::test_value",
            "issues/100",
            "pull/101",
        ):
            self.assertNotIn(secret_value, serialized_task)
        self.assertFalse(summary["gold_patch_content_written"])
        self.assertFalse(summary["test_patch_content_written"])
        self.assertFalse(summary["test_names_written_to_tasks"])

    def test_sensitive_problem_is_blocked_without_persisting_raw_statement(self):
        tasks, labels, summary = prepare_swebench_codefix_records(
            [
                swebench_row(
                    problem_statement=(
                        "Failure includes api_key="
                        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
                    )
                )
            ],
            "SWE-bench/SWE-bench_Verified",
            "fixture-revision",
        )

        self.assertEqual(BLOCKED_STATEMENT, tasks[0]["problem_statement"])
        self.assertEqual("blocked", tasks[0]["preflight"]["status"])
        self.assertEqual("blocked", labels[0]["expected_execution"])
        self.assertEqual(1, summary["blocked_sensitive_rows"])

    def test_rejects_empty_or_overlapping_test_partitions(self):
        with self.assertRaisesRegex(ValueError, "FAIL_TO_PASS must not be empty"):
            prepare_swebench_codefix_records(
                [swebench_row(fail_to_pass=[])],
                "dataset",
                "revision",
            )
        with self.assertRaisesRegex(ValueError, "partitions must be disjoint"):
            prepare_swebench_codefix_records(
                [
                    swebench_row(
                        fail_to_pass=["tests/test_widget.py::test_same"],
                        pass_to_pass=["tests/test_widget.py::test_same"],
                    )
                ],
                "dataset",
                "revision",
            )

    def test_deterministic_sampling_is_order_independent(self):
        rows = [
            swebench_row(instance_id=f"example__project-{index}")
            for index in range(10)
        ]
        first = select_deterministic_rows(rows, 3, "seed")
        second = select_deterministic_rows(list(reversed(rows)), 3, "seed")
        self.assertEqual(
            [row["instance_id"] for row in first],
            [row["instance_id"] for row in second],
        )

    def test_evaluation_requires_all_f2p_and_p2p_tests(self):
        _, labels, _ = prepare_swebench_codefix_records(
            [
                swebench_row(
                    fail_to_pass=[
                        "tests/test_widget.py::test_empty",
                        "tests/test_widget.py::test_none",
                    ]
                )
            ],
            "dataset",
            "revision",
        )
        label = labels[0]
        partial = completed_result(
            label,
            fail_passed=["tests/test_widget.py::test_empty"],
            fail_failed=["tests/test_widget.py::test_none"],
        )
        report = evaluate_swebench_codefix_results(labels, [partial])

        self.assertEqual(0, report["counts"]["resolved"])
        self.assertEqual(0.5, report["metrics"]["fail_to_pass_rate"])
        self.assertEqual(1.0, report["metrics"]["pass_to_pass_rate"])
        self.assertEqual(0.0, report["metrics"]["resolved_rate"])

        resolved = evaluate_swebench_codefix_results(
            labels,
            [completed_result(label)],
        )
        self.assertEqual(1, resolved["counts"]["resolved"])
        self.assertEqual(1.0, resolved["metrics"]["resolved_rate"])
        self.assertIn("Fully resolved cases: 1", render_evaluation_markdown(resolved))

    def test_evaluation_counts_missing_results_and_blocked_execution_violation(self):
        safe = swebench_row(instance_id="example__project-201")
        blocked = swebench_row(
            instance_id="example__project-202",
            problem_statement=(
                "Failure includes api_key="
                "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
            ),
        )
        _, labels, _ = prepare_swebench_codefix_records(
            [safe, blocked],
            "dataset",
            "revision",
        )
        report = evaluate_swebench_codefix_results(
            labels,
            [completed_result(labels[1])],
        )

        self.assertEqual(1, report["counts"]["eligible"])
        self.assertEqual(1, report["counts"]["blocked"])
        self.assertEqual(1, report["counts"]["missing_or_incomplete"])
        self.assertEqual(1, report["counts"]["blocked_execution_violations"])
        self.assertEqual(0.0, report["metrics"]["execution_coverage"])

    def test_completed_result_must_exactly_cover_private_tests(self):
        _, labels, _ = prepare_swebench_codefix_records(
            [swebench_row()],
            "dataset",
            "revision",
        )
        bad_result = completed_result(
            labels[0],
            fail_passed=["tests/test_widget.py::unknown"],
        )
        with self.assertRaisesRegex(ValueError, "exactly cover expected tests"):
            evaluate_swebench_codefix_results(labels, [bad_result])

    def test_imports_official_harness_report_without_instance_id_leakage(self):
        _, labels, _ = prepare_swebench_codefix_records(
            [
                swebench_row(instance_id="example__project-301"),
                swebench_row(instance_id="example__project-302"),
            ],
            "dataset",
            "revision",
        )
        successful = {
            labels[0]["instance_id"]: {
                "patch_exists": True,
                "patch_is_None": False,
                "patch_successfully_applied": True,
                "resolved": True,
                "tests_status": {
                    "FAIL_TO_FAIL": {"success": [], "failure": []},
                    "FAIL_TO_PASS": {
                        "success": labels[0]["fail_to_pass"],
                        "failure": [],
                    },
                    "PASS_TO_FAIL": {"success": [], "failure": []},
                    "PASS_TO_PASS": {
                        "success": labels[0]["pass_to_pass"],
                        "failure": [],
                    },
                },
            }
        }
        patch_error = {
            labels[1]["instance_id"]: {
                "patch_successfully_applied": False,
                "resolved": False,
            }
        }

        results = import_swebench_harness_reports(
            labels,
            [successful, patch_error],
        )

        self.assertEqual("completed", results[0]["status"])
        self.assertEqual("error", results[1]["status"])
        serialized = json.dumps(results, sort_keys=True)
        self.assertNotIn(labels[0]["instance_id"], serialized)
        self.assertNotIn(labels[1]["instance_id"], serialized)

    def test_harness_import_fails_closed_on_incomplete_or_conflicting_report(self):
        _, labels, _ = prepare_swebench_codefix_records(
            [swebench_row()],
            "dataset",
            "revision",
        )
        label = labels[0]
        incomplete = {
            label["instance_id"]: {
                "patch_successfully_applied": True,
                "resolved": False,
                "tests_status": {
                    "FAIL_TO_PASS": {"success": [], "failure": []},
                    "PASS_TO_PASS": {
                        "success": label["pass_to_pass"],
                        "failure": [],
                    },
                },
            }
        }
        with self.assertRaisesRegex(ValueError, "exactly cover private tests"):
            import_swebench_harness_reports(labels, [incomplete])

        conflicting = {
            label["instance_id"]: {
                "patch_successfully_applied": True,
                "resolved": False,
                "tests_status": {
                    "FAIL_TO_PASS": {
                        "success": label["fail_to_pass"],
                        "failure": [],
                    },
                    "PASS_TO_PASS": {
                        "success": label["pass_to_pass"],
                        "failure": [],
                    },
                },
            }
        }
        with self.assertRaisesRegex(ValueError, "conflicts with test outcomes"):
            import_swebench_harness_reports(labels, [conflicting])

        unsupported_auxiliary = {
            label["instance_id"]: {
                "patch_successfully_applied": True,
                "resolved": True,
                "tests_status": {
                    "FAIL_TO_FAIL": {
                        "success": ["unexpected auxiliary test"],
                        "failure": [],
                    },
                    "FAIL_TO_PASS": {
                        "success": label["fail_to_pass"],
                        "failure": [],
                    },
                    "PASS_TO_PASS": {
                        "success": label["pass_to_pass"],
                        "failure": [],
                    },
                },
            }
        }
        with self.assertRaisesRegex(ValueError, "unsupported auxiliary"):
            import_swebench_harness_reports(labels, [unsupported_auxiliary])

    def test_private_label_refs_are_bound_to_dataset_revision_and_instance(self):
        _, labels, _ = prepare_swebench_codefix_records(
            [swebench_row()],
            "dataset",
            "revision",
        )
        labels[0]["instance_id"] = "example__project-999"
        with self.assertRaisesRegex(ValueError, "case_ref is not bound"):
            evaluate_swebench_codefix_results(labels, [])

    def test_cli_prepares_and_scores_without_overwriting_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            tasks = root / "tasks.jsonl"
            labels = root / "labels.jsonl"
            summary = root / "summary.json"
            results = root / "results.jsonl"
            report_json = root / "report.json"
            report_md = root / "report.md"
            source.write_text(
                json.dumps(swebench_row(), sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    0,
                    prepare_main(
                        [
                            str(source),
                            "--dataset-revision",
                            "fixture-revision",
                            "--tasks-output",
                            str(tasks),
                            "--labels-output",
                            str(labels),
                            "--summary-output",
                            str(summary),
                        ]
                    ),
                )
            label = json.loads(labels.read_text(encoding="utf-8").splitlines()[0])
            results.write_text(
                json.dumps(completed_result(label), sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    0,
                    evaluate_main(
                        [
                            str(labels),
                            str(results),
                            "--output-json",
                            str(report_json),
                            "--output-md",
                            str(report_md),
                        ]
                    ),
                )
            report = json.loads(report_json.read_text(encoding="utf-8"))
            self.assertEqual(1, report["counts"]["resolved"])
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    2,
                    prepare_main(
                        [
                            str(source),
                            "--dataset-revision",
                            "fixture-revision",
                            "--tasks-output",
                            str(tasks),
                            "--labels-output",
                            str(labels),
                            "--summary-output",
                            str(summary),
                        ]
                    ),
                )

    def test_cli_imports_nested_harness_reports(self):
        _, labels, _ = prepare_swebench_codefix_records(
            [swebench_row()],
            "dataset",
            "revision",
        )
        label = labels[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            labels_path = root / "labels.jsonl"
            reports_root = root / "logs"
            report_dir = reports_root / "run" / label["instance_id"]
            output = root / "results.jsonl"
            report_dir.mkdir(parents=True)
            labels_path.write_text(
                json.dumps(label, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            (report_dir / "report.json").write_text(
                json.dumps(
                    {
                        label["instance_id"]: {
                            "patch_successfully_applied": True,
                            "resolved": True,
                            "tests_status": {
                                "FAIL_TO_PASS": {
                                    "success": label["fail_to_pass"],
                                    "failure": [],
                                },
                                "PASS_TO_PASS": {
                                    "success": label["pass_to_pass"],
                                    "failure": [],
                                },
                            },
                        }
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                code = import_harness_main(
                    [
                        str(labels_path),
                        str(reports_root),
                        "--output",
                        str(output),
                    ]
                )
            result = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(0, code)
        self.assertEqual("completed", result["status"])
        self.assertNotIn(label["instance_id"], json.dumps(result))

    def test_cli_harness_import_rejects_empty_report_root(self):
        _, labels, _ = prepare_swebench_codefix_records(
            [swebench_row()],
            "dataset",
            "revision",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            labels_path = root / "labels.jsonl"
            reports_root = root / "logs"
            output = root / "results.jsonl"
            reports_root.mkdir()
            labels_path.write_text(
                json.dumps(labels[0], sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with contextlib.redirect_stderr(io.StringIO()):
                code = import_harness_main(
                    [
                        str(labels_path),
                        str(reports_root),
                        "--output",
                        str(output),
                    ]
                )

        self.assertEqual(2, code)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
