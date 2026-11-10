"""One bounded sponsored-model request on synthetic arithmetic; never forecast.

No Metaculus or Perplexity credentials are supplied by the workflow. This checks
the exact SDK/model integration, not forecasting accuracy, and never retries.
"""

import asyncio
import json
import math
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from automatic_model import AUTOMATIC_MODEL_ID, AUTOMATIC_MODEL_VERSION, build_automatic_model
from sponsored_credit_guard import verify_sponsored_openrouter_credit


PROMPT = """Synthetic integration check. There is no real-world event or web research.
A fair six-sided die is rolled once. Return ONLY one JSON object with two keys:
\"probability_even\": the probability of rolling an even number, as a number;
\"groups\": an object with probabilities for \"one_or_two\", \"three_or_four\",
and \"five_or_six\". The three groups are mutually exclusive and exhaustive.
Use numeric probabilities with at least six decimal places where needed.
Do not add Markdown, commentary or other keys."""


def validate_response(text):
    if not isinstance(text, str) or len(text) > 10000:
        raise ValueError("Synthetic model response missing or oversized")
    data = json.loads(text)
    if not isinstance(data, dict) or set(data) != {"probability_even", "groups"}:
        raise ValueError("Synthetic response schema differs")
    even, groups = data["probability_even"], data["groups"]
    if (type(even) not in (float, int) or not math.isfinite(even)
            or abs(even - 0.5) > 0.00001
            or not isinstance(groups, dict)
            or set(groups) != {"one_or_two", "three_or_four", "five_or_six"}
            or any(type(p) not in (float, int) or not math.isfinite(p)
                   or abs(p - 1 / 3) > 0.00001 for p in groups.values())):
        raise ValueError("Synthetic probability check failed")


async def run():
    verify_sponsored_openrouter_credit(
        os.environ.get("OPENROUTER_API_KEY"),
        expected_suffix=os.environ.get("METACULUS_SPONSORED_KEY_SUFFIX"),
    )
    started = time.monotonic()
    async with asyncio.timeout(200):
        response = await build_automatic_model().invoke(PROMPT)
    validate_response(response)
    print("AUTOMATIC_MODEL_SMOKE " + json.dumps({
        "status": "passed", "model": AUTOMATIC_MODEL_ID,
        "version": AUTOMATIC_MODEL_VERSION,
        "synthetic_model_requests": 1, "metaculus_requests": 0,
        "perplexity_requests": 0,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "forecast_accuracy_measured": False,
    }, sort_keys=True))


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except Exception as error:
        # No request or exception representation can reveal credentials.
        print("Synthetic model smoke failed: " + type(error).__name__ + "; no retry")
        raise SystemExit(1) from None
