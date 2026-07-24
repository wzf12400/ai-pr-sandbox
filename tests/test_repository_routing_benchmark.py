import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from src.repository_routing_benchmark import (
    BLOCKED_STATEMENT,
    PREDICTION_SCHEMA_VERSION,
    evaluate_main,
    evaluate_routing_predictions,
    load_ablation_aliases,
    prepare_main,
    prepare_swebench_records,
    render_evaluation_markdown,
    select_stratified_rows,
)


REPOSITORIES = ("example-org/alpha", "example-org/beta")


def swebench_row(
    instance_id,
    repository,
    problem_statement,
    base_commit="a" * 40,
):
    return {
        "instance_id": instance_id,
        "repo": repository,
        "problem_statement": problem_statement,
        "base_commit": base_commit,
        "created_at": "2024-01-02T03:04:05Z",
        "patch": "must never enter predictor input",
        "test_patch": "must never enter predictor input",
        "issue_url": f"https://github.com/{repository}/issues/1",
        "pr_url": f"https://github.com/{repository}/pull/2",
    }


def label(case_ref, status, repository=None, source_repository=None):
    return {
        "schema_version": "repository-routing-benchmark-label/v1",
        "case_ref": case_ref,
        "expected_status": status,
        "expected_repository": repository,
        "source_repository": source_repository or repository or REPOSITORIES[0],
        "source_ref": "source_ref:" + case_ref.rsplit(":", 1)[-1],
        "dataset_revision": "unit-test-revision",
        "gold_base_commit": "a" * 40,
        "created_at": "2024-01-02T03:04:05Z",
        "variant": "original",
    }


def prediction(case_ref, status, repository=None):
    return {
        "schema_version": PREDICTION_SCHEMA_VERSION,
        "case_ref": case_ref,
        "status": status,
        "selected_repository": repository,
        "policy_version": "repository-resolution-policy/v1",
        "top_score": 75 if status == "resolved" else 0,
        "runner_up_score": 0,
        "margin": 75 if status == "resolved" else 0,
    }


def case_ref(character):
    return f"swebench_ref:{character * 32}"


class RepositoryRoutingBenchmarkTest(unittest.TestCase):
    def test_stratified_selection_is_deterministic_and_bounded(self):
        rows = [
            swebench_row(
                f"example-org__alpha-{index}",
                REPOSITORIES[0],
                f"AlphaController.route_{index} fails.",
            )
            for index in range(6)
        ] + [
            swebench_row(
                f"example-org__beta-{index}",
                REPOSITORIES[1],
                f"BetaController.route_{index} fails.",
            )
            for index in range(3)
        ]

        first = select_stratified_rows(rows, 2, "unit-test-seed")
        second = select_stratified_rows(list(reversed(rows)), 2, "unit-test-seed")
        held_out = select_stratified_rows(rows, 2, "unit-test-seed", 2)

        self.assertEqual(
            [row["instance_id"] for row in first],
            [row["instance_id"] for row in second],
        )
        self.assertEqual(
            {REPOSITORIES[0]: 2, REPOSITORIES[1]: 2},
            {
                repository: sum(row["repo"] == repository for row in first)
                for repository in REPOSITORIES
            },
        )
        self.assertTrue(
            {row["instance_id"] for row in first}.isdisjoint(
                {row["instance_id"] for row in held_out}
            )
        )

    def test_information_ablation_masks_project_aliases_and_keeps_labels_private(self):
        rows = [
            swebench_row(
                "example-org__alpha-20",
                REPOSITORIES[0],
                "Alpha alpha_client.retry_delivery() raises DeliveryRetryError.",
            ),
            swebench_row(
                "example-org__beta-21",
                REPOSITORIES[1],
                "Beta BetaScheduler.run fails.",
            ),
        ]

        inputs, labels, summary = prepare_swebench_records(
            rows,
            "fixture-revision",
            derive_out_of_scope=True,
            derive_information_ablation=True,
            repository_aliases={
                REPOSITORIES[0]: ("Alpha", "alpha_client"),
                REPOSITORIES[1]: ("Beta",),
            },
        )

        self.assertEqual(2, summary["information_ablation_rows"])
        self.assertEqual(2, summary["gold_removed_ablation_rows"])
        ablated_inputs = [
            item
            for item in inputs
            if item["derived_from"] is not None
            and "[REDACTED_PROJECT_ALIAS]" in item["problem_statement"]
        ]
        self.assertEqual(4, len(ablated_inputs))
        self.assertNotIn("alpha_client", json.dumps(ablated_inputs).casefold())
        ablated_labels = [
            item
            for item in labels
            if item["variant"] in {
                "information_ablation",
                "gold_removed_ablation",
            }
        ]
        self.assertEqual(4, len(ablated_labels))

    def test_loads_strict_ablation_alias_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "aliases.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": "repository-routing-ablation-aliases/v1",
                        "repositories": {
                            REPOSITORIES[0]: ["alpha", "alpha_client"],
                            REPOSITORIES[1]: ["beta"],
                        },
                    }
                ),
                encoding="utf-8",
            )

            aliases = load_ablation_aliases(path)

        self.assertEqual(("alpha", "alpha_client"), aliases[REPOSITORIES[0]])

    def test_prepare_masks_answer_fields_and_derives_unknown_case(self):
        rows = [
            swebench_row(
                "example-org__alpha-1",
                REPOSITORIES[0],
                (
                    "Failure in example-org/alpha. See "
                    "https://github.com/example-org/alpha/issues/1 for details."
                ),
            ),
            swebench_row(
                "example-org__beta-2",
                REPOSITORIES[1],
                "Contact maintainer@example.com before reproducing.",
            ),
        ]

        inputs, labels, summary = prepare_swebench_records(
            rows,
            "fixture-revision",
            derive_out_of_scope=True,
        )

        self.assertEqual(3, len(inputs))
        self.assertEqual(3, len(labels))
        self.assertEqual(1, summary["derived_out_of_scope_rows"])
        self.assertEqual(1, summary["blocked_sensitive_rows"])
        original = next(item for item in inputs if item["derived_from"] is None)
        self.assertNotIn(REPOSITORIES[0], original["problem_statement"])
        self.assertNotIn("github.com", original["problem_statement"])
        self.assertNotIn("example-org__alpha-1", json.dumps(original))
        self.assertFalse(original["answer_fields_present"])
        negative = next(item for item in inputs if item["derived_from"] is not None)
        self.assertNotIn(REPOSITORIES[0], negative["candidate_repositories"])
        negative_label = next(
            item for item in labels if item["case_ref"] == negative["case_ref"]
        )
        self.assertEqual("unknown", negative_label["expected_status"])
        self.assertIsNone(negative_label["expected_repository"])
        blocked = next(
            item for item in inputs if item["preflight"]["status"] == "blocked"
        )
        self.assertEqual(BLOCKED_STATEMENT, blocked["problem_statement"])
        self.assertNotIn("maintainer@example.com", json.dumps(inputs))
        self.assertNotIn("must never enter predictor input", json.dumps(inputs))

    def test_evaluate_reports_false_routes_abstentions_and_macro_metrics(self):
        labels = [
            label(case_ref("1"), "resolved", REPOSITORIES[0]),
            label(case_ref("2"), "resolved", REPOSITORIES[1]),
            label(case_ref("3"), "unknown", source_repository=REPOSITORIES[0]),
            label(case_ref("4"), "ambiguous", source_repository=REPOSITORIES[0]),
            label(case_ref("5"), "blocked", source_repository=REPOSITORIES[1]),
        ]
        predictions = [
            prediction(case_ref("1"), "resolved", REPOSITORIES[0]),
            prediction(case_ref("2"), "resolved", REPOSITORIES[0]),
            prediction(case_ref("3"), "unknown"),
            prediction(case_ref("4"), "resolved", REPOSITORIES[1]),
        ]

        report = evaluate_routing_predictions(labels, predictions)

        self.assertEqual(1, report["counts"]["correct_resolved"])
        self.assertEqual(2, report["counts"]["wrong_resolved"])
        self.assertEqual(1, report["counts"]["missing_predictions"])
        self.assertAlmostEqual(1 / 3, report["metrics"]["auto_route_precision"])
        self.assertAlmostEqual(2 / 3, report["metrics"]["false_route_rate"])
        self.assertEqual(0.5, report["metrics"]["positive_auto_route_precision"])
        self.assertEqual(0.5, report["metrics"]["positive_wrong_route_rate"])
        self.assertEqual(1.0, report["metrics"]["resolved_coverage"])
        self.assertEqual(0.5, report["metrics"]["correct_route_recall"])
        self.assertAlmostEqual(1 / 3, report["metrics"]["unsafe_fallback_rate"])
        self.assertAlmostEqual(1 / 3, report["metrics"]["safe_nonresolution_rate"])
        self.assertEqual(0.4, report["metrics"]["exact_outcome_accuracy"])
        self.assertAlmostEqual(1 / 3, report["metrics"]["safe_abstention_accuracy"])
        self.assertEqual(0.5, report["metrics"]["macro_repository_recall"])
        self.assertIsNotNone(
            report["confidence_intervals_95"]["auto_route_precision"]
        )
        self.assertFalse(
            report["audit"]["private_labels_provided_to_predictor"]
        )
        self.assertEqual(
            ["original"],
            report["audit"]["evaluation_variants"],
        )
        markdown = render_evaluation_markdown(report)
        self.assertIn("33.33%", markdown)
        self.assertNotIn("AlphaController.route fails.", json.dumps(report))

    def test_evaluate_uses_null_rates_when_no_issue_is_auto_routed(self):
        labels = [label(case_ref("6"), "resolved", REPOSITORIES[0])]
        predictions = [prediction(case_ref("6"), "unknown")]

        report = evaluate_routing_predictions(labels, predictions)

        self.assertIsNone(report["metrics"]["auto_route_precision"])
        self.assertIsNone(report["metrics"]["false_route_rate"])
        self.assertEqual(0.0, report["metrics"]["resolved_coverage"])
        self.assertIsNone(
            report["confidence_intervals_95"]["auto_route_precision"]
        )

    def test_evaluate_rejects_prediction_for_unknown_case(self):
        with self.assertRaisesRegex(ValueError, "unknown case_ref"):
            evaluate_routing_predictions(
                [label(case_ref("7"), "resolved", REPOSITORIES[0])],
                [prediction(case_ref("8"), "unknown")],
            )

    def test_cli_prepares_and_evaluates_jsonl_without_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "swebench.jsonl"
            rows = [
                swebench_row(
                    "example-org__alpha-10",
                    REPOSITORIES[0],
                    "AlphaController.route fails.",
                ),
                swebench_row(
                    "example-org__beta-11",
                    REPOSITORIES[1],
                    "BetaController.route fails.",
                    base_commit="b" * 40,
                ),
            ]
            source.write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )
            inputs_path = root / "inputs.jsonl"
            labels_path = root / "labels.jsonl"
            summary_path = root / "summary.json"
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = prepare_main(
                    [
                        str(source),
                        "--dataset-revision",
                        "fixture-revision",
                        "--inputs-output",
                        str(inputs_path),
                        "--labels-output",
                        str(labels_path),
                        "--summary-output",
                        str(summary_path),
                    ]
                )
            self.assertEqual(0, exit_code)
            prepared_labels = [
                json.loads(line) for line in labels_path.read_text().splitlines()
            ]
            predictions_path = root / "predictions.jsonl"
            predictions_path.write_text(
                "".join(
                    json.dumps(
                        prediction(
                            item["case_ref"],
                            "resolved",
                            item["expected_repository"],
                        )
                    )
                    + "\n"
                    for item in prepared_labels
                ),
                encoding="utf-8",
            )
            report_path = root / "report.json"
            markdown_path = root / "report.md"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    0,
                    evaluate_main(
                        [
                            str(labels_path),
                            str(predictions_path),
                            "--output-json",
                            str(report_path),
                            "--output-md",
                            str(markdown_path),
                        ]
                    ),
                )
            report = json.loads(report_path.read_text())
            self.assertEqual(1.0, report["metrics"]["auto_route_precision"])
            self.assertEqual(0.0, report["metrics"]["false_route_rate"])
            self.assertEqual(
                2,
                json.loads(summary_path.read_text())["source_rows"],
            )
            with contextlib.redirect_stderr(
                io.StringIO()
            ), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    2,
                    prepare_main(
                        [
                            str(source),
                            "--dataset-revision",
                            "fixture-revision",
                            "--inputs-output",
                            str(inputs_path),
                            "--labels-output",
                            str(labels_path),
                            "--summary-output",
                            str(summary_path),
                        ]
                    ),
                )
            shared_output = root / "shared-output"
            with contextlib.redirect_stderr(
                io.StringIO()
            ), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    2,
                    evaluate_main(
                        [
                            str(labels_path),
                            str(predictions_path),
                            "--output-json",
                            str(shared_output),
                            "--output-md",
                            str(shared_output),
                        ]
                    ),
                )
            self.assertFalse(shared_output.exists())


if __name__ == "__main__":
    unittest.main()
