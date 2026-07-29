# SWE-bench code-fix benchmark

## Purpose

The existing SWE-bench adaptation measures repository routing. This second
layer prepares code-fix tasks for the guarded Issue-to-code workflow and scores
execution results with SWE-bench fail-to-pass (F2P) and pass-to-pass (P2P)
tests.

It is deliberately offline. It does not download a dataset, clone a target
repository, run Docker, call Copilot, create an Issue, or publish a pull
request.

## Leakage boundary

`bin/prepare-swebench-codefix` creates two physically separate JSONL files.

Agent-visible tasks contain only:

- an opaque case reference;
- the authorized target repository;
- the exact pre-fix `base_commit`;
- the public problem statement;
- bounded version and environment metadata;
- local sensitive-data preflight state.

Private labels contain:

- the original `instance_id`;
- the pinned dataset name and revision;
- F2P and P2P test names;
- SHA-256 digests of the gold solution and test patches.

The task file never contains gold or test patch content, test names, raw
instance IDs, or Issue/PR URLs. Never pass the private label file to a code
agent.

## Prepare a small deterministic pilot

Export and pin the official dataset as described in
`docs/swebench-routing-benchmark.md`, then run:

```bash
./bin/prepare-swebench-codefix swebench-verified.jsonl \
  --dataset-name SWE-bench/SWE-bench_Verified \
  --dataset-revision '<exact-hugging-face-commit>' \
  --max-instances 5 \
  --sample-seed first-codefix-pilot \
  --tasks-output .benchmark-output/codefix-tasks.jsonl \
  --labels-output .benchmark-output/codefix-labels.jsonl \
  --summary-output .benchmark-output/codefix-preparation.json
```

`--max-instances` uses deterministic SHA-256 sampling. The preparer accepts
F2P/P2P fields encoded either as JSON arrays or as JSON strings containing
arrays. It rejects empty F2P sets, duplicate tests, overlapping F2P/P2P
partitions, duplicate instances, invalid repository names, and invalid commit
identifiers.

Use a complete dataset export. The Hugging Face preview `first-rows` response
truncates long cell values and is not valid benchmark input; use the dataset
rows API or the pinned Parquet file instead.

If the local sensitive-data policy blocks a public problem statement, the task
stores only the fixed blocked marker. That row must not be executed.

## Result contract

An offline runner writes one result per attempted case:

```json
{
  "schema_version": "swebench-codefix-result/v1",
  "case_ref": "swebench_codefix_ref:0123456789abcdef0123456789abcdef",
  "status": "completed",
  "fail_to_pass_passed": ["tests/test_widget.py::test_empty"],
  "fail_to_pass_failed": [],
  "pass_to_pass_passed": ["tests/test_widget.py::test_value"],
  "pass_to_pass_failed": []
}
```

A completed result must report every private F2P and P2P test exactly once.
Unknown, duplicated, or missing test outcomes fail closed. `error` and
`skipped` results must not claim test outcomes.

## Import official harness reports

The official SWE-bench harness writes one `report.json` below each evaluated
instance directory. Convert a complete harness run into the result contract
with:

```bash
./bin/import-swebench-harness-results \
  .benchmark-output/codefix-labels.jsonl \
  logs/run_evaluation/<run-id>/<model-name> \
  --output .benchmark-output/codefix-results.jsonl
```

The importer recursively reads only files named `report.json`. It privately
maps the official `instance_id` to the opaque `case_ref`; instance IDs are not
written to the result file. A successfully applied patch must report every
private F2P and P2P test exactly once, and the harness `resolved` value must
agree with those outcomes. Unknown or duplicate instances, incomplete test
coverage, conflicting outcomes, symbolic-link reports, empty report roots, and
oversized reports fail closed. A patch-application failure becomes an `error`
result and remains in the evaluation denominator.

## Evaluate

```bash
./bin/evaluate-swebench-codefix \
  .benchmark-output/codefix-labels.jsonl \
  .benchmark-output/codefix-results.jsonl \
  --output-json .benchmark-output/codefix-evaluation.json \
  --output-md .benchmark-output/codefix-evaluation.md
```

A case is fully resolved only when all F2P tests pass and all P2P tests remain
passing. The report keeps execution coverage, resolved rate, aggregate F2P/P2P
rates, missing or incomplete cases, and attempts to execute policy-blocked
cases separate.

## Execution boundary

The remaining execution stage is a deliberately small Docker-backed runner
that:

1. checks out only the pinned `base_commit`;
2. gives the code agent only the task JSON and repository snapshot;
3. captures the generated patch without publishing it;
4. invokes the official SWE-bench harness;
5. imports harness outcomes with the command above.

That runner should start with one or two public Verified instances. Docker,
repository downloads, model calls, and real code execution require a separate
explicit approval because they are outside this preparer's offline boundary.
