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
- One run fetches at most 100 remote hits and accepts at most 20 local
  candidates.
- Only selected source fields are requested. The full OpenSearch document is
  not requested or persisted.
- Raw hits exist only in process memory. Each hit is sanitized before an
  artifact is written or an AI call is made.
- The default mode is a dry run. AI generation requires `--generate`.
- GitHub publication requires `--generate --publish --confirm` and is limited
  to three candidates per run.
- Events containing credential evidence require separate security review and
  cannot be published by this command.
- Successfully published event references are recorded using HMAC identifiers
  so later runs do not create duplicate Issues.

Use a stable `LOG_SANITIZER_HMAC_KEY` from a local secret manager. Changing the
key changes event references and disables cross-run deduplication.

## 1. Dry run

```bash
export LOG_SANITIZER_HMAC_KEY="<stable-local-secret-at-least-32-bytes>"

./bin/kibana-to-issues \
  --discover-url '<full-discover-url>' \
  --prompt-password
```

The command writes a summary and sanitized candidate events under
`.kibana-issue-output/`. It does not call AI or GitHub.
When no `OPENSEARCH_USERNAME` is set, the command prompts for the username and
then reads the password without echoing it.

## 2. Generate local Issue drafts

Configure the existing AI gateway variables, then run:

```bash
./bin/kibana-to-issues \
  --discover-url '<full-discover-url>' \
  --prompt-password \
  --prompt-api-key \
  --generate
```

Review each generated `candidate-*/issue.md` before publication.

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

## Current boundary

This phase implements one bounded query per run. Scheduling, durable retries,
cursor pagination beyond the first 100 hits, alert grouping, and Jira API
retrieval remain separate future work. A production rollout should add those
capabilities only after a read-only live trial confirms the data-view API,
permissions, query volume, and field mappings.
