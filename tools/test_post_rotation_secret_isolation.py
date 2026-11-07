"""Fail-closed static regression for the Metaculus credential cutover.

No network or API calls and never imports main.py. Checks that EVERY workflow
using the acting bot's Metaculus credentials binds ONLY the post-rotation
repository secret. Does NOT prove the old token was revoked.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = [
    "run_bot_on_tournament.yaml",
    "run_bot_on_metaculus_cup.yaml",
    "test_bot.yaml",
    "initialize_publication_state.yaml",
    "review_bot.yaml",
]
NEW_BINDING = "METACULUS_TOKEN: " + "$" + "{{ secrets.METACULUS_TOKEN_V2 }}"
OLD_BINDING = "METACULUS_TOKEN: " + "$" + "{{ secrets.METACULUS_TOKEN }}"
EXPECTED_DIR = ROOT / ".github" / "workflows"


class TokenIsolationRegression(unittest.TestCase):
    def test_all_known_entrypoints_bind_only_new_secret(self):
        for name in WORKFLOWS:
            with self.subTest(workflow=name):
                data = (EXPECTED_DIR / name).read_text(encoding="utf-8")
                self.assertEqual(data.count(NEW_BINDING), 1)
                self.assertNotIn(OLD_BINDING, data)
                self.assertNotIn("secrets.METACULUS_TOKEN ||", data)

    def test_publication_flows_stay_manual(self):
        for name in WORKFLOWS[:4]:
            with self.subTest(workflow=name):
                data = (EXPECTED_DIR / name).read_text(encoding="utf-8")
                self.assertIn("  workflow_dispatch:", data)
                self.assertNotIn("\n  schedule:", data)
                self.assertNotIn("\n  push:", data)

    def test_new_secret_not_exposed_in_python_source(self):
        data = (ROOT / "github_publication.py").read_text(encoding="utf-8")
        self.assertIn('os.getenv("METACULUS_TOKEN", "")', data)
        self.assertNotIn("METACULUS_TOKEN_V2", data)

    def test_no_auto_initialize(self):
        data = (EXPECTED_DIR / "initialize_publication_state.yaml").read_text(encoding="utf-8")
        self.assertIn("all-prior-runs-reconciled", data)
        self.assertIn("--confirm-prior-runs-reconciled", data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
