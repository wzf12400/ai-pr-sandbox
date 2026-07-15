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
