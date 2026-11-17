"""One-shot direct OpenAI connectivity probe; no tournament data or publishing.

Only the first main push of this dedicated workflow is authorized. A single
model GET precedes at most one synthetic Responses POST. No retries, fallback,
redirects, proxy endpoints, SDK defaults, or production credentials are used.
The ~USD 0.03 estimate uses published standard pricing checked 2026-11-17;
it is not a provider-enforced dollar cap or proof of bonus billing attribution.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

REPOSITORY = "mcabejsek-creator/metac-bot-template"
WORKFLOW = ".github/workflows/openai_bonus_probe.yaml"
MODEL = "gpt-5.6-sol"
MODEL_URL = f"https://api.openai.com/v1/models/{MODEL}"
RESPONSE_URL = "https://api.openai.com/v1/responses"
MAX_OUTPUT_TOKENS = 512
MAX_RESPONSE_BYTES = 128_000
EXPIRES_AT = datetime(2026, 11, 20, tzinfo=timezone.utc)
PROMPT = (
    "Synthetic API connectivity test, not a live forecast. "
    "A bag contains 3 red balls and 7 blue balls. One ball is drawn uniformly. "
    'Return exactly {"probability":0.3} as JSON, with no explanation.'
)


class ProbeError(Exception):
    """Carries only a fixed nonsecret status code."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def authorized(env: dict[str, str], now: datetime) -> bool:
    return (
        env.get("GITHUB_REPOSITORY") == REPOSITORY
        and env.get("GITHUB_REF") == "refs/heads/main"
        and env.get("GITHUB_EVENT_NAME") == "push"
        and env.get("GITHUB_RUN_NUMBER") == "1"
        and env.get("GITHUB_RUN_ATTEMPT") == "1"
        and env.get("GITHUB_WORKFLOW_REF")
        == f"{REPOSITORY}/{WORKFLOW}@refs/heads/main"
        and now < EXPIRES_AT
    )


def payload() -> dict[str, Any]:
    return {
        "model": MODEL,
        "input": PROMPT,
        "reasoning": {"effort": "high"},
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "store": False,
        "service_tier": "default",
        "tools": [],
        "text": {"format": {"type": "json_schema", "name": "synthetic_probability",
                            "strict": True, "schema": {
                                "type": "object", "properties": {"probability": {"type": "number"}},
                                "required": ["probability"], "additionalProperties": False}}},
    }


def request_json(opener, key: str, method: str, url: str,
                 data: dict[str, Any] | None = None) -> dict[str, Any]:
    # Fixed destinations only; do not send a bearer token to a user-supplied URL.
    if (method, url) not in {("GET", MODEL_URL), ("POST", RESPONSE_URL)}:
        raise ProbeError("destination_denied")
    raw = json.dumps(data).encode("utf-8") if data is not None else None
    request = urllib.request.Request(url, data=raw, method=method,
                                     headers={"Authorization": f"Bearer {key}",
                                              "Content-Type": "application/json"})
    try:
        with opener.open(request, timeout=120) as response:
            if response.status != 200:
                raise ProbeError("unexpected_http_status")
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        # Never print server bodies or exception messages (they can echo inputs).
        raise ProbeError(f"http_{int(exc.code)}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ProbeError("transport_error_no_retry") from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise ProbeError("response_too_large_no_retry")
    try:
        result = json.loads(body)
    except (ValueError, UnicodeError):
        raise ProbeError("invalid_json_no_retry") from None
    if not isinstance(result, dict):
        raise ProbeError("invalid_response_shape")
    return result


def validate_completion(response: dict[str, Any]) -> dict[str, Any]:
    if response.get("status") != "completed":
        raise ProbeError("completion_not_completed_no_retry")
    response_model = response.get("model")
    if not isinstance(response_model, str) or not (
        response_model == MODEL or response_model.startswith(MODEL + "-")
    ):
        raise ProbeError("unexpected_response_model")
    texts = []
    for item in response.get("output", []):
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    texts.append(part.get("text", ""))
    try:
        answer = json.loads("".join(texts))
    except (ValueError, TypeError):
        raise ProbeError("synthetic_answer_invalid") from None
    if (not isinstance(answer, dict) or set(answer) != {"probability"}
            or type(answer["probability"]) not in (float, int)
            or abs(answer["probability"] - 0.3) > 1e-9):
        raise ProbeError("synthetic_answer_incorrect")
    usage = response.get("usage", {})
    counts = {}
    for name in ("input_tokens", "output_tokens"):
        value = usage.get(name)
        if type(value) is not int or value < 0:
            raise ProbeError("usage_missing_or_invalid")
        counts[name] = value
    if counts["output_tokens"] > MAX_OUTPUT_TOKENS:
        raise ProbeError("output_limit_not_respected")
    return {
        "synthetic_answer_correct": True,
        **counts,
        # All input charged at uncached rate; reasoning is part of output_tokens.
        "estimated_usd_at_2026_11_17_rates": round(
            (4 * counts["input_tokens"] + 20 * counts["output_tokens"]) / 1_000_000, 8),
    }


def run_probe(env: dict[str, str], *, now: datetime | None = None, opener=None) -> dict[str, Any]:
    report: dict[str, Any] = {"model": MODEL, "model_get_attempts": 0,
                              "response_post_attempts": 0, "publishes": 0,
                              "billing_attribution_verified": False}
    if not authorized(env, now or datetime.now(timezone.utc)):
        return {**report, "status": "not_authorized"}
    key = env.get("VRCHCAB_OPENAI_TEST_KEY", "").strip()
    if not key or any(char.isspace() for char in key):
        return {**report, "status": "missing_or_invalid_test_key"}
    # This is not a production client. It cannot fall back to another key.
    client = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    stage = "model_get"
    start = time.monotonic()
    try:
        report["model_get_attempts"] = 1
        model = request_json(client, key, "GET", MODEL_URL)
        if model.get("id") != MODEL:
            raise ProbeError("model_not_verified")
        report["model_access_verified"] = True
        stage = "synthetic_response"
        # Mark the attempted POST BEFORE transport; errors never cause a retry.
        report["response_post_attempts"] = 1
        response = request_json(client, key, "POST", RESPONSE_URL, payload())
        report.update(validate_completion(response))
        report["status"] = "success"
    except ProbeError as exc:
        report.update(status=str(exc), failed_stage=stage)
    except Exception:
        report.update(status="unexpected_error_no_retry", failed_stage=stage)
    report["elapsed_seconds"] = round(time.monotonic() - start, 3)
    return report


def main() -> int:
    report = run_probe(dict(os.environ))
    print("OPENAI_BONUS_PROBE " + json.dumps(report, sort_keys=True))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as target:
            target.write("## One-shot OpenAI connectivity test\n\n```json\n"
                         + json.dumps(report, indent=2) + "\n```\n"
                         "Synthetic connectivity only: not forecast accuracy and not a billing statement.\n")
    return 0 if report["status"] == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
