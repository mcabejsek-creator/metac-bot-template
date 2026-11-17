"""Prospective comparison policy; no network, credentials or publication here.

Compare the deployed GPT-4.1 aggregate with ONE blind GPT-5.6 Sol response on
identical, frozen evidence. A smoke test is not forecasting accuracy evidence.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math

EXPERIMENT_ID = "sol-shadow-20261117-v1"
MODEL = "gpt-5.6-sol"
REASONING_EFFORT = "high"
MAX_ATTEMPTS = 20
MAX_PROMPT_BYTES = 24_000
MAX_OUTPUT_TOKENS = 6_000
RESERVE_PER_ATTEMPT_USD = Decimal("0.24")
TOTAL_RESERVATION_USD = MAX_ATTEMPTS * RESERVE_PER_ATTEMPT_USD
# Pricing is promotional through November 30; do not silently use later rates.
ENDS_AT = datetime(2026, 11, 30, tzinfo=timezone.utc)
MAX_CAPTURE_AGE_SECONDS = 900
INPUT_USD_PER_MILLION = Decimal("4")
OUTPUT_USD_PER_MILLION = Decimal("20")

class ShadowRejected(ValueError):
    """Safe fixed diagnostic; never interpolate provider data or credentials."""


def eligible_time(captured_at: datetime, closes_at: datetime, now: datetime) -> bool:
    dates = (captured_at, closes_at, now)
    if any(not isinstance(x, datetime) or x.tzinfo is None for x in dates):
        return False
    return (datetime(2026, 11, 17, 22, tzinfo=timezone.utc) <= captured_at <= now < ENDS_AT
            and (now - captured_at).total_seconds() <= MAX_CAPTURE_AGE_SECONDS
            and now < closes_at)


def valid_options(options: list[str]) -> list[str]:
    if (not isinstance(options, list) or not 2 <= len(options) <= 100
            or any(not isinstance(x, str) or not x or len(x) > 1000 for x in options)
            or len(set(options)) != len(options)):
        raise ShadowRejected("invalid_options")
    return options


def request_for(prompt: str, options: list[str]) -> dict:
    valid_options(options)
    if not isinstance(prompt, str) or not prompt.strip():
        raise ShadowRejected("empty_prompt")
    # Only the frozen baseline INPUT is passed; never its answer or rationale.
    suffix = ("\nFor this blind comparison, finish with a JSON object containing "
              "only 'probabilities' (one number from 0 to 1 for each option in "
              "the following order) and 'rationale' (a brief evidence-based "
              "explanation). Do not add new sources or claim knowledge beyond "
              "the frozen evidence. Option order: " + json.dumps(options, ensure_ascii=False))
    text = prompt + suffix
    if len(text.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ShadowRejected("prompt_too_large_no_truncation")
    return {"model": MODEL, "input": text, "reasoning": {"effort": REASONING_EFFORT},
            "max_output_tokens": MAX_OUTPUT_TOKENS, "store": False, "tools": [],
            "text": {"format": {"type": "json_schema", "name": "blind_forecast", "strict": True,
                "schema": {"type": "object", "properties": {
                    "probabilities": {"type": "array", "items": {"type": "number"}},
                    "rationale": {"type": "string"}},
                    "required": ["probabilities", "rationale"], "additionalProperties": False}}}}


def probabilities(values: list[float], option_count: int) -> list[float]:
    if (type(option_count) is not int or not 2 <= option_count <= 100
            or not isinstance(values, list) or len(values) != option_count
            or any(type(x) not in (int, float) or not math.isfinite(x) or not 0 <= x <= 1 for x in values)
            or abs(sum(values) - 1) > 1e-6):
        raise ShadowRejected("invalid_distribution")
    return [float(x) for x in values]


def payload_digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def estimated_cost(input_tokens: int, output_tokens: int) -> Decimal:
    if (type(input_tokens) is not int or type(output_tokens) is not int
            or input_tokens < 0 or not 0 <= output_tokens <= MAX_OUTPUT_TOKENS):
        raise ShadowRejected("invalid_usage")
    return (input_tokens * INPUT_USD_PER_MILLION + output_tokens * OUTPUT_USD_PER_MILLION) / Decimal(1_000_000)


def score_pair(baseline: list[float], candidate: list[float], outcome: int) -> dict:
    """Diagnostic proper scores, NOT official Metaculus peer score."""
    baseline = probabilities(baseline, len(baseline))
    candidate = probabilities(candidate, len(baseline))
    if type(outcome) is not int or not 0 <= outcome < len(baseline):
        raise ShadowRejected("invalid_outcome")
    def score(p):
        # Divide multiclass squared error by 2; binary reduces to (p-y)^2.
        return {"brier": sum((v - int(i == outcome)) ** 2 for i, v in enumerate(p)) / 2,
                "logloss": -math.log(max(p[outcome], 1e-15))}
    return {"baseline": score(baseline), "candidate": score(candidate)}
