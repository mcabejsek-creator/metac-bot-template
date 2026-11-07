import asyncio
import http.client
import io
import ssl
import urllib.error
from datetime import datetime, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import requests

from . import safety
from .support import OfflineTestCase, agent_response, app, question
from .test_runtime_limits import RuntimeTestHelpers
from research_safety import (
    ResearchAccessDenied, ResearchDeadlineExceeded, ResearchValidationError,
    parse_agent_response, retry_after_seconds, transient_research_error,
    call_agent_once,
)
from runtime_limits import BlockingDeadlineExceeded


def http_error(code, headers=None):
    return urllib.error.HTTPError("https://offline.invalid", code, "Synthetic error", headers or {}, io.BytesIO(b"offline"))


class AgentValidationTests(OfflineTestCase):
    def test_failed_pending_incomplete_missing_and_malformed_outputs_are_rejected(self):
        cases = [None, [], {}, b"", {"status": "completed"},
                 {"status": "completed", "output": []}, agent_response(""), agent_response(" \n ")]
        for status in ("failed", "incomplete", "in_progress", "queued", "cancelled", None):
            cases.append({**agent_response("Some partial facts"), "status": status})
        cases.extend([
            {**agent_response("Facts"), "error": {"code": 500}},
            {**agent_response("Facts"), "incomplete_details": {"reason": "max_tokens"}},
            {**agent_response("Facts"), "finish_reason": "length"},
            {"status": "completed", "output": [None]},
            {"status": "completed", "output": [{"type": "message", "content": []}]},
            {"status": "completed", "output": [{"type": "message", "content": "wrong"}]},
            {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "text": "No"}]}]},
            {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": None}]}]},
            {"status": "completed", "output": [{"type": "search_results", "results": [{"url": "https://offline.invalid"}]}]},
        ])
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ResearchValidationError):
                parse_agent_response(data)

    def test_completed_text_does_not_hide_a_failed_message_or_error_item(self):
        for changes in ({"status": "incomplete"}, {"finish_reason": "length"}, {"error": {}}):
            data = agent_response("Useful but truncated")
            data["output"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ResearchValidationError):
                parse_agent_response(data)
        data = agent_response("Some facts")
        data["output"].append({"type": "error"})
        with self.assertRaises(ResearchValidationError):
            parse_agent_response(data)

    def test_valid_text_and_sources_keep_the_original_format(self):
        data = agent_response("  Exact research text.  ")
        data["output"].append({"type": "search_results", "results": [
            {"title": "Official", "url": "https://offline.invalid/source"}]})
        self.assertEqual(parse_agent_response(data), "  Exact research text.  \n\nSources:\n- Official: https://offline.invalid/source")

    async def test_invalid_research_blocks_models_and_both_publication_steps(self):
        for invalid in (b"", b"not-json", None, agent_response(""),
                        {**agent_response("Partial text"), "status": "incomplete"},
                        {**agent_response("Partial text"), "status": "failed"}):
            with self.subTest(invalid=invalid):
                calls = self.agent([invalid, invalid])
                models = self.model()
                bot = self.bot(publish_reports_to_metaculus=True)
                bot.set_llm("perplexity-agent", "researcher")
                q = question("japanese_diet")
                result = await bot.forecast_questions([q], return_exceptions=True)
                self.assertIsInstance(result[0], Exception)
                self.assertEqual(len(calls), 2)  # Two configured reports; no malformed-response retry.
                self.assertEqual(models, [])
                self.assertEqual((bot.metaculus_client.forecasts, bot.metaculus_client.comments), ([], []))
                self.assertIsNone(self.publication_store.pending(bot._publication_resource(q)))

    async def test_failed_fact_check_never_falls_back_to_unverified_initial_research(self):
        for invalid in (agent_response(""), {"status": "failed", "output": []}):
            with self.subTest(invalid=invalid):
                calls = self.agent([agent_response("Unverified first pass"), invalid] * 2)
                models = self.model()
                bot = self.bot(publish_reports_to_metaculus=True)
                bot.set_llm("perplexity-agent", "researcher")
                result = await bot.forecast_questions([question("japanese_diet")], return_exceptions=True)
                self.assertIsInstance(result[0], Exception)
                self.assertEqual((len(calls), len(models)), (4, 0))
                self.assertFalse(bot.metaculus_client.forecasts)

    async def test_one_valid_report_preserves_existing_threshold_and_visible_error(self):
        self.agent([agent_response("Initial facts"), agent_response("Verified facts"), agent_response("")])
        models = self.model()
        bot = self.bot(publish_reports_to_metaculus=True)
        bot.set_llm("perplexity-agent", "researcher")
        with self.assertLogs("forecasting_tools.forecast_bots.forecast_bot", level="WARNING") as logs:
            report = await bot.forecast_question(question("japanese_diet"))
        self.assertEqual(report.prediction, .65)
        self.assertEqual(len(models), 9)  # Only the valid report's 3 forecasts + 6 parser calls.
        self.assertTrue(any("ResearchValidationError" in error for error in report.errors))
        self.assertTrue(any("Encountered errors while researching" in line for line in logs.output))
        self.assertEqual((len(bot.metaculus_client.forecasts), len(bot.metaculus_client.comments)), (1, 1))

    async def test_explicitly_optional_research_still_allows_forecasts(self):
        calls = self.agent([])
        for index, mode in enumerate(("no_research", "None", "", None)):
            with self.subTest(mode=mode):
                models = self.model()
                bot = self.bot(publish_reports_to_metaculus=True)
                bot.set_llm(mode, "researcher")
                q = question("japanese_diet").model_copy(update={"id_of_question": index + 100})
                with self.assertLogs("main", level="INFO") as logs:
                    report = await bot.forecast_question(q)
                self.assertEqual(report.prediction, .65)
                self.assertFalse(report.errors)
                self.assertEqual(len(models), 18)
                self.assertTrue(any("Research intentionally disabled" in line for line in logs.output))
        self.assertEqual(calls, [])

    async def test_enabled_model_cannot_silently_become_optional(self):
        async def empty(*args, **kwargs):
            return "  "

        self.start_patch(patch.object(app.GeneralLlm, "invoke", empty))
        bot = self.bot(publish_reports_to_metaculus=True)
        bot.set_llm("openrouter/openai/gpt-4o-mini", "researcher")
        result = await bot.forecast_questions([question("japanese_diet")], return_exceptions=True)
        self.assertIsInstance(result[0], Exception)
        self.assertFalse(bot.metaculus_client.forecasts)


class ResearchRetryTests(OfflineTestCase):
    def agent_bot(self):
        bot = self.bot()
        bot.set_llm("perplexity-agent", "researcher")
        return bot

    def simulated_backoff(self):
        self.start_patch(patch("research_safety.random.uniform", return_value=0))
        return self.start_patch(patch("research_safety.asyncio.sleep", new=AsyncMock()))

    async def test_transient_errors_recover_without_rerunning_successful_stages(self):
        errors = [http_error(429), http_error(500), http_error(503), TimeoutError("offline"),
                  urllib.error.URLError("offline outage"), http.client.IncompleteRead(b"partial")]
        for error in errors:
            with self.subTest(error=type(error).__name__, status=getattr(error, "code", None)):
                calls = self.agent([agent_response("Initial"), error, agent_response("Verified")])
                sleep = self.simulated_backoff()
                self.assertEqual(await self.agent_bot().run_research(question("fda")), "Verified")
                self.assertEqual(len(calls), 3)
                self.assertEqual(calls[1]["payload"], calls[2]["payload"])
                self.assertNotEqual(calls[0]["payload"], calls[1]["payload"])
                sleep.assert_awaited_once_with(2)

    async def test_transient_attempts_are_bounded_and_response_is_closed(self):
        for code in (408, 429, 500, 503):
            with self.subTest(code=code):
                error = http_error(code)
                calls = self.agent(error=error)
                sleep = self.simulated_backoff()
                with self.assertRaises(urllib.error.HTTPError):
                    await self.agent_bot().run_research(question("fda"))
                self.assertEqual(len(calls), 3)
                self.assertEqual([call.args[0] for call in sleep.await_args_list], [2, 4])
                self.assertTrue(error.closed)

    async def test_permanent_errors_do_not_backoff_or_retry(self):
        for code in (400, 401, 403, 404, 422):
            with self.subTest(code=code):
                error = http_error(code)
                calls = self.agent(error=error)
                sleep = self.simulated_backoff()
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    await self.agent_bot().run_research(question("fda"))
                self.assertIs(caught.exception, error)
                self.assertEqual(len(calls), 1)
                self.assertTrue(error.closed)
                sleep.assert_not_awaited()

    async def test_403_billing_stops_later_reports_but_new_instance_can_try_again(self):
        calls = self.agent(error=http_error(403))
        sleep = self.simulated_backoff()
        bot = self.agent_bot()
        bot.publish_reports_to_metaculus = True
        result = await bot.forecast_questions([question("japanese_diet")], return_exceptions=True)
        self.assertIsInstance(result[0], Exception)
        self.assertEqual(len(calls), 1)
        self.assertFalse(bot.metaculus_client.forecasts)
        with self.assertRaises(ResearchAccessDenied):
            await bot.run_research(question("fda"))
        self.assertEqual(len(calls), 1)
        with self.assertRaises(urllib.error.HTTPError):
            await self.agent_bot().run_research(question("fda"))
        self.assertEqual(len(calls), 2)
        sleep.assert_not_awaited()

    async def test_retry_after_seconds_is_a_minimum_wait(self):
        calls = self.agent([http_error(429, {"Retry-After": "7"}), agent_response("Initial"), agent_response("Verified")])
        sleep = self.simulated_backoff()
        await self.agent_bot().run_research(question("fda"))
        self.assertEqual(len(calls), 3)
        sleep.assert_awaited_once_with(7)

    async def test_denial_observed_during_backoff_prevents_another_attempt(self):
        calls = self.agent(error=http_error(500))
        bot = self.agent_bot()

        async def sleep(delay):
            bot._research_denials["agent"] = 403

        with patch("research_safety.asyncio.sleep", sleep):
            with self.assertRaises(ResearchAccessDenied):
                await bot.run_research(question("fda"))
        self.assertEqual(len(calls), 1)

    def test_socket_timeout_is_capped_by_the_remaining_phase_budget(self):
        calls = self.agent([agent_response("Research")])
        with patch("research_safety.time.monotonic", return_value=100):
            self.assertEqual(call_agent_once("same prompt", 117), "Research")
        self.assertEqual(calls[0]["timeout"], 17)
        with patch("research_safety.time.monotonic", return_value=118):
            with self.assertRaises(ResearchDeadlineExceeded):
                call_agent_once("same prompt", 117)
        self.assertEqual(len(calls), 1)

    def test_retry_after_http_date_and_invalid_values(self):
        now = 1_600_000_000
        future = format_datetime(datetime.fromtimestamp(now + 12, timezone.utc), usegmt=True)
        with patch("research_safety.time.time", return_value=now):
            self.assertEqual(retry_after_seconds(http_error(503, {"Retry-After": future})), 12)
        for value in ("wrong", "-1", "NaN", "inf"):
            self.assertIsNone(retry_after_seconds(http_error(429, {"Retry-After": value})))

    async def test_retry_after_exceeding_deadline_is_never_truncated_to_an_early_retry(self):
        calls = self.agent(error=http_error(429, {"Retry-After": "3600"}))
        sleep = self.simulated_backoff()
        with self.assertRaises(ResearchDeadlineExceeded):
            await self.agent_bot().run_research(question("fda"))
        self.assertEqual(len(calls), 1)
        sleep.assert_not_awaited()

    def test_network_classifier_excludes_deadlines_tls_and_validation_errors(self):
        wrapped_certificate = httpx.ConnectError("offline")
        wrapped_certificate.__cause__ = ssl.SSLCertVerificationError()
        for error in (TimeoutError(), ConnectionResetError(), requests.Timeout(), requests.ConnectionError(),
                      httpx.ConnectError("offline"), httpx.ReadTimeout("offline")):
            self.assertTrue(transient_research_error(error), type(error))
        for error in (BlockingDeadlineExceeded(), ResearchDeadlineExceeded(), ResearchValidationError(),
                      ssl.SSLCertVerificationError(), requests.exceptions.SSLError(),
                      urllib.error.URLError(ssl.SSLCertVerificationError()), wrapped_certificate, ValueError()):
            self.assertFalse(transient_research_error(error), type(error))


class ResearchDeadlineTests(RuntimeTestHelpers, OfflineTestCase):
    async def waiting_retry(self, *, pipeline=False):
        calls = self.agent(error=http_error(429))
        observed = self.timeout_probe()
        entered = asyncio.Event()

        async def sleep(delay):
            entered.set()
            await asyncio.Event().wait()

        self.start_patch(patch("research_safety.asyncio.sleep", sleep))
        bot = self.bot(publish_reports_to_metaculus=pipeline)
        bot.set_llm("perplexity-agent", "researcher")
        task = asyncio.create_task(bot.forecast_questions([question("japanese_diet")], return_exceptions=True)
                                   if pipeline else bot.run_research(question("fda")))
        await entered.wait()
        return bot, task, calls, observed

    async def test_deadline_during_backoff_stops_without_a_second_request(self):
        bot, task, calls, observed = await self.waiting_retry()
        # Expire the phase guard, not the completed attempt's inner IO deadline.
        next(guard for seconds, guard in observed if seconds == bot.runtime_limits.agent_seconds).reschedule(
            asyncio.get_running_loop().time())
        with self.assertRaises(TimeoutError):
            await task
        self.assertEqual(len(calls), 1)

    async def test_question_deadline_during_retry_cannot_publish(self):
        bot, task, calls, observed = await self.waiting_retry(pipeline=True)
        self.expire(observed, bot.runtime_limits.question_seconds)
        result = await task
        self.assertIsInstance(result[0], Exception)
        self.assertEqual(len(calls), 1)
        self.assertFalse(bot.metaculus_client.forecasts)
        self.assertFalse(bot.metaculus_client.comments)
        self.assertEqual(bot._runtime_state().workers, 0)

    async def test_cancelled_retry_never_starts_a_late_request(self):
        _, task, calls, _ = await self.waiting_retry()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(calls), 1)


class ModelResearchRetryTests(OfflineTestCase):
    def sdk_researcher(self, outcomes):
        # Restore the actual locked SDK invoke body, with only its direct model
        # boundary replaced locally. All HTTP/socket guards remain installed.
        self.start_patch(patch.object(app.GeneralLlm, "invoke", safety.sdk_model_invoke))
        direct = self.start_patch(patch.object(app.GeneralLlm, "_mockable_direct_call_to_model", new=AsyncMock(side_effect=outcomes)))
        self.start_patch(patch("research_safety.random.uniform", return_value=0))
        sleep = self.start_patch(patch("research_safety.asyncio.sleep", new=AsyncMock()))
        bot = self.bot()
        bot.set_llm(app.GeneralLlm(model="openrouter/openai/gpt-4o-mini", timeout=60, allowed_tries=2), "researcher")
        return bot, direct, sleep

    async def test_real_sdk_researcher_does_not_retry_401_or_403(self):
        class ApiError(Exception):
            def __init__(self, code):
                self.status_code = code

        for code in (401, 403):
            with self.subTest(code=code):
                bot, direct, sleep = self.sdk_researcher([ApiError(code)])
                before = bot.make_llm_dict()
                with self.assertRaises(ApiError):
                    await bot.run_research(question("fda"))
                with self.assertRaises(ResearchAccessDenied):
                    await bot.run_research(question("fda"))
                self.assertEqual(direct.await_count, 1)
                sleep.assert_not_awaited()
                self.assertEqual(bot.make_llm_dict(), before)

    async def test_real_sdk_403_prevents_an_entirely_unresearched_publication(self):
        class ApiError(Exception):
            status_code = 403

        bot, direct, _ = self.sdk_researcher([ApiError()])
        bot.publish_reports_to_metaculus = True
        result = await bot.forecast_questions([question("japanese_diet")], return_exceptions=True)
        self.assertIsInstance(result[0], Exception)
        self.assertEqual(direct.await_count, 1)
        self.assertFalse(bot.metaculus_client.forecasts)
        self.assertFalse(bot.metaculus_client.comments)

    async def test_real_sdk_transient_retry_respects_existing_allowed_tries(self):
        class ApiError(Exception):
            status_code = 500

        bot, direct, sleep = self.sdk_researcher([ApiError(), SimpleNamespace(data="Valid local research")])
        with self.assertLogs("main", level="WARNING") as logs:
            self.assertEqual(await bot.run_research(question("fda")), "Valid local research")
        self.assertTrue(any("completion status unavailable" in line for line in logs.output))
        self.assertEqual(direct.await_count, 2)
        sleep.assert_awaited_once_with(2)
        self.assertEqual(bot._llms["researcher"].allowed_tries, 2)
        self.assertEqual(bot.get_llm("researcher", "llm").to_dict(), bot._llms["researcher"].to_dict())

    async def test_real_sdk_retries_do_not_multiply_after_exhaustion(self):
        class ApiError(Exception):
            status_code = 429

        bot, direct, sleep = self.sdk_researcher([ApiError(), ApiError()])
        with self.assertRaises(ApiError):
            await bot.run_research(question("fda"))
        self.assertEqual(direct.await_count, 2)
        sleep.assert_awaited_once()
