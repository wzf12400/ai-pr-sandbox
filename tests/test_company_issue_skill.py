import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = ROOT / ".agents" / "skills" / "company-issue-generator" / "SKILL.md"


class CompanyIssueSkillTest(unittest.TestCase):
    def test_skill_keeps_company_safety_contract(self):
        content = SKILL_PATH.read_text(encoding="utf-8")
        frontmatter = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)

        self.assertIsNotNone(frontmatter)
        self.assertIn("name: company-issue-generator", frontmatter.group(1))
        for contract in (
            "Never send raw Jira or Kibana payloads to the model.",
            "Preserve missing facts as `unknown` or empty lists.",
            "Put source-attributed speculation only in `reported_hypothesis`.",
            "Require evidence paths for every known factual claim.",
            "Never create or update a remote Issue in this skill.",
            "Never start code localization, implementation, testing, or PR creation from raw Jira, Kibana, platform, log, or draft input.",
        ):
            self.assertIn(contract, content)

    def test_upstream_context_inference_rule_is_not_adopted(self):
        content = SKILL_PATH.read_text(encoding="utf-8")

        self.assertNotIn("Infer missing context", content)
        self.assertIn("Do not claim that Jira is connected.", content)
        self.assertIn("issue-intake/v1", content)

    def test_approved_github_issue_is_the_downstream_entry(self):
        content = SKILL_PATH.read_text(encoding="utf-8")

        self.assertIn("The approved GitHub Issue is the only downstream task entry.", content)
        self.assertIn("Do not run code localization or modification from a local Issue draft.", content)
        self.assertIn("does not yet implement the full Issue-to-Code", content)


if __name__ == "__main__":
    unittest.main()
