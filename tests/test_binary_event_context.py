"""Binary forecasts must distinguish observed events from platform timing."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from .support import NOW, OfflineTestCase, question
from question_context import TARGET_PERIOD_NOTE


class BinaryEventContextTests(OfflineTestCase):
    async def capture(self, q, research):
        bot = self.bot()
        probe = AsyncMock(return_value=None)
        with patch.object(bot, "_binary_prompt_to_forecast", probe):
            await bot._run_forecast_on_binary(q, research)
        probe.assert_awaited_once()
        self.assertIs(probe.await_args.args[0], q)
        return probe.await_args.args[1]

    async def test_open_question_does_not_contradict_completed_qualifying_event(self):
        q = question("japanese_diet").model_copy(update={
            "question_text": "Will the synthetic council approve motion X during November 1-8?",
            "resolution_criteria": "Resolve Yes if motion X is approved between 2026-11-01T00:00:00Z and 2026-11-08T23:59:59Z.",
            "fine_print": "Approval is sufficient; no later signature is required.",
            "background_info": "The question remains open for forecasts.",
            "close_time": NOW + timedelta(hours=3),
            "scheduled_resolution_time": NOW + timedelta(days=14),
            "api_json": {"question": {"status": "open"}},
        })
        evidence = "Synthetic official minutes: motion X was approved on 2026-11-05 at 15:00 UTC."
        prompt = await self.capture(q, evidence)
        for source in (q.question_text, q.resolution_criteria, q.fine_print, evidence):
            self.assertIn(source, prompt)
        self.assertNotIn("These criteria have not yet been satisfied", prompt)
        self.assertIn("An open or unresolved question on Metaculus does not establish whether the real-world event has already occurred", prompt)
        self.assertIn("remaining requirements in the criteria or fine print", prompt)

    async def test_later_adjudication_does_not_create_extra_qualifying_days(self):
        q = question("japanese_diet").model_copy(update={
            "question_text": "Will instrument X exceed its threshold during the specified observation window?",
            "resolution_criteria": "Only readings from 2026-11-07T00:00:00Z through 2026-11-08T23:59:59Z qualify.",
            "fine_print": "Readings on November 9 or later do not count, even if the data are reviewed later.",
            "background_info": "Synthetic timing fixture with a separate adjudication date.",
            "close_time": NOW + timedelta(hours=3),
            "scheduled_resolution_time": NOW + timedelta(days=14),
        })
        evidence = "Synthetic source: an above-threshold reading is expected on November 12, outside the qualifying observation window."
        prompt = await self.capture(q, evidence)
        for source in (q.resolution_criteria, q.fine_print, evidence, TARGET_PERIOD_NOTE):
            self.assertIn(source, prompt)
        self.assertIn("Days until close: 0", prompt)
        self.assertIn("Days until scheduled resolution: 14", prompt)
        self.assertIn("A later scheduled resolution time does not extend that event window", prompt)
        self.assertNotIn("assess the remaining opportunity using the scheduled resolution time", prompt)
        self.assertIn("Never use zero or expired days until close as evidence for Yes or No", prompt)
