# SWE-bench repository-routing benchmark

## Purpose

SWE-bench normally gives a system the target repository and asks it to produce
a patch. This adaptation evaluates an earlier decision: whether a system can
select the correct repository from an authorized candidate scope without
seeing answer-bearing SWE-bench fields.

This milestone implements dataset preparation and metric calculation only. It
does not download SWE-bench, clone repositories, search historical snapshots,
call an AI provider, publish Issues, apply patches, or run SWE-bench Docker
tests.

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

The next implementation slice will bridge these inputs to a local pinned
multi-repository search adapter. GitHub's current default-branch code-search
index is not a faithful evaluator for historical SWE-bench tasks.

## Evaluate predictions

```bash
./bin/evaluate-repository-routing \
  .benchmark-output/labels.jsonl \
  .benchmark-output/predictions.jsonl \
  --output-json .benchmark-output/evaluation.json \
  --output-md .benchmark-output/evaluation.md
```

The report includes:

- auto-route precision: correct resolved predictions divided by all resolved
  predictions;
- false-route rate: wrong resolved predictions divided by all resolved
  predictions;
- resolved coverage: route attempts on uniquely resolvable cases;
- correct-route recall: correct resolved predictions divided by gold resolved
  cases;
- exact outcome accuracy across resolved/ambiguous/unknown/blocked;
- safe-abstention accuracy for non-resolved gold cases;
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

## Remaining work

- implement a read-only local search adapter over pinned repository snapshots;
- select every candidate repository's latest commit before each case timestamp,
  not today's default branch;
- add Python module, class/function, import, and traceback evidence families;
- create controlled ambiguous duplicate/fork fixtures;
- add information-ablation cases;
- use gold patch paths for file Recall@1/5/10 after repository routing;
- use fail-to-pass and pass-to-pass tests only in the later code-fix evaluation;
- supplement Python-heavy SWE-bench with Java and company shadow cases.

Until these steps are complete, this harness measures prepared predictions but
does not claim a real SWE-bench repository-routing rate.
