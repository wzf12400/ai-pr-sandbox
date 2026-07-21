# AI PR Sandbox

This repository is a small, controlled target for testing an AI-assisted
GitHub workflow:

1. Turn a natural-language request into a structured issue.
2. Create a branch and implement the requested change.
3. Run automated tests in GitHub Actions.
4. Open a draft pull request for human review.

## Run tests

```bash
python3 -m unittest discover -s tests -v
```

The initial calculator supports addition and subtraction. New behavior should
be introduced through GitHub issues and pull requests.

## Generate a local Issue draft

The deterministic draft command uses synthetic, sanitized input only. It does
not call an AI model, Jira, Kibana, or the GitHub API.

Three sample sources are available under `examples/`:

- `manual.json`
- `jira.json`
- `kibana.json`

Generate a local Markdown draft:

```bash
python3 -m src.issue_draft examples/jira.json \
  --output drafts/jira-CALC-101.md
```

The command validates required fields, scans for common secret and personal
data patterns, and records the source reference in `.issue-draft-state.json`.
Running the same source record twice is rejected. Generated drafts and local
state are ignored by Git.

The internal JSON keeps detailed evidence for later retrieval, while the
generated Issue renders eight compact sections. Log and stack excerpts are
limited to 50 lines, and request or response summaries are limited to 4,000
characters each.

## Sanitize a raw Kibana event

`examples/kibana_raw.json` is fully synthetic but follows the expected
Elasticsearch hit shape. Set a local HMAC key and generate an AI-safe event:

```bash
export LOG_SANITIZER_HMAC_KEY="<local-test-key-at-least-32-bytes>"
python3 -m src.kibana_sanitizer examples/kibana_raw.json \
  --output sanitized/kibana-event.json
```

The sanitizer parses the Java log envelope, removes secret and identifier
fields, converts event and trace identifiers to HMAC references, omits internal
container identifiers, and performs a final secret scan. Unclassified
high-entropy values block downstream AI and Issue processing.

## Run the phase-one flow

Run the complete local path from one raw Kibana hit to a sanitized event and,
only for an eligible error, a guarded triage draft:

```bash
export LOG_SANITIZER_HMAC_KEY="<local-key-at-least-32-bytes>"
python3 -m src.phase_one kibana examples/kibana_raw.json \
  --sanitized-output sanitized/kibana-event.json \
  --draft-output drafts/kibana-error.md
```

The draft always includes object, interface, and error sections. Information
that is not present in the log is listed as missing instead of being invented.
Non-error events are skipped, blocked events stay blocked, and publication is
disabled until the required context and security review gates are complete.

Locate a real GitHub Issue in a checked-out repository:

```bash
python3 -m src.phase_one locate-github-issue .trial-data/issue.json \
  --repo .trial-repos/project \
  --output reports/location.json
```

The locator scans tracked source files without executing repository code. It
combines bounded lexical retrieval with Python AST class-inheritance analysis
and returns ranked files, symbols, lines, and human-readable evidence.

The first real-project benchmark uses SymPy Issue #20567 and its fixing PR.
See [`docs/real-project-trial.md`](docs/real-project-trial.md) for the pinned
inputs, safety boundary, reproduction commands, and measured result. The
machine-readable output is stored in
[`reports/real-project-sympy-20567.json`](reports/real-project-sympy-20567.json).

## Generate an Issue with AI

The AI command accepts a sanitized `issue-intake/v1` record, a
`sanitized-kibana-event/v1` event, or a public GitHub Issue API response. Raw
Jira and Kibana payloads are not accepted at the model boundary.

Configure an OpenAI-compatible Chat Completions gateway locally. Never commit
the real values from `.env`:

```bash
export AI_BASE_URL="https://example.invalid/api/v1"
export AI_API_KEY="<local-secret>"
export AI_MODEL="ailemac/gpt-5-mini"
export AI_REVIEW_MODEL="ailemac/gpt-5-mini"
export AI_SAFETY_IDENTIFIER="<local-stable-identifier>"
```

Generate and review a local draft:

```bash
python3 -m src.phase_one ai-issue .trial-data/issue.json \
  --output-json reports/ai-issue.json \
  --output-md drafts/ai-issue.md
```

The gateway request uses strict JSON Schema and `max_completion_tokens`. A
second model call reviews claims against the minimized evidence. Local code
then rejects extra fields, unknown evidence paths, unsupported claims, and
sensitive output. The persisted result contains an input hash instead of the
raw source. Phase one always requires human confirmation and keeps both GitHub
publication and AI implementation disabled.

The live public-project AI trial uses SymPy Issue #20567. See
[`docs/ai-issue-trial.md`](docs/ai-issue-trial.md) for its safety boundary,
observed blocked iterations, final guarded result, and limitations.

## Create an Issue from natural language and a log

The terminal entry accepts a UTF-8 description plus one raw Kibana hit or
plain-text log. It sanitizes and minimizes both inputs, runs the guarded AI
generator and reviewer, and writes local audit artifacts under
`.issue-entry-output/`:

```bash
export LOG_SANITIZER_HMAC_KEY="<local-key-at-least-32-bytes>"
export AI_BASE_URL="https://example.invalid/api/v1"
export AI_MODEL="ailemac/gpt-5-mini"

./bin/issue-entry \
  --description-file examples/natural_request.txt \
  --log examples/kibana_error_raw.json \
  --prompt-api-key
```

Review the generated `issue.md`. To create the Issue in the repository, first
authenticate `gh`, then make the human publication decision explicit:

```bash
gh auth login
./bin/issue-entry \
  --description-file examples/natural_request.txt \
  --log examples/kibana_error_raw.json \
  --repository wzf12400/ai-pr-sandbox \
  --prompt-api-key --publish --confirm
```

`--prompt-api-key` reads the secret without echoing it and does not save it to
the repository. `AI_API_KEY` may still be supplied as an environment variable
for non-interactive automation.

For gateways that implement the older Chat Completions parameter names, set
`AI_API_MODE=compatible`. This sends `max_tokens` and JSON object mode; the
same strict local schema and evidence validation still run before publication.

The command rejects blocked AI output and prevents publication when credentials
were present in the source, even after redaction. It never uses model output as
authorization to modify code.

## Pull error candidates from OpenSearch Dashboards

`bin/kibana-to-issues` accepts a complete Discover URL, resolves its data view,
and performs a bounded read-only error search. The default run only writes
sanitized local incident candidates. Deterministic grouping runs before the
candidate limit and before AI: equal HMAC trace references take priority, while
trace-less fallback grouping requires the same service, a bounded time window,
and auditable software-semantic signatures.

```bash
export LOG_SANITIZER_HMAC_KEY="<stable-local-secret-at-least-32-bytes>"

./bin/kibana-to-issues \
  --discover-url '<full-discover-url>' \
  --prompt-password
```

Add `--generate --prompt-api-key` for locally reviewed AI drafts. Publishing
also requires `--publish --confirm`, a GitHub repository, and a maximum of
three candidates per run. Raw OpenSearch responses, passwords, and AI keys are
not persisted.

See [`docs/kibana-connector.md`](docs/kibana-connector.md) for access
requirements, the complete commands, safety gates, and current phase boundary.
