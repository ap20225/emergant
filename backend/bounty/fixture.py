"""Deliberately controlled reference repo, never represented as a real bounty."""
from .models import OpportunityInput

ORIGINAL = 'def slugify(value):\n    """Convert whitespace-separated words to a URL slug."""\n    return value.lower().replace(" ", "-")\n'
FIXED = 'def slugify(value):\n    """Convert whitespace-separated words to a URL slug."""\n    return "-".join(value.lower().split())\n'
TESTS = '''import unittest
from slugify import slugify

class SlugifyTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(slugify("Hello World"), "hello-world")
    def test_multiple_spaces(self):
        self.assertEqual(slugify("Hello   World"), "hello-world")
    def test_trim(self):
        self.assertEqual(slugify("  Hello World  "), "hello-world")
    def test_tabs_and_newlines(self):
        self.assertEqual(slugify("Hello\\tWorld\\nAgain"), "hello-world-again")
    def test_empty(self):
        self.assertEqual(slugify(""), "")
    def test_whitespace_only(self):
        self.assertEqual(slugify("  \\t  "), "")
    def test_unicode(self):
        self.assertEqual(slugify("Café Society"), "café-society")

if __name__ == "__main__":
    unittest.main(verbosity=2)
'''


def opportunity():
    data = OpportunityInput(title="Normalize consecutive whitespace in slug generation",
        description="SAFE TEST FIXTURE — slugify produces repeated hyphens and fails on tabs/newlines. Preserve normal and Unicode inputs. This is not a real bounty or reward.",
        repository="penniless-lab/slugify", issue_number=1, reward=40, estimated_hours=0.5,
        probability_of_success=0.9, probability_of_payment=0.8, estimated_difficulty=2,
        competition=0, repository_health=1, agent_compatibility=1, risk_score=2,
        payment_terms="Simulated USD reward only. No external payer or actual payment.").model_dump()
    return {**data, "source": "safe-test", "source_url": None, "simulated": True,
        "payment_verified": False, "payment_method": "SIMULATION", "deadline": None,
        "labels": ["safe-test", "bug", "python"], "repository_size": 3,
        "requirements": ["Collapse consecutive whitespace", "Retain Unicode", "All seven regression tests pass"]}