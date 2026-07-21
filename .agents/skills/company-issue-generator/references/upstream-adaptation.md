# Upstream Adaptation

## Source

- Repository: `microsoft/skills`
- Path: `.github/skills/github-issue-creator/SKILL.md`
- Source URL: https://github.com/microsoft/skills/blob/main/.github/skills/github-issue-creator/SKILL.md
- Retrieved: 2026-07-21
- Retrieved SHA-256: `b6515408456d0eccf77ebd8418aec34f3f0ccdf25919a9486da24a8d0f029da8`
- Upstream repository license: MIT

## Retained Ideas

- Convert notes, logs, and screenshots into a concise Issue.
- Separate summary, environment, reproduction, expected behavior, actual behavior, errors, visual evidence, impact, and additional context.
- Keep generated prose compact and actionable.

## Company-Safe Changes

- Replace contextual inference with explicit `unknown` values.
- Require evidence mapping for known facts.
- Keep source-attributed speculation in `reported_hypothesis`.
- Prevent the model from assigning incident severity without an authoritative source.
- Route raw Kibana events through deterministic sanitization before AI processing.
- Reject raw Jira payloads until a trusted connector produces `issue-intake/v1`.
- Add strict schema generation, second-pass model review, and local fail-closed validation.
- Disable remote Issue creation and implementation inside the skill.

The upstream file is a writing-oriented reference. This adapted skill uses the repository's existing safety and validation implementation as the executable contract.
