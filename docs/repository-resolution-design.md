# Repository resolution before GitHub Issue publication

## Purpose

Natural-language reports and sanitized OpenSearch incidents first become a
repository-independent `IssueDraft`. The repository resolver then searches an
explicitly authorized GitHub repository scope, decides whether one repository
is a sufficiently strong match, and only then hands the draft to the existing
GitHub publication boundary.

The scope configuration is not a service-to-repository registry. It only says
which repositories may be searched. Repository ownership is inferred from
current repository evidence, so adding a repository requires one small scope
entry and moving code does not require updating a parallel routing map.

## Safety boundary

- Inputs must be locally validated AI Issue results derived from sanitized
  evidence. Raw logs, Jira exports, screenshots, and model-only text cannot be
  searched directly.
- Search terms are limited to sanitized software identifiers already present
  in the IssueDraft evidence: qualified classes, classes and methods, filenames,
  package prefixes, module artifacts, interface paths, and service identifiers.
- Generic exception names and natural-language similarity cannot independently
  select a repository.
- Search is read-only and limited to repositories in the reviewed scope file.
- The pre-publication resolver returns repository-level evidence aggregates. It
  does not return editable source snippets, implementation instructions, or
  authoritative file/line claims.
- A repository result cannot authorize code modification. Detailed code
  localization still starts only from an approved, published GitHub Issue.
- `ambiguous`, `unknown`, and `blocked` are normal fail-closed outcomes.
- AI may extract candidate search terms but cannot choose the repository,
  change the search scope, change thresholds, or authorize publication.

## End-to-end flow

```text
natural language ─┐
                  ├─> sanitized evidence -> reviewed IssueDraft
OpenSearch ERROR ─┘
                                   |
                                   v
                  repository-search-scope/v1
                                   |
                                   v
          native repository search + repository verification
                                   |
                                   v
         repository-resolution/v1: resolved / ambiguous / unknown
                                   |
                          resolved only
                                   v
                 target repository Issue duplicate search
                                   |
                 existing match or create new GitHub Issue
                                   |
                          needs-discussion
                                   |
                         human approval
                                   v
                 repo_locator inside the approved repository
```

GitHub Issues cannot exist without a repository. Before resolution, "Issue"
means the local structured IssueDraft and its audit record. After resolution
and publication, it means the canonical GitHub Issue.

## Components

### 1. Search scope loader

`repository-search-scope/v1` contains only:

- a stable scope identifier;
- GitHub repositories that may be searched;
- enabled/disabled state;
- optional default branches and operator labels;
- bounded query and candidate limits.

The file contains no credentials, company URLs, service mappings, class
mappings, or publication authorization. Duplicate repository entries, invalid
`owner/name` values, an empty enabled scope, and out-of-range limits are local
validation errors.

### 2. Query planner

The planner extracts a bounded set of terms from known IssueDraft fields and
their evidence paths. It searches the strongest terms first:

1. fully qualified class or unique filename;
2. class and method pair;
3. interface path plus a business object;
4. module artifact or package prefix;
5. service/deployment identifier;
6. repository metadata tokens.

Terms removed by sanitization, values marked `unknown`, credentials, customer
identifiers, unrestricted stack traces, and generic prose are excluded.

### 3. Native search adapter

The first implementation may use GitHub code search across each enabled
repository. A later local-index adapter can implement the same interface.

```text
search(scope, planned_terms) -> bounded repository hit aggregates
```

The adapter returns repository, branch/ref, evidence family, matched sanitized
term, and bounded hit count. Source snippets are not part of the repository
resolution contract.

### 4. Repository verifier

The verifier checks whether independent evidence families co-occur in a
candidate repository. It can confirm, for example, that a qualified class and
method or an interface path and business class both exist. It records only a
repository-level aggregate and a stable evidence reference.

The existing `repo_locator.py` is not this verifier. `repo_locator.py` requires
one already selected repository and produces file/symbol candidates after a
GitHub Issue has been approved.

### 5. Deterministic scorer

Scores are bounded to 100 and capped per evidence family so repeated hits do
not overwhelm independent evidence. Initial design weights are:

| Evidence family | Maximum contribution |
| --- | ---: |
| Fully qualified class or exact unique filename | 40 |
| Class and method co-occurrence | 35 |
| Interface path plus business object | 30 |
| Module artifact or package prefix | 25 |
| Service/deployment identifier | 15 |
| Repository metadata token | 5 |
| Generic exception or prose similarity | 0 |

One observation may satisfy multiple families only when each family has its
own source evidence path. Contradictory exact identifiers are recorded as a
conflict rather than averaged away.

Initial decision thresholds:

- `resolved`: top score at least 70, at least two independent strong evidence
  families, margin over second place at least 20, and no exact conflict;
- `ambiguous`: at least one candidate score of 40, but the resolved conditions
  are not met;
- `unknown`: no candidate reaches 40;
- `blocked`: input safety, scope validation, authorization, or query-budget
  validation failed.

These values are configuration-independent versioned policy constants. Any
future tuning requires synthetic fixtures and real approved benchmark cases.

### 6. Existing Issue matcher

After one repository is resolved, search that repository's Issues using the
deterministic Issue fingerprint and a bounded set of structured fields. The
outcome is one of:

- `existing_issue_candidate`: return the Issue URL and evidence; do not append
  automatically in the first implementation;
- `new_issue`: no sufficiently strong existing match;
- `ambiguous_existing_issues`: several Issues are similarly close.

Updating an existing Issue is a separate write action and must not be inferred
from repository resolution alone.

## Maintenance model

Adding a searchable repository is one configuration change:

```json
{
  "repository": "example-org/new-service",
  "enabled": true,
  "labels": ["backend"]
}
```

Disabling a repository sets `enabled` to `false`, preserving review history.
The loader should provide a dry run that verifies GitHub access and reports
added, removed, disabled, inaccessible, archived, or duplicate entries.

Future organization discovery can produce the same normalized scope from a
GitHub App installation or an approved repository topic. Explicit entries and
discovered entries must pass the same schema and authorization checks.

## Versioned interfaces

- Search scope: `schemas/repository-search-scope-v1.schema.json`
- Resolution result: `schemas/repository-resolution-v1.schema.json`
- Company-neutral examples:
  - `examples/repository-search-scope.example.json`
  - `examples/repository-resolution-result.example.json`

## Implementation sequence

1. Implement strict scope/result dataclasses and local schema-equivalent
   validation with no network access.
2. Implement IssueDraft term extraction and query budgets using synthetic
   fixtures.
3. Define a search adapter protocol and a fake adapter for deterministic tests.
4. Implement scoring, conflicts, thresholds, and auditable results.
5. Add an authorized GitHub native-search adapter.
6. Add target-repository existing-Issue matching.
7. Connect only `resolved` output to the separately authorized publication
   operation.

No scheduler, watcher, or automatic code modification is part of this design
milestone.

## Current implementation

`bin/resolve-issue-repository` now implements the first read-only slice:

- strict loading of `repository-search-scope/v1` and eligible, locally
  validated `ai-issue-generation/v1` results;
- grounded extraction of fully qualified classes and class/method pairs;
- bounded GitHub CLI code search within enabled repositories only;
- same-file in-memory verification for class/method pairs;
- deterministic scoring and fail-closed result states;
- repository-level evidence references without persisted source paths or
  snippets.

The default adapter is `github-code-search`. A separate
`github-tree-probe` adapter exists only for newly created synthetic repositories
that have not entered GitHub's asynchronous code-search index. It fails closed
for repositories above 1 MB, trees above 500 entries, truncated trees, archived
repositories, or matching Java files above 256 KB. It is not a production
large-repository search strategy.

Module/package, interface/business-object, service, and repository-metadata
families are not yet implemented. Target-repository existing-Issue matching is
also not implemented, and resolver output is not connected to Issue
publication.
