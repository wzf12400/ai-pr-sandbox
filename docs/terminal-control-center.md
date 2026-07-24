# Terminal AI Change Agent

## One entry

Run:

```bash
./bin/ai-agent
```

The terminal displays the active GitHub account, Copilot model, validated
repository checkout, and policy-owned write scope. It then accepts one input:
a natural-language change request, or `/logs` to read an OpenSearch Dashboards
candidate.

The natural-language path treats the employee's request as explicit requested
behavior. Feature, refactor, and documentation work therefore does not require
an artificial error message or description of current behavior. Bug,
performance, and security reports must still contain an observed problem.
Unknown facts remain unknown.

When the configuration enables exactly one repository, that operator-approved
scope is the repository decision. The agent does not require the request to
contain an English class, method, or file name and does not run GitHub code
search merely to rediscover the only allowed repository. With multiple enabled
repositories, the evidence-grounded resolver remains mandatory.

## Log platform path

Log mode asks for:

- a complete HTTPS OpenSearch Dashboards Discover URL with a bounded relative
  time range;
- a dedicated read-only username;
- a password entered without echo.

The password is held in process memory only. A local owner-only HMAC key is
created under `.issue-entry-state/` so event references remain stable without
storing raw identifiers. The connector requests at most 50 records and only
whitelisted fields, sanitizes each record, groups related events
deterministically, and displays at most five candidates. Raw responses and
credentials are not written to disk. Blocked records do not enter AI.

The selected sanitized incident uses the same no-tools Copilot Issue generator,
independent reviewer, repository resolver, preview, and approval path as a
natural-language request.

## One approval

Before any remote write, the terminal shows:

- exact Issue title and body;
- target repository and Copilot model;
- approval labels and allowed write paths;
- Issue publication, code modification, tests, and Draft PR scope.

Only `y` approves the displayed digest. Any other input cancels without
creating an Issue or modifying code. After approval, the agent may publish the
Issue, apply its repository-owned approval labels, claim the exact Issue
snapshot, call Copilot, validate the diff, run policy-listed tests, and create
a Draft PR. It never merges or deploys.

The approved Issue URL is dispatched directly and then fully revalidated; the
terminal does not wait for GitHub's label search index to discover an Issue it
just created. If an earlier attempt created the exact fingerprinted Issue but
stopped before a claim, a fresh run and fresh `y` approval reuse that Issue
instead of creating a duplicate. Existing claim/work branches or a Draft PR
still stop duplicate execution.

Code localization receives a task-only projection of the canonical Issue.
System-owned Source, Review Gate, routing-audit, fingerprint, and validated
repository metadata are excluded from locator text, so deterministic IDs do
not masquerade as secrets or code clues. Object, interface, error, behavior,
reproduction, impact, and acceptance content stays under the normal
high-entropy and credential checks.

## Commands

```bash
# Interactive source selection
./bin/ai-agent

# One natural-language request
./bin/ai-agent --request '在计算器模块新增乘法功能，并添加正数、负数和零的测试。'

# Read from the log platform
./bin/ai-agent --logs

# Generate the preview and stop before all remote writes
./bin/ai-agent --request '...' --preview-only

# Deliberately resume an eligible run whose exact remote claim was retained
./bin/ai-agent --resume 20260724T083021Z-542700c2

# Replace the configured repository
./bin/ai-agent --configure
```

Resume is not a general retry switch. It is accepted only when the latest
append-only dispatch audit proves that the exact Issue snapshot was claimed,
no local/remote work branch or Draft PR conflicts with recovery, and either:

- Copilot did not start because of a bounded pre-modifier failure; or
- Copilot returned success but produced an exact empty diff, no tests ran, and
  no Draft PR was created.

An audited empty local work branch may be removed only when it is still clean,
still points at the recorded base commit, and exactly matches the recorded
work-branch name. Every attempt displays the retained claim and requires a
fresh `y`. The terminal revalidates the live Issue, claim commit, repository,
branch, and PR state. It never deletes or replaces the claim.

Recovery is bounded to three explicitly approved attempts. Each result is
written to a new audit file (`dispatch-resume.json`,
`dispatch-resume-2.json`, and `dispatch-resume-3.json`); earlier audit files
are never overwritten. There is no automatic retry loop.

The ignored configuration stores only the GitHub login, selected Copilot
model, repository names, and local checkout paths. GitHub and Copilot
credentials stay in their existing CLI sessions. The application runs in the
foreground and is not a durable queue, scheduler, merge service, or deployment
system.
