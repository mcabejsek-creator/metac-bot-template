import urllib.error

from .support import OfflineTestCase, agent_response, app, question


class ResearchTests(OfflineTestCase):
    def researcher(self):
        bot = self.bot()
        bot.set_llm("perplexity-agent", "researcher")
        return bot

    async def test_two_agent_passes_and_transport_timeout(self):
        calls = self.agent()
        research = await self.researcher().run_research(question("fda"))
        self.assertEqual(research, "Verified synthetic research.")
        self.assertEqual(len(calls), 2)
        self.assertEqual([c["timeout"] for c in calls], [180, 180])
        self.assertEqual([c["payload"]["preset"] for c in calls], ["fast", "fast"])
        self.assertIn("Synthetic research.", calls[1]["payload"]["input"])

    async def test_paid_credit_403_fails_fast_and_does_not_publish(self):
        error = urllib.error.HTTPError("https://example.invalid", 403, "Agent API requires paid credits", {}, None)
        calls = self.agent(error=error)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            await self.researcher().run_research(question("japanese_diet"))
        self.assertIs(caught.exception, error)
        self.assertEqual(len(calls), 1)

    async def test_research_exception_prevents_any_publication(self):
        self.agent(error=urllib.error.HTTPError("https://example.invalid", 403, "Paid credits required", {}, None))
        bot = self.researcher()
        bot.publish_reports_to_metaculus = True  # LocalMetaculus sink only
        results = await bot.forecast_questions([question("japanese_diet")], return_exceptions=True)
        self.assertIsInstance(results[0], Exception)
        self.assertEqual(bot.metaculus_client.forecasts, [])

    async def test_failed_agent_response_is_rejected(self):
        self.agent([{"status": "failed", "output": [], "error": {"message": "Synthetic failure"}}, agent_response("Replacement text")])
        with self.assertRaises(ValueError):
            await self.researcher().run_research(question("fda"))

    async def test_empty_fact_check_is_not_a_publishable_research_report(self):
        self.agent([agent_response("Initial facts"), agent_response("")])
        self.model()
        bot = self.researcher()
        # Direct boundary test: future validation should reject unusable research.
        with self.assertRaises(ValueError):
            await bot.run_research(question("fda"))

    async def test_transient_429_has_bounded_retry(self):
        await self.check_transient_retry(urllib.error.HTTPError("https://example.invalid", 429, "Rate limit", {"Retry-After": "0"}, None))

    async def test_transient_503_has_bounded_retry(self):
        await self.check_transient_retry(urllib.error.HTTPError("https://example.invalid", 503, "Unavailable", {}, None))

    async def test_transport_timeout_has_bounded_retry(self):
        await self.check_transient_retry(TimeoutError("Synthetic read timeout"))

    async def check_transient_retry(self, error):
        from unittest.mock import AsyncMock, patch
        calls = self.agent(error=error)
        # Backoff is simulated too; future retry implementations must not make
        # offline tests sleep in real time.
        with patch("time.sleep"), patch("asyncio.sleep", new=AsyncMock()):
            try:
                await self.researcher().run_research(question("fda"))
            except type(error):
                pass
        self.assertTrue(2 <= len(calls) <= 4,
                           f"Expected bounded transient retry, observed {len(calls)} attempt(s)")

    async def test_fda_options_and_full_criteria_reach_initial_research(self):
        calls = self.agent()
        q = question("fda")
        await self.researcher().run_research(q)
        prompt = calls[0]["payload"]["input"]
        for text in [*q.options, q.resolution_criteria, q.fine_print, q.background_info]:
            self.assertIn(text, prompt)

    async def test_fact_check_receives_explicit_options(self):
        calls = self.agent()  # Initial fake response deliberately omits option labels.
        # Options can appear incidentally in prose criteria. Remove that
        # duplication so this checks explicit option context, not a false match.
        q = question("fda").model_copy(update={
            "resolution_criteria": "Apply the exact regulatory outcome within the specified interval."
        })
        await self.researcher().run_research(q)
        self.assertTrue(all(option in calls[1]["payload"]["input"] for option in q.options),
                           "Fact-check lacks explicit FDA answer options")

    async def test_fact_check_receives_fine_print(self):
        calls = self.agent()
        q = question("fda")
        await self.researcher().run_research(q)
        self.assertTrue(q.fine_print in calls[1]["payload"]["input"],
                           "Fact-check lacks the binding fine print")

    async def test_cbo_corrected_year_value_reaches_forecast(self):
        corrected = "FY2028 projected outlays: 8.151 trillion USD; FY2027: 7.772 trillion USD."
        calls = self.agent([agent_response("Incorrect pairing FY2028: 7.772 trillion USD"), agent_response(corrected)])
        q = question("cbo")
        bot = self.researcher()
        research = await bot.run_research(q)
        self.assertEqual(research, corrected)
        self.assertIn(q.background_info, calls[0]["payload"]["input"])
        self.assertIn("FY2028: 7.772", calls[1]["payload"]["input"])
        from unittest.mock import AsyncMock, patch
        probe = AsyncMock(return_value=None)
        with patch.object(bot, "_numeric_prompt_to_forecast", probe):
            await bot._run_forecast_on_numeric(q, research)
        self.assertIn(corrected, probe.await_args.args[1])
