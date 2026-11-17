"""Real locked-SDK capture compatibility, with all paid boundaries mocked."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from .support import OfflineTestCase, question
import shadow_capture as capture

class ShadowPipelineTests(OfflineTestCase):
    async def test_real_binary_pipeline_unchanged_and_encrypted_snapshot(self):
        self.model()
        bot=self.bot(research_reports_per_question=1,predictions_per_research_report=3)
        n=datetime.now(timezone.utc)
        q=question('japanese_diet').model_copy(update={'id_of_post':123,'id_of_question':456,
            'open_time':n-timedelta(hours=1),'scheduled_close_time':n+timedelta(days=1),
            'actual_close_time':None})
        observer=capture.Capture()
        with patch.object(capture,'ENDS_AT',n+timedelta(days=2)):
            observer.attach(bot)
            report=await bot.forecast_question(q)
            observer.detach(bot)
        self.assertEqual(report.prediction,.65)
        self.assertIn((123,456),observer.records)
        frozen=observer.records[(123,456)]
        self.assertIn(q.question_text,frozen['prompt'])
        self.assertNotIn('Probability of YES: 65%',frozen['prompt'])
        env={'GITHUB_ACTIONS':'true','GITHUB_REPOSITORY':capture.REPO,'GITHUB_REF':'refs/heads/main',
             'GITHUB_RUN_ATTEMPT':'1','GITHUB_RUN_ID':'999','GITHUB_SHA':'a'*40,'METACULUS_TOKEN':'k'*64}
        with tempfile.TemporaryDirectory() as d, patch.object(capture,'CAPTURE_FILE',Path(d)/'capture.enc'):
            self.assertTrue(observer.export([report],env))
            raw=(Path(d)/'capture.enc').read_bytes()
            data=json.loads(capture.cipher('k'*64).decrypt(raw))
            self.assertEqual(data['baseline'],[.65,.35])
            self.assertEqual(data['prompt'],frozen['prompt'])
            self.assertNotIn(q.question_text.encode(),raw)
        self.assertFalse(bot.metaculus_client.forecasts)

    async def test_capture_failure_does_not_modify_production_forecast(self):
        self.model()
        bot=self.bot(research_reports_per_question=1,predictions_per_research_report=3)
        observer=capture.Capture()
        with patch.object(observer,'observe',side_effect=RuntimeError('synthetic capture failure')):
            observer.attach(bot)
            result=await bot.forecast_question(question('japanese_diet'))
            observer.detach(bot)
        self.assertEqual(result.prediction,.65)
        self.assertEqual(observer.records,{})

    def test_no_failed_or_foreign_run_export(self):
        observer=capture.Capture()
        self.assertFalse(observer.export([],{}))
        self.assertFalse(observer.export([RuntimeError('no forecast')],{}))

    async def test_multiple_choice_is_never_instrumented_or_captured(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        original = AsyncMock(return_value='unchanged')
        bot = SimpleNamespace(_binary_prompt_to_forecast=original,
                              _multiple_choice_prompt_to_forecast=original)
        observer = capture.Capture()
        observer.attach(bot)
        self.assertIs(bot._multiple_choice_prompt_to_forecast, original)
        q = SimpleNamespace(question_type='multiple_choice')
        observer.observe(bot, q, 'frozen evidence')
        self.assertEqual(observer.records, {})
        self.assertEqual(await bot._multiple_choice_prompt_to_forecast(q, 'evidence'), 'unchanged')
        observer.detach(bot)
