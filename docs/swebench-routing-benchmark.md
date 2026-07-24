# SWE-bench repository-routing benchmark

## Purpose

SWE-bench normally gives a system the target repository and asks it to produce
a patch. This adaptation evaluates an earlier decision: whether a system can
select the correct repository from an authorized candidate scope without
seeing answer-bearing SWE-bench fields.

The current implementation prepares the data, predicts routes from bounded
local source snapshots, and calculates metrics. It does not download datasets
or repositories itself, call an AI provider, publish Issues, apply patches, or
run SWE-bench Docker tests.

## Leakage boundary

`bin/prepare-swebench-routing` writes two physically separate JSONL files:

- predictor inputs contain an opaque case reference, minimized problem
  statement, preflight status, and candidate repositories;
- private labels contain the expected status/repository, source repository,
  pinned gold base commit, dataset revision, and opaque source reference.

Predictor inputs never contain:

- `repo` as a gold label;
- raw `instance_id`;
- Issue or pull-request URLs;
- gold patch or test patch;
- changed file paths;
- fail-to-pass or pass-to-pass test names.

The preparer masks explicit `owner/repo` strings and GitHub URLs in the problem
statement. If the existing local sensitive-data detector finds a credential,
email, private key, phone number, or regulated identifier, the raw statement is
not persisted: the case becomes an expected `blocked` outcome.

Never pass `labels.jsonl` to an AI model, repository search adapter, or routing
program. It is evaluation-only data.

## Export and pin the dataset

Pin the exact Hugging Face dataset revision before exporting. Dataset variants
and row counts can change over time.

One possible export is:

```python
from datasets import load_dataset

revision = "<exact-hugging-face-commit>"
dataset = load_dataset(
    "SWE-bench/SWE-bench_Verified",
    split="test",
    revision=revision,
)
dataset.to_json("swebench-verified.jsonl")
```

The project intentionally does not add `datasets`, Docker, or network downloads
as runtime dependencies. Keep downloaded public benchmark artifacts outside
Git tracking.

## Prepare routing inputs and labels

```bash
./bin/prepare-swebench-routing swebench-verified.jsonl \
  --dataset-revision '<exact-hugging-face-commit>' \
  --inputs-output .benchmark-output/inputs.jsonl \
  --labels-output .benchmark-output/labels.jsonl \
  --summary-output .benchmark-output/preparation.json \
  --derive-out-of-scope
```

`--derive-out-of-scope` creates one additional `unknown` case for every
eligible positive case when at least two repositories are present. The derived
case keeps the same minimized problem statement but removes the gold
repository from its candidate scope. It measures whether the resolver guesses
an incorrect fallback repository.

For a reproducible balanced pilot, preparation also supports:

- `--max-per-repository` and `--sample-offset-per-repository` for deterministic
  SHA-256 stratified development/held-out folds;
- `--minimum-repository-rows` to exclude repositories that cannot fill both
  folds;
- `--derive-information-ablation --repository-aliases FILE` to mask project
  and package aliases before a second routing run.

The alias file is used only by the private preparation step. It is not a
repository answer passed to the predictor. See
`examples/swebench-repository-aliases.example.json`.

Ambiguous duplicate/fork cases cannot be created safely from metadata alone.
They require a controlled candidate snapshot where the same grounded symbols
exist in more than one repository.

## Prediction contract

The routing system writes one JSON object per line:

```json
{
  "schema_version": "repository-routing-benchmark-prediction/v1",
  "case_ref": "swebench_ref:0123456789abcdef0123456789abcdef",
  "status": "resolved",
  "selected_repository": "example-org/example-repo",
  "policy_version": "repository-resolution-policy/v1",
  "top_score": 75,
  "runner_up_score": 0,
  "margin": 75
}
```

`selected_repository` must be `null` for `ambiguous`, `unknown`, and
`blocked`. A missing prediction is retained in the report and counts as
incorrect rather than silently reducing the denominator.

## Predict from local snapshots

Prepare a manifest with one local, exact-commit source snapshot per known
repository:

```json
{
  "schema_version": "repository-routing-snapshot-manifest/v1",
  "snapshot_kind": "current_head_proxy",
  "captured_at": "2026-07-24T02:28:34Z",
  "repositories": [
    {
      "repository": "example-org/example-repo",
      "path": "/private/tmp/example-repo",
      "commit": "0123456789abcdef0123456789abcdef01234567"
    }
  ]
}
```

Then run:

```bash
./bin/predict-swebench-routing \
  .benchmark-output/inputs.jsonl \
  --snapshots .benchmark-output/snapshots.json \
  --output .benchmark-output/predictions.jsonl \
  --audit-output .benchmark-output/prediction-audit.json
```

The predictor:

- never accepts the private labels file;
- scans bounded text/source files only for the union of candidate repositories,
  without executing repository code;
- indexes only identifiers present in the current input batch;
- ignores symlinks and common generated/dependency directories;
- requires at least two terms, a score of 55, and a 20-point margin;
- returns `unknown` when a known repository/package alias points outside the
  current candidate scope, without searching that excluded source;
- persists hashes and counts, not source snippets or problem statements.

`current_head_proxy` is useful for an engineering pilot, but it can contain
code added after the historical Issue. A publication-grade SWE-bench result
must use `historical_cutoff` snapshots selected independently for every
repository at the case timestamp.

## Evaluate predictions

```bash
./bin/evaluate-repository-routing \
  .benchmark-output/labels.jsonl \
  .benchmark-output/predictions.jsonl \
  --output-json .benchmark-output/evaluation.json \
  --output-md .benchmark-output/evaluation.md
```

Use repeated `--variant` options to compare full text and project-name
ablation without mixing their denominators.

The report includes:

- auto-route precision: correct resolved predictions divided by all resolved
  predictions;
- false-route rate: wrong resolved predictions divided by all resolved
  predictions;
- positive auto-route precision: correct routes divided by route attempts on
  resolvable cases;
- positive wrong-route rate: incorrect repositories among route attempts on
  resolvable cases;
- resolved coverage: route attempts on uniquely resolvable cases;
- correct-route recall: correct resolved predictions divided by gold resolved
  cases;
- unsafe fallback rate: `resolved` predictions when the correct repository is
  outside the candidate scope;
- safe non-resolution rate: explicit `unknown`, `ambiguous`, or `blocked`
  predictions for non-resolved gold cases;
- exact outcome accuracy across resolved/ambiguous/unknown/blocked;
- exact non-resolved status accuracy, which is stricter than safe
  non-resolution;
- macro recall with every repository weighted equally;
- per-repository results;
- 95% Wilson intervals for auto-route precision and false-route rate.

Auto-route precision and false-route rate are `null`, not zero, if a system
never predicts `resolved`.

## Required benchmark layers

Report two separate experiments:

1. Resolver-only: freeze evidence-grounded structured Issue results and measure
   the deterministic repository resolver.
2. End-to-end: start from the minimized problem statement and include AI
   extraction/review before repository resolution.

The first isolates search/scoring quality. The second measures extraction
coverage and model variability as part of the real workflow.

## 2026-07-24 held-out pilot

The checked-in report at `reports/swebench-routing-heldout-20260724.md` uses:

- official SWE-bench Verified data commit
  `fd80552a1f66168960a36eb84c498a0d535eacfb`;
- a disjoint held-out fold of five issues from each of nine repositories with
  at least ten Verified rows;
- nine candidate repositories per case;
- fixed current-head source snapshots captured on 2026-07-24;
- 45 full-text positives plus 45 gold-removed negatives;
- 42 project-alias-ablated positives plus 42 corresponding negatives.

Full text produced 62.22% correct-route recall and 100% auto-route precision
(28 correct routes, no wrong routes). With project/package aliases masked,
correct-route recall fell to 47.62%; positive auto-route precision was 90.91%,
and unsafe fallback was 7.14%.

This is a held-out engineering pilot, not a historical SWE-bench leaderboard
result. The ablation failures mean the policy is not approved for unattended
company Issue publication.

## Remaining work

- select every candidate repository's latest commit before each case timestamp,
  not today's default branch;
- improve Python module, class/function, import, and traceback evidence
  families without tuning on the held-out fold;
- create controlled ambiguous duplicate/fork fixtures;
- use gold patch paths for file Recall@1/5/10 after repository routing;
- use fail-to-pass and pass-to-pass tests only in the later code-fix evaluation;
- supplement Python-heavy SWE-bench with Java and company shadow cases.

Until these steps are complete, report the result as a current-head routing
pilot, not a historical or production repository-routing rate.
