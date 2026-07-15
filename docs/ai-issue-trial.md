# AI Issue generation trial

## Scope

The live trial used public SymPy Issue
[`sympy/sympy#20567`](https://github.com/sympy/sympy/issues/20567). The source
was minimized to its repository, title, body, labels, and public URL before it
crossed the model boundary. User profiles, internal API metadata, credentials,
and unrelated GitHub response fields were excluded.

The configured OpenAI-compatible test gateway ran `gpt-5-mini` once to
generate a strict-schema draft and once to review every claim against the same
minimized evidence. The repository stores neither gateway credentials nor
provider request identifiers.

## Result

The final state was `needs_human_context`, not ready for publication:

- The object was grounded as repository `sympy/sympy` and code object `Symbol`.
- No network interface was present, so all interface fields remained `unknown`.
- The observed `AttributeError` and version difference were preserved.
- The reporter's suspected cause was isolated in `reported_hypothesis`.
- No dedicated expected-behavior evidence existed, so expected behavior stayed
  `unknown` and acceptance criteria stayed empty.
- The second review found no unsupported claims or sensitive data.
- Local validation passed, but publication and implementation remained disabled.

The machine-readable summary is
[`reports/ai-issue-sympy-20567.json`](../reports/ai-issue-sympy-20567.json).

## Iteration evidence

Earlier live attempts were intentionally blocked when the model copied a
speculative root cause into factual background, omitted required evidence
mappings, or split traceback output into reproduction steps. Those failures
were used to add deterministic checks rather than to weaken the gate.

This is one real public-project trial. It demonstrates the guarded workflow,
not general extraction accuracy. Broader evaluation needs a fixed public Issue
set with field-level gold labels and blocked-case expectations.
