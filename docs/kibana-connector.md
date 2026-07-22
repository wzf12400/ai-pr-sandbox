# OpenSearch Dashboards to GitHub Issue

This connector turns a bounded OpenSearch Dashboards Discover view into
sanitized, reviewed GitHub Issue candidates. It does not reuse browser cookies
and does not persist the OpenSearch password or raw search response.

## Required access

Use a dedicated read-only account with access only to:

- read the selected Dashboards data view;
- search the indexes matched by that data view;
- access the Dashboards console proxy used for the bounded search.

Do not use an administrator account. The current phase supports Basic
authentication because the target Dashboards login exposes username/password
authentication. The password can be entered with `--prompt-password` or
supplied through `OPENSEARCH_PASSWORD` by a secret manager for automation.

## Safety contract

- The Discover URL must use HTTPS and contain a relative time range such as
  `now-2h` to `now`.
- One run fetches at most 100 remote hits and accepts at most 20 local incident
  candidates.
- Only selected source fields are requested. The full OpenSearch document is
  not requested or persisted.
- Raw hits exist only in process memory. Each hit is sanitized before an
  artifact is written or an AI call is made.
- Eligible sanitized events are grouped by deterministic local rules before
  the candidate limit or any AI call is applied. The model cannot decide which
  events belong to the same incident.
- Separate incidents with different traces are compared using a conservative,
  versioned Issue signature. Exact service, request path, exception, system,
  and top-frame components suppress repeat Issue candidates without merging
  their incident audit records. The model does not decide this signature.
- The default mode is a dry run. AI generation requires `--generate`.
- GitHub publication requires `--generate --publish --confirm` and is limited
  to three candidates per run.
- Events containing credential evidence require separate security review and
  cannot be published by this command.
- Successfully published event references are recorded using HMAC identifiers
  together with the Issue signature so later runs do not create duplicate
  Issues for the same event or the same deterministic failure shape.

Use a stable `LOG_SANITIZER_HMAC_KEY` from a local secret manager. Changing the
key changes event references and disables cross-run deduplication.

## 1. Dry run

```bash
export LOG_SANITIZER_HMAC_KEY="<stable-local-secret-at-least-32-bytes>"

./bin/kibana-to-issues \
  --discover-url '<full-discover-url>' \
  --prompt-password \
  --timeout-seconds 60
```

The command writes a summary and sanitized incident candidates under
`.kibana-issue-output/`. It does not call AI or GitHub.
When no `OPENSEARCH_USERNAME` is set, the command prompts for the username and
then reads the password without echoing it.
The summary includes aggregate selection diagnostics such as parsed log levels,
blocked events, non-error events, and duplicates. It never includes rejected
raw log messages. For blocked `ERROR` or `FATAL` events, it may include up to
ten minimized previews containing only HMAC event references, timestamps,
software object fields, blocked categories, and a twice-scanned sanitized
summary. Each preview may also contain up to three short contexts around an
already-redacted high-entropy marker. The original candidate value is never
included; each context is sanitized again before it is written.

The per-request timeout defaults to 30 seconds and can be raised with
`--timeout-seconds` up to 120 seconds for a slow read-only endpoint. A timeout
stops the run safely; it does not trigger automatic retries or partial output.

Sanitization minimizes request URLs to a checked route plus query-key names;
the host, fragment, and every query value are removed. Credential-like keys
such as `appKey`, `sign`, and `signature` still mark the incident as requiring
security review, so the connector cannot publish it. Client application and
instance descriptors are removed using their explicit log syntax. Long Java
identifiers bypass the entropy rule only in narrow exception, stack-frame, or
XML class-path contexts. An unexplained high-entropy value anywhere else still
blocks AI and GitHub processing.

MyBatis-style `### SQL:` statements are removed as a whole before entropy
analysis. The surrounding mapper, exception, and database error evidence is
retained, but query text, schema details, literals, and bound values do not
enter local artifacts or AI evidence.

### Incident grouping policy

Each candidate contains a `sanitized-incident.json` audit artifact. Grouping
uses the versioned `kibana-incident-grouping/v1` policy:

- equal non-empty HMAC `trace_ref` values take priority, even across services;
- placeholder trace values such as `-`, `null`, and `unknown` are treated as
  missing rather than shared traces;
- without a trace, events must have the same non-empty service, timestamps no
  more than five seconds apart, and a shared software-semantic signature;
- the narrow exact-timestamp fallback can use a shared fixed system anchor
  such as `S3`; a wider time match also requires matching exception/frame or
  frame/system evidence;
- a multi-event fallback group uses complete-link matching: every new member
  must match every existing member, preventing transitive bridge merges.

The artifact records the strategy, criteria, member HMAC references, pairwise
time deltas, and matched signatures. `--max-candidates` limits incidents after
all returned hits have been sanitized and grouped; it no longer truncates the
event scan before grouping.

## 2. Generate local Issue drafts

Configure the existing AI gateway variables, then run:

```bash
./bin/kibana-to-issues \
  --discover-url '<full-discover-url>' \
  --prompt-password \
  --prompt-api-key \
  --generate
```

Review each `candidate-*/sanitized-incident.json` and generated
`candidate-*/issue.md` before publication.

## 3. Publish reviewed Issues

```bash
./bin/kibana-to-issues \
  --discover-url '<full-discover-url>' \
  --prompt-password \
  --prompt-api-key \
  --generate \
  --publish --confirm \
  --max-candidates 3 \
  --repository owner/repository
```

The generated GitHub Issue remains the sole downstream entry for later code
retrieval, modification, testing, and pull-request work.

## 4. Policy-approved automatic publication

Unattended publication uses a secret-free routing policy rather than a
hard-coded repository. Copy
`examples/auto-publish-policy.example.json`, define exact sanitized service
names and their target GitHub `owner/repository` values, then review the file.
Only the `github_cli` provider exists today; other providers fail closed.

Bind the command to the reviewed bytes of that policy:

```bash
POLICY_SHA256=$(shasum -a 256 path/to/auto-publish-policy.json | awk '{print $1}')

./bin/kibana-to-issues \
  --discover-url '<full-discover-url>' \
  --username "$OPENSEARCH_USERNAME" \
  --generate \
  --auto-publish-policy path/to/auto-publish-policy.json \
  --confirm-policy-sha256 "$POLICY_SHA256" \
  --max-candidates 5
```

Changing any policy byte invalidates the confirmed SHA-256 and stops
publication. A route matches exactly one sanitized service. Missing or
ambiguous routes, blocked/invalid AI results, credential security review, and
the per-run publication limit produce auditable blocked publication results;
they do not authorize a fallback repository. One blocked candidate does not
prevent an independent safe candidate from being published.

The policy is an operator authorization boundary, not model authorization.
AI output cannot add routes, change repositories, increase the run limit, or
make an ineligible event publishable.

### Foreground polling

For continuous operation in one process, inject secrets from a secret manager
or a protected process environment and run:

```bash
export OPENSEARCH_PASSWORD='<injected-secret>'
export AI_API_KEY='<injected-secret>'
export LOG_SANITIZER_HMAC_KEY='<stable-secret-at-least-32-bytes>'

./bin/kibana-issue-watch \
  --interval-seconds 300 \
  -- \
  --discover-url '<full-discover-url>' \
  --username "$OPENSEARCH_USERNAME" \
  --generate \
  --auto-publish-policy path/to/auto-publish-policy.json \
  --confirm-policy-sha256 "$POLICY_SHA256" \
  --max-candidates 5
```

Watch mode rejects interactive password/API-key prompts. Secrets remain
outside the policy and repository. The HMAC key must remain stable across
restarts or event and Issue-signature state cannot provide cross-run
deduplication.

## Current boundary

Each poll still implements one bounded query and in-process deterministic
grouping. The watcher is a foreground polling loop, not a scheduler, durable
queue, durable retry system, or cross-window incident lifecycle. There is no
cursor pagination beyond the first 100 hits and no Jira API retrieval. A
production rollout should add durable supervision, backoff, metrics, policy
deployment, and cursor semantics only after a read-only live trial confirms
the data-view API, permissions, query volume, and field mappings.
