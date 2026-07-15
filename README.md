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

This phase uses synthetic, sanitized input only. It does not call an AI model,
Jira, Kibana, or the GitHub API.

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
