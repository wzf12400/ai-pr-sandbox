# Approved Issue watcher and code-dispatch preflight

## Purpose

`bin/watch-approved-issues` connects the approved-GitHub-Issue boundary to the
existing guarded code modifier. It supports one foreground poll in either
read-only or explicitly enabled execution mode:

```text
open Issue with required approval labels
  -> canonical Issue snapshot
  -> approval, fingerprint, and sensitive-data gates
  -> duplicate claim, work branch, and Draft PR checks
  -> optional atomic remote claim
  -> existing modify-approved-issue preflight or Copilot execution
  -> deterministic diff validation and policy tests
```

The command does not accept Jira records, OpenSearch events, local Issue
drafts, or model-selected repository names. It reads the tracked
`.github/issue-code-policy.json`, queries only the repository named by that
policy, and reuses the modifier's repository and Issue gates.

## Read-only command

Run it from an up-to-date, clean checkout of the policy base branch:

```bash
./bin/watch-approved-issues \
  --repo /path/to/repository \
  --once \
  --dry-run \
  --max-candidates 10 \
  --output /path/to/repository/.issue-code-output/dispatch-preflight.json
```

`--once` and one mode are mandatory. Dry-run uses the current employee's
existing `gh` login for read-only Issue and PR inspection. It does not create a
claim, invoke Copilot, or change the repository.

## Copilot execution command

After reviewing a dry-run report, explicitly enable local code modification:

```bash
./bin/watch-approved-issues \
  --repo /path/to/repository \
  --once \
  --execute \
  --max-candidates 10 \
  --output /path/to/repository/.issue-code-output/dispatch-execution.json
```

Execution uses the employee's existing `gh` and `copilot` logins. The workflow
does not ask for, copy, print, persist, or share their credentials. Before
Copilot starts, it creates a remote claim branch for the exact approved Issue
snapshot. This is an intentional GitHub write. Code changes and policy tests
remain local; this command does not commit or publish the work branch.

An explicit publication mode uses the same claim and gates, then creates a
Draft PR after tests pass:

```bash
./bin/watch-approved-issues \
  --repo /path/to/repository \
  --once \
  --publish-pr \
  --output /path/to/repository/.issue-code-output/dispatch-publication.json
```

The terminal agent targets the exact Issue approved in its preview. The general
watcher continues to select at most one eligible Issue in deterministic
Issue-number order.

## Deterministic selection and revalidation

The general watcher:

1. validates the clean local base branch, tracked policy, repository remote,
   and known `origin/<base>` commit;
2. lists at most the configured candidate limit of open Issues carrying every
   policy-required label;
3. sorts candidates by Issue number and dispatches at most one;
4. refetches the canonical Issue and reruns all modifier approval checks,
   including repository, state, labels, exactly one publication fingerprint,
   size, timestamp, and sensitive-data rules;
5. derives exact claim and work branches and checks for an existing local work
   branch, remote claim/work branch, or Draft PR;
6. in execute mode, refetches the Issue, atomically claims its snapshot, then
   refetches it again before calling Copilot;
7. invokes the existing modifier in dry-run or execute mode;
8. binds every modifier fetch to the initially approved snapshot;
9. in execute mode, validates the diff and runs only policy-listed tests.

The terminal agent already has the exact Issue URL covered by the displayed
human approval. It bypasses step 2 for that URL to avoid GitHub label-search
index lag, then performs steps 4 through 9 unchanged. A canonical URL for the
policy repository, the required labels, the exact fetched URL, and all other
approval rules remain mandatory.

An existing branch or PR is treated as a prior claim and prevents duplicate
dispatch.

## Atomic claim

The execution claim is separate from the code work branch. Each competing
employee creates a different empty commit with the pinned base as its parent,
then pushes that unique commit to the same deterministic claim branch. The
first creation wins. A second push is a non-fast-forward conflict and stops
before Copilot.

The claim branch is retained on success and failure. It is a durable
idempotency record, not an expiring lease. This version does not automatically
release claims or retry failed work. The terminal control center offers a
bounded explicit recovery path for audited pre-modifier failures and the exact
case where Copilot returned success with an empty diff, no tests, and no Draft
PR. `--resume RUN_ID` requires fresh human approval on every attempt, matches
the exact retained claim commit and Issue snapshot, revalidates repository and
branch state, and appends a new audit without overwriting earlier attempts.
At most three recovery attempts are allowed. Other failures still require
maintainer investigation.

The approval label is still a repository-governance signal rather than a
cryptographic snapshot signature. The dispatcher prevents changes between its
initial read, claim, Copilot call, and tests. Repositories must also remove and
reapply the approval label when an Issue is edited after human approval; a
future trusted approval workflow should enforce that invalidation
automatically.

## Audit result

The `issue-code-dispatch/v1` JSON report records:

- policy identifier and SHA-256;
- repository and pinned base commit;
- bounded candidate count;
- Issue URL, number, snapshot SHA-256, approval-rule outcomes, and deterministic
  work branch;
- local branch, remote branch, and existing-PR idempotency state;
- claim branch, claim commit, and conflict state when execution is requested;
- the existing modifier's safe preflight or execution report.

Pre-modifier exceptions are classified into bounded safe categories such as
localization-input safety and Copilot CLI preflight failure. Raw rejected text,
exception output, and credentials are not copied into the dispatch report.

It does not persist Issue title or body, raw upstream records, prompts,
transcripts, credentials, or unrestricted command output. Output files are
created atomically and never overwritten.

## Current boundary

This version still does not:

- add, remove, or approve GitHub labels;
- publish claim status as an Issue comment or check;
- expire, release, or retry claims automatically;
- poll continuously, schedule jobs, retry durably, merge, deploy, or perform
  production actions.

`--execute` can now call Copilot, validate its changes, and run policy tests.
`--publish-pr` additionally commits, pushes, and creates a Draft PR without
bypassing the retained claim. Label approval remains external to this CLI; the
terminal agent may apply it only as part of a displayed, exact combined human
approval.
