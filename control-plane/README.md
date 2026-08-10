# Control plane

This directory contains the first Java 21 backend slice for the company-safe
Issue-to-Draft-PR workflow. It accepts local natural-language tasks and
already-sanitized, deterministically grouped log incidents. A bounded
single-batch adapter connects the legacy sanitized OpenSearch intake; Jira and
a durable company-log watcher are not yet connected. Model, GitHub Issue,
original approved-Issue dispatcher, guarded Copilot, policy tests, and Draft PR
calls are connected behind explicit gates and remain disabled by default. One
explicitly authorized run against the public sandbox completed the guarded
Issue-to-Draft-PR path; this is not production-log or continuous-worker evidence.
The development server binds to `127.0.0.1` by default; authentication for
non-local use is intentionally not part of this slice.

## Current flow

1. `POST /api/tasks` selects the `NATURAL_LANGUAGE` or `LOG_INCIDENT` Issue profile.
2. Natural-language input is locally redacted. A log task accepts only
   `SANITIZED` evidence with an opaque source reference; raw logs are rejected.
3. The log profile persists deterministic first/last occurrence times, current
   and historical counts, incident groups, affected endpoints/user range,
   identifier coverage, and aggregation basis. The model cannot rewrite them.
4. A deterministic local catalog searches only configured, authorized repositories.
5. A resolved task is persisted as `PENDING`; an uncertain task is persisted as
   `NEEDS_CONTEXT` with bounded candidate repositories.
6. After the database commit, Java writes only the task ID to a local Redis
   list. The database remains the source of truth.
7. The bounded Python Mock Worker claims the task through Java. With Issue
   publication disabled, it reuses `repo_locator` against an isolated, clean
   checkout without making external calls.
8. When the policy-pinned Issue gate is enabled, the worker selects the source
   profile, generates and independently reviews an Issue, checks existing Issues for duplication,
   creates one through GitHub REST when needed, refetches the exact Issue
   snapshot, and records the number and URL in the control plane.
9. If separately enabled, the original approved-Issue CLI performs its existing
   approval, snapshot, Claim, localization, Copilot, diff, and test gates. Only
   a tested Draft PR is recorded as `AWAITING_PR_REVIEW`; merge and deployment
   are outside this service.

The current authorized catalog contains the public test repository
`wzf12400/ai-pr-sandbox`. Its GitHub visibility, default `main` branch, and
read/write account permission were verified on 2026-08-10. Runtime writes remain
disabled by default. A bounded synthetic-log integration run created Issue #40
and Draft PR #41 in that sandbox; it did not read company logs, merge, or deploy.

MySQL is the source of truth. Redis is only a local wake-up queue and stores no
task body. The worker receives a minimized, sanitized claim contract rather
than reading MySQL directly. All tasks in this slice use `executionMode=MOCK`;
the real write gates are off by default, and only a persisted Draft PR URL
indicates that the guarded publication path actually completed.

## Local database

Create the local-only database before starting the service:

```sql
CREATE DATABASE github_ai_agent
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;
```

The checked-in defaults use the local Homebrew MySQL `root` user with no
password. Override `DB_URL`, `DB_USER`, and `DB_PASSWORD` outside local
development. Never commit credentials.

## Run

Start local MySQL and Redis first. Install the Python worker dependency once in
the repository virtual environment:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-worker.txt
```

Create the ignored worker checkout once. The worker verifies its origin,
`main` branch, and clean status before every task:

```bash
git clone --depth 1 --single-branch --branch main \
  https://github.com/wzf12400/ai-pr-sandbox.git \
  .worker-repos/ai-pr-sandbox
```

Start the Java control plane:

```bash
cd control-plane
mvn spring-boot:run
```

Create a synthetic task:

```bash
curl -X POST http://127.0.0.1:8080/api/tasks \
  -H 'Content-Type: application/json' \
  -d '{"sourceType":"NATURAL_LANGUAGE","input":"计算器的 divide 遇到零时返回明确错误，并检查 src/calculator.py 和 tests/test_calculator.py"}'
```

Create a synthetic sanitized log incident (an API contract test, not a raw
company-log connector):

```bash
curl -X POST http://127.0.0.1:8080/api/tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "sourceType":"LOG",
    "input":"测试仓库 calculator divide 出现 ZeroDivisionError",
    "logIncident":{
      "dataSafetyStatus":"SANITIZED",
      "sourceReference":"incident_ref:0123456789abcdefabcd",
      "firstSeenAt":"2026-08-04T01:00:00Z",
      "lastSeenAt":"2026-08-04T02:00:00Z",
      "currentScanEventCount":5,
      "historicalEventCount":18,
      "incidentGroupCount":1,
      "affectedEndpoints":["/api/calculator/divide"],
      "affectedUserCountMin":null,
      "affectedUserCountMax":null,
      "userIdentifierEventCount":0,
      "historicalCountComplete":true,
      "aggregationBasis":"service=calculator; exception=ZeroDivisionError"
    }
  }'
```

## Log platform intake

The legacy bounded OpenSearch connector is connected to this control plane
through a one-batch adapter. Start MySQL, Redis, and the Java service, then from
the repository root set the non-secret Discover URL and read-only username:

```bash
export OPENSEARCH_DISCOVER_URL='FULL_BOUNDED_DISCOVER_URL'
export OPENSEARCH_USERNAME='READ_ONLY_USER'
./bin/log-platform-to-tasks --once --prompt-password
```

The password is read without echo and is not written to JSON, MySQL, Redis, or
the command line. When `.issue-entry-state/log-platform.json` is present, the
adapter loads the non-secret URL and username from that owner-only ignored file
and tries the matching macOS Keychain entry before prompting. The adapter
reuses the original bounded query, sanitizer,
deterministic incident grouping, cumulative inbox, delayed-ingestion window,
and forward cursor. It submits only minimized sanitized summaries and
observability statistics to `POST /api/tasks`. Raw hits remain in process
memory and are never sent to the model or control plane.

The cursor is deferred until every candidate in the batch is acknowledged by
the Java API. A failed API call leaves the cursor unchanged. Retrying the same
stable incident reference reuses the existing Java task, and an inbox record
already bound to a task is not submitted again. A successful resolved task is
then enqueued to Redis by Java for the existing Python worker; an uncertain
repository match remains `NEEDS_CONTEXT` and is not guessed.

This command intentionally requires `--once`. It is a verified local intake
bridge, not a durable watcher. Scheduling, acknowledgement retries, dead-letter
handling, and stale-task reconciliation remain part of the production queue
gate below. Real Issue publication and code execution keep their existing,
disabled-by-default policy gates.

Each macOS user can initialize the same connector without editing project
files. From the repository root run:

```bash
./bin/log-platform-to-tasks --configure \
  --discover-url 'FULL_BOUNDED_DISCOVER_URL' \
  --username 'READ_ONLY_USER'
```

The command delegates the hidden password prompt to macOS Keychain and writes
only the URL and username to the ignored owner-only local settings file. Do not
commit a shared password. A deployed service should inject it from the company
secret manager or orchestrator secret, preferably using a dedicated read-only
identity with rotation and audit rather than one shared employee password.

List tasks:

```bash
curl http://127.0.0.1:8080/api/tasks
```

Process at most one queued task and exit (this is not a watcher):

```bash
cd ..
.venv/bin/python -m src.mock_task_worker --once
```

The worker rejects non-loopback control-plane and Redis URLs, a repository not
on its explicit allowlist, a checkout with a different origin or branch, and a
dirty checkout. A duplicate Redis item is harmless because the Java claim is
atomic and only a `PENDING` task can be claimed. If enqueueing fails, the task
stays `PENDING` in MySQL and can be retried locally through
`POST /api/internal/tasks/{taskId}/enqueue`.

The existing AI Issue generator, repository Issue deduplication logic, and
GitHub REST Issue API adapter are connected behind
`WORKER_ISSUE_PUBLICATION_ENABLED=false`. The reviewed policy is pinned by its
SHA-256 digest and currently authorizes only `wzf12400/ai-pr-sandbox`. When the
gate is enabled, the worker requires separate generator/reviewer model settings
and `GITHUB_ISSUE_TOKEN`; the token is never stored in MySQL or Redis. A created
or deduplicated Issue number and URL are recorded in MySQL before code
localization continues.

The original approved-Issue dispatcher and `modify-approved-issue` execution
path are now connected behind `WORKER_CODE_MODE=disabled`. Supported values:

- `disabled`: stop after Issue refetch and local read-only localization;
- `dry_run`: run the original approval/idempotency/localization preflight;
- `execute`: additionally create the exact-snapshot remote claim, call the
  current user's Copilot CLI, validate the diff, and run policy-listed tests;
- `publish_pr`: additionally commit, push, and create a Draft PR.

Every non-disabled mode requires Issue publication to be enabled. The worker
targets the exact task Issue, but it does not bypass the original CLI gates:
the Issue must be open, carry the tracked policy's required approval label,
contain exactly one publication fingerprint, remain unchanged, and contain no
sensitive data. The repository must be clean on its pinned base commit and the
tracked `.github/issue-code-policy.json` controls model, write paths, tests,
limits, Draft-PR-only behavior, and no-auto-merge. Execute/publish modes retain
the original remote claim branch on every outcome. Safe audit JSON is written
under the ignored `WORKER_CODE_AUDIT_DIR`; raw Issue text, prompts, credentials,
and full Copilot output are not persisted.

Automatic company approval is a separate gate:
`WORKER_CODE_AUTO_APPROVAL_ENABLED=false`. Its reviewed policy is independently
confirmed by `WORKER_CODE_AUTO_APPROVAL_POLICY_SHA256` and binds the exact Issue
publication policy SHA, original Issue code policy SHA, repository, source
types, and required labels. Version 1 permits only `LOG` and `JIRA`, applies
labels only to the one Issue newly created by the same invocation, and never
auto-approves natural-language tasks or deduplicated existing Issues. The label
must already exist in the repository; this worker does not let a model create
or choose it. Jira intake is still blocked until its sanitized deterministic
record contract is implemented, so the current runnable automatic source is
only `LOG`.

The Java task records the tested Draft PR only after the original dispatcher
reports `draft_pr_created`, then transitions to `AWAITING_PR_REVIEW`. It never
merges or deploys. If a post-publication label or CLI step fails, the canonical
Issue reference is still recorded before the task fails safely. Keep both code
write gates disabled until a separately authorized bounded integration test is
ready.

## Production queue gate

Before enabling the real Python execution path, Copilot, or automatic Draft PR
creation, replace the current `List` + `BLPOP` mock queue with a reliable
delivery mechanism. The production gate must include explicit acknowledgement,
retry limits, a dead-letter path, stale-task recovery, and idempotent claims
(for example, Redis Streams consumer groups plus MySQL reconciliation).

Run tests:

```bash
mvn test
cd ..
python3 -m unittest discover -s tests
```
