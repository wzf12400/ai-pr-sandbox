# SWE-bench repository-routing held-out pilot — 2026-07-24

## Outcome

The current lexical snapshot policy is useful as a conservative first-stage
router, but it is not approved for unattended company Issue publication.

| Held-out condition | Correct-route recall | Positive route precision | Wrong positive routes | Unsafe out-of-scope fallback |
|---|---:|---:|---:|---:|
| Full public Issue text | 28/45 (62.22%) | 28/28 (100.00%) | 0 | 0/45 (0.00%) |
| Project/package aliases masked | 20/42 (47.62%) | 20/22 (90.91%) | 2 | 3/42 (7.14%) |

The 95% Wilson interval for full-text auto-route precision is 87.94%–100.00%.
For the ablation run it is 60.87%–91.14%. The ablation unsafe-fallback interval
is 2.46%–19.01%.

## Data and split

- Dataset: `SWE-bench/SWE-bench_Verified`
- Dataset revision: `fd80552a1f66168960a36eb84c498a0d535eacfb`
- Parquet SHA-256:
  `43ed5a3d1d98da36472c1ade65ddd2085d7b4ff694fcaf6a023a07c5c1f32f21`
- Source dataset rows: 500
- Eligible repositories: nine repositories with at least ten Verified rows
- Held-out selection: SHA-256 stratified, seed
  `repository-routing-pilot-v1`, offset five, maximum five per repository
- Held-out source rows: 45
- Full-text evaluation: 45 positive plus 45 gold-removed `unknown` cases
- Information ablation: 42 positive plus 42 gold-removed `unknown` cases

The first five hash-ranked rows per repository were used during policy
development. This report uses the next five rows, so no Issue instance overlaps
the development fold.

## Leakage and execution boundary

The predictor received only leakage-controlled inputs. It did not load:

- gold repository labels;
- raw instance identifiers;
- Issue or pull-request URLs;
- gold patches, test patches, changed paths, or test names.

The manifest knew 12 public repositories. The held-out candidate union enabled
nine of them, so the predictor scanned 15,631 bounded text/source files
(131,672,297 bytes) from those nine exact-commit snapshots. It used only
repository/package alias metadata for known repositories outside an individual
case scope; it did not score or search their source. It did not execute
repository code and did not persist source snippets or problem statements.

The current manifest is a `current_head_proxy` captured at
`2026-07-24T02:28:34Z`. It is not a historical benchmark: current snapshots can
contain code added after the original Issue.

## Policy

The frozen policy was `repository-routing-lexical-snapshot/v1`:

- minimum resolved score: 55;
- minimum margin: 20;
- minimum independent evidence terms: two;
- ambiguous evidence threshold: 25;
- an explicit known repository/package alias outside the candidate scope forces
  `unknown` without searching excluded source.

## Interpretation

Full Issue text often names the project or its import package. That is valid
real-world evidence, but it makes routing easier. The alias-ablation result is
the more relevant stress test for company logs where a service, class, or
exception may not reveal the repository name.

The two wrong positive routes and three unsafe fallback routes in the ablation
run are release blockers for unattended Issue publication. Until they are
eliminated on another held-out fold, `ambiguous` and `unknown` must remain
non-publishing outcomes and resolved results still need the reviewed policy
gate.

## Next validation

1. Replace current-head snapshots with per-case historical cutoffs.
2. Improve traceback, Python import/module, qualified symbol, and file-path
   evidence without tuning on this held-out fold.
3. Add controlled duplicate/fork ambiguity cases.
4. Freeze the new policy and evaluate a third disjoint fold.
5. Add Java and company-shadow cases before changing automatic publication
   policy.
