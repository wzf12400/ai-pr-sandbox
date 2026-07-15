# Real project trial: SymPy Issue #20567

Run date: 2026-07-15

This trial uses public artifacts instead of a locally invented bug:

- Repository: https://github.com/sympy/sympy
- Issue: https://github.com/sympy/sympy/issues/20567
- Fixing PR: https://github.com/sympy/sympy/pull/20590
- PR base commit: `cffd4e0f86fefd4802349a9f9b19ed70934ea354`

The GitHub Issue reports that `Symbol` instances unexpectedly gained a
`__dict__` in SymPy 1.7. The real fixing PR changed these files:

- `sympy/core/_print_helpers.py`
- `sympy/core/tests/test_basic.py`

## Safety boundary

The trial checks out the PR's base commit and performs a read-only scan of Git
tracked source files. It does not install SymPy dependencies, import SymPy, run
its tests, or execute any third-party repository code. Symlinked source files
are excluded from indexing.

## Reproduce

Download the public Issue and PR metadata with the GitHub API, then initialize
a local repository and fetch only the recorded base commit:

```bash
mkdir -p .trial-data .trial-repos/sympy reports
curl --fail --location \
  https://api.github.com/repos/sympy/sympy/issues/20567 \
  --output .trial-data/sympy-20567.json
curl --fail --location \
  https://api.github.com/repos/sympy/sympy/pulls/20590 \
  --output .trial-data/sympy-pr-20590.json
curl --fail --location \
  'https://api.github.com/repos/sympy/sympy/pulls/20590/files?per_page=100' \
  --output .trial-data/sympy-pr-20590-files.json
git init .trial-repos/sympy
git -C .trial-repos/sympy remote add origin https://github.com/sympy/sympy.git
git -C .trial-repos/sympy fetch --depth=1 origin \
  cffd4e0f86fefd4802349a9f9b19ed70934ea354
git -C .trial-repos/sympy checkout --detach FETCH_HEAD
python3 -m src.real_project_trial \
  --issue-json .trial-data/sympy-20567.json \
  --pr-json .trial-data/sympy-pr-20590.json \
  --pr-files-json .trial-data/sympy-pr-20590-files.json \
  --repo .trial-repos/sympy \
  --output reports/reproduced-sympy-20567.json \
  --top-k 10
```

## Result

- Indexed source files: 1,446
- Indexed Python classes: 1,959
- Real implementation file rank: `sympy/core/_print_helpers.py:8`, rank 1
- Real regression test file rank: `sympy/core/tests/test_basic.py`, rank 4
- Gold implementation recall at 10: 100%
- Gold file recall at 10: 100%
- Third-party code executed: no

The locator also normalized the public Jupyter traceback frame
`<ipython-input-...>` before scanning. Unknown high-entropy values remain a
hard stop.

This is one real benchmark case, not a general accuracy claim. More repositories,
languages, and issue types are needed before setting a production threshold.
