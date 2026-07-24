# Approved Issue to Copilot CLI code modification

## Purpose

`bin/modify-approved-issue` is the first guarded downstream implementation
slice after Issue creation and repository routing. It fetches one published
GitHub Issue, validates a repository-owned policy, runs deterministic code
localization, and can ask the current operating-system user's GitHub Copilot
CLI to make bounded changes. It then validates the diff, runs only
repository-approved tests, and can create a Draft PR.

The command never accepts a raw Jira item, OpenSearch event, log, local
IssueDraft, or model-generated repository choice. The published GitHub Issue
is the only task input.

## One employee, one Copilot identity

The executable and repository policy are shared. Credentials are not.

Each employee installs the official GitHub Copilot CLI and authenticates it
with their own company-authorized GitHub account:

```bash
brew install --cask copilot-cli
copilot login
```

The OAuth credential is managed by the employee's local credential store.
The workflow does not read, copy, print, persist, or redistribute it. It also
removes `COPILOT_ALLOW_ALL`, `COPILOT_MODEL`, and unrelated secret-bearing
environment variables from the Copilot subprocess. A future GitHub Actions
provider can implement the same `CodeModifier` interface without changing the
Issue or audit contracts.

## Repository policy

Every repository that permits automated code changes owns a tracked,
reviewed `.github/issue-code-policy.json` using
`issue-code-policy/v1`. The policy defines:

- repository and base branch;
- required approval labels;
- allowed and default Copilot models;
- allowed and blocked write globs;
- exact test commands expressed as argument arrays, never shell strings;
- file, line, and timeout budgets;
- Draft-PR-only and no-auto-merge invariants.

The command rejects a symbolic-link, untracked, modified, oversized, unknown,
or schema-invalid policy. It also requires the clean local base branch and its
known `origin/<base>` commit to match.

## Approval contract

Before Copilot is called, all of these gates must pass:

- the URL resolves to one open GitHub Issue in the policy repository;
- the canonical Issue contains exactly one deterministic publication
  fingerprint;
- every policy-required label is present;
- title and minimized body stay within size limits;
- the Issue contains no credential, personal-data, or known-secret pattern;
- repository, base branch, tracked policy, and worktree are valid and clean;
- the selected model is explicitly allowed by repository policy.

The required label is an authorization signal owned by a trusted repository
workflow or human maintainer. AI output cannot add it or change the policy.

## Copilot sandbox

The program creates an Issue-bound branch, then sends the canonical Issue
snapshot and deterministic localization candidates to Copilot through stdin.
The Issue text does not appear in process arguments.

Copilot receives only:

- local repository view, search, and edit tools;
- the pinned model;
- no interactive questions.

The invocation disables built-in GitHub MCP access, remote session export,
memory, URL access, all shell commands, custom instructions, and experimental
tools. It uses a fresh temporary `COPILOT_HOME` so saved personal tool or path
approvals do not enter the task. It never uses `--allow-all`,
`--allow-all-tools`, or `--yolo`. Copilot cannot run tests or publish its own
result.

## Diff and test gates

After Copilot exits, local deterministic checks reject:

- no-op results;
- files outside the allowed globs;
- blocked paths such as workflow, deployment, or infrastructure files;
- symbolic links and binary changes;
- excessive changed files, added lines, or deleted lines;
- a changed Issue snapshot;
- failing or timed-out policy tests;
- any worktree change made while tests were running.

The last check hashes the complete tracked diff and all new file contents
before and after tests. This prevents validating one patch and publishing a
different patch.

## Commands

Read-only preflight and localization:

```bash
./bin/modify-approved-issue \
  https://github.com/OWNER/REPOSITORY/issues/123 \
  --repo /path/to/repository \
  --output .issue-code-output/issue-123-preflight.json
```

Modify the local Issue branch and run tests, without a GitHub write:

```bash
./bin/modify-approved-issue \
  https://github.com/OWNER/REPOSITORY/issues/123 \
  --repo /path/to/repository \
  --execute \
  --output .issue-code-output/issue-123-execution.json
```

Modify, test, commit, push, and create a Draft PR:

```bash
./bin/modify-approved-issue \
  https://github.com/OWNER/REPOSITORY/issues/123 \
  --repo /path/to/repository \
  --publish-pr \
  --output .issue-code-output/issue-123-publication.json
```

`--publish-pr` never merges or deploys. GitHub branch protection, Code Owners,
CI, and human review remain authoritative.

## Audit output

`issue-code-execution/v1` records:

- canonical Issue URL, number, and snapshot SHA-256;
- policy identifier and SHA-256;
- base commit and Issue-bound branch;
- approval rule outcomes;
- Copilot CLI version and pinned model;
- localization candidates;
- changed paths, line counts, and diff SHA-256;
- test commands, exit codes, timings, and output hashes;
- Draft PR URL when publication succeeds.

The report does not persist the raw Issue, Copilot prompt, full Copilot
transcript, OAuth credential, or unrestricted process output.

## Current limitations

- local-user execution only; the GitHub Actions provider is not implemented;
- GitHub.com repositories only;
- one Issue and one repository per invocation;
- no automatic dependency installation;
- no automatic merge, release, deployment, database operation, or production
  action;
- a failed branch with changes is retained for investigation and must be
  handled before retrying;
- repository routing accuracy is an upstream gate and is not repaired by this
  executor.
