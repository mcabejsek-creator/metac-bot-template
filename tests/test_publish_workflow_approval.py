"""Offline static regression of default-off live-publishing workflow gates."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PUBLISHERS = (
    "run_bot_on_tournament.yaml",
    "run_bot_on_metaculus_cup.yaml",
    "test_bot.yaml",
)


class LivePublishingApprovalGuards(unittest.TestCase):
    def test_all_publishers_require_single_run_explicit_confirmation(self):
        for filename in PUBLISHERS:
            with self.subTest(filename=filename):
                source = (ROOT / ".github" / "workflows" / filename).read_text()
                self.assertIn("  workflow_dispatch:", source)
                self.assertNotIn("\n  schedule:", source)
                self.assertIn("      publication_approval:", source)
                self.assertIn("        required: true", source)
                self.assertIn("        default: DO_NOT_RUN", source)
                self.assertIn("          - DO_NOT_RUN", source)
                self.assertIn("          - APPROVE_ONE_LIVE_PUBLICATION_RUN", source)
                self.assertIn("  forecast_job:\n    if: >-", source)
                self.assertIn(
                    "inputs.publication_approval == 'APPROVE_ONE_LIVE_PUBLICATION_RUN'",
                    source,
                )
                self.assertIn("github.event_name == 'workflow_dispatch'", source)
                self.assertIn("github.run_attempt == 1", source)
                self.assertLess(
                    source.index("  forecast_job:\n    if: >-"),
                    source.index("    runs-on: ubuntu-latest"),
                )
                self.assertIn("  contents: write", source)  # needed ONLY if approved

    def test_stale_external_cron_payload_cannot_authorize_publishing(self):
        # Historic cron-job.org dispatched workflow_dispatch to the tournament
        # workflow with no explicit publication_approval input. The new default
        # DO_NOT_RUN and the server-side if gate keep forecast_job skipped.
        source = (ROOT / ".github" / "workflows" / PUBLISHERS[0]).read_text()
        self.assertIn("default: DO_NOT_RUN", source)
        self.assertIn("inputs.publication_approval == 'APPROVE_ONE_LIVE_PUBLICATION_RUN'", source)


if __name__ == "__main__":
    unittest.main()
