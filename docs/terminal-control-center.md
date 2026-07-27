# Terminal AI Change Agent

## One entry

Run:

```bash
./bin/ai-agent
```

The terminal displays the active GitHub account, Copilot model, validated
repository checkout, and policy-owned write scope. With no command-line
arguments it shows only a compact pixel mascot and the input prompt; the
function menu appears only after `help`. Employees may enter a natural-language
change request, `logs`/`日志`/`/logs`, `inbox`/`收件箱`,
`review INCIDENT_ID`, `help`, or `exit`/`退出`. This no-argument mode stays
open after completed actions and recoverable input, password, connection, and
policy errors. An empty line returns to the prompt; Ctrl-D or `exit` ends the
session. Explicit command-line modes remain one-shot so scripts retain stable
exit codes. Command-like input is classified locally before any AI call;
unknown slash commands are rejected instead of becoming Issue text.

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

The preferred log path is a persistent local inbox:

```bash
# Poll once while validating the connection and inbox behavior
./bin/ai-agent watch --once

# Keep polling in the foreground
./bin/ai-agent watch

# List safe, deduplicated incidents
./bin/ai-agent inbox

# Generate the exact Issue preview and make the human decision
./bin/ai-agent review INC-123456789ABC
```

The first `watch` asks for:

- a complete HTTPS OpenSearch Dashboards Discover URL with a bounded relative
  time range;
- a dedicated read-only username;
- a password entered without echo.

After the non-secret source is configured, interactive `logs` and `--logs`
reuse its Discover target and read-only username. They ask only for the
process-only password. Explicit `--discover-url` or `--username` values may
still override the local defaults.

The parsed base URL, data-view ID, bounded relative time range, read-only
username, and interval are saved in the ignored owner-only local configuration.
The same `logs` object stores `initial_scan_hits` (default `30`, maximum `100`)
and `max_scan_hits` (default `1000`, maximum `5000`), so both persistent limits
can be changed without changing the startup command.
The password is held in process memory only. It may also be supplied as
`OPENSEARCH_PASSWORD` by the employee's local process manager; it is never
written by this application. HTTP 401 responses trigger up to three hidden
password attempts within the current action; an empty retry returns to the
interactive prompt. A local owner-only HMAC key is
created under `.issue-entry-state/` so event references remain stable without
storing raw identifiers. A new cursor starts from the latest 30 errors rather
than draining the historical two-hour backlog. Later polls overlap the previous
completed window by five minutes, read 50-record pages from one fixed snapshot,
and advance the cursor only after the whole bounded incremental scan succeeds.
If new backlog exceeds the limit, the poll fails without advancing the cursor.
All sanitized incidents are retained in the inbox even when the interactive
selector shows only the first 20. Raw responses and credentials are not written
to disk. Blocked records do not enter AI. Repeated incident references or
deterministic issue signatures update the existing inbox record instead of
creating a second item.

`review` sends only the selected minimized incident through the same no-tools
Copilot Issue generator, independent reviewer, repository resolver, preview,
and approval path as a natural-language request. Ambiguous repositories,
insufficient evidence, and safety failures disable code approval and leave the
incident locally blocked until the employee adds context.

The inbox survives terminal restarts. The watcher itself is currently a
foreground polling process, not an installed launch service or durable remote
queue. Closing the terminal stops new polling but does not lose inbox state.

## One approval

Before any remote write, the terminal shows:

- exact Issue title and body;
- target repository and Copilot model;
- approval labels and allowed write paths;
- Issue publication, code modification, tests, and Draft PR scope.

Natural-language mode keeps its single `y` approval. Log review offers these
explicit actions after the exact Issue preview:

- `a`: publish or reuse the Issue, internally apply the repository-owned
  `ai-code-approved` evidence, claim the exact snapshot, run Copilot and tests,
  and create a Draft PR;
- `i`: publish or reuse the Issue only, without adding the code-approval label
  and without calling the code dispatcher;
- `e`: add sanitized human context and regenerate on the next review;
- `s`: snooze locally for 24 hours;
- `x`: ignore locally without remote writes.

The `a` and `i` plans have different approval digests. An Issue-only approval
cannot authorize code modification. Any other input cancels without
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

# Preferred persistent log inbox
./bin/ai-agent watch --once
./bin/ai-agent watch
./bin/ai-agent inbox
./bin/ai-agent review INC-123456789ABC

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
model, repository names, local checkout paths, and non-secret parsed log-source
settings. GitHub and Copilot credentials stay in their existing CLI sessions;
the log password stays in the current process. The application runs in the
foreground and is not a merge service or deployment system.
