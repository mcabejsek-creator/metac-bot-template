import json

from .support import OfflineTestCase, question


class MultipleChoiceValidationTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.accelerate_parser_retries()

    async def parse(self, options):
        self.model(json.dumps({"predicted_options": options}))
        return await self.bot()._multiple_choice_prompt_to_forecast(question("fda"), "Synthetic reasoning request")

    async def test_valid_fda_options_survive_real_parser_and_aggregation(self):
        from forecasting_tools import MultipleChoiceReport
        options = [{"option_name": name, "probability": probability} for name, probability in
                   zip(question("fda").options, (.3, .5, .2))]
        result = await self.parse(options)
        aggregate = await MultipleChoiceReport.aggregate_predictions([result.prediction_value], question("fda"))
        self.assertEqual(set(aggregate.to_dict()), set(question("fda").options))
        self.assertAlmostEqual(sum(aggregate.to_dict().values()), 1)

    async def test_duplicate_options_are_rejected(self):
        with self.assertRaises(ValueError):
            await self.parse([{"option_name": "FDA Approval", "probability": .5}] * 2)

    async def test_missing_option_is_rejected(self):
        with self.assertRaises(ValueError):
            await self.parse([{"option_name": name, "probability": .5} for name in question("fda").options[:2]])

    async def test_unknown_option_is_rejected(self):
        with self.assertRaises(ValueError):
            await self.parse([{"option_name": name, "probability": probability} for name, probability in
                              zip(["Unlisted outcome", *question("fda").options[1:]], (.3, .5, .2))])
