import json
import math

from .support import OfflineTestCase, question


class DistributionTests(OfflineTestCase):
    def assert_cdf(self, distribution):
        cdf = distribution.get_cdf()
        self.assertEqual(len(cdf), 201)
        self.assertTrue(all(math.isfinite(p.value) and math.isfinite(p.percentile) for p in cdf))
        self.assertTrue(all(0 <= p.percentile <= 1 for p in cdf))
        self.assertTrue(all(a.value < b.value and a.percentile <= b.percentile for a, b in zip(cdf, cdf[1:])))
        return cdf

    async def test_numeric_parser_produces_valid_closed_cdf(self):
        quantiles = [{"percentile": p, "value": v} for p, v in
                     zip((.1, .2, .4, .6, .8, .9), (10, 20, 40, 60, 80, 90))]
        calls = self.model(json.dumps(quantiles))
        report = await self.bot()._numeric_prompt_to_forecast(question("percentage"), "Local numeric fixture")
        cdf = self.assert_cdf(report.prediction_value)
        self.assertEqual((cdf[0].percentile, cdf[-1].percentile), (0, 1))
        self.assertEqual((cdf[0].value, cdf[-1].value), (0, 100))
        self.assertTrue(any("The units for the forecast are: %" in p for p in calls))

    async def test_date_parser_converts_explicit_utc_to_ordered_timestamps(self):
        dates = ("2026-12-01", "2027-01-01", "2027-03-01", "2027-05-01", "2027-07-01", "2027-09-01")
        quantiles = [{"percentile": p, "value": date + "T00:00:00Z"} for p, date in
                     zip((.1, .2, .4, .6, .8, .9), dates)]
        self.model(json.dumps(quantiles))
        report = await self.bot()._date_prompt_to_forecast(question("date"), "Local date fixture")
        self.assert_cdf(report.prediction_value)
        self.assertEqual(report.prediction_value.declared_percentiles[0].value, 1796083200)
