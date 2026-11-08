"""Validate research and retry research-only requests within one fixed deadline.

Never used for Metaculus publication or GitHub journal writes. Retrying a research
POST can repeat provider work/billing after a lost reply; attempts stay bounded.
"""

import asyncio
import copy
import errno
import http.client
import json
import logging
import math
import os
import random
import socket
import ssl
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime

from runtime_limits import BlockingDeadlineExceeded, check_blocking_cancelled

logger = logging.getLogger(__name__)
AGENT_URL = "https://api.perplexity.ai/v1/agent"
AGENT_ATTEMPTS = 3


class ResearchValidationError(ValueError):
    """An enabled researcher returned no usable completed report."""


class ResearchDeadlineExceeded(TimeoutError):
    """The research retry budget, not a completed socket attempt, expired."""


class ResearchAccessDenied(PermissionError):
    def __init__(self, status):
        self.status_code = status
        super().__init__(f"Research access already denied (HTTP {status}) in this bot instance")


def validate_research_text(text):
    if not isinstance(text, str) or not text.strip():
        raise ResearchValidationError("Enabled researcher returned empty or non-text research")
    return text  # Keep the original text/whitespace used by forecasting prompts.


def parse_agent_response(data):
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise ResearchValidationError("Agent research is missing a completed status")
    if data.get("error") is not None or data.get("incomplete_details"):
        raise ResearchValidationError("Agent research reports an error or incomplete output")
    output = data.get("output")
    if not isinstance(output, list) or not output:
        raise ResearchValidationError("Agent research has no output items")
    answer_parts, sources = [], []
    for item in [data, *output]:
        if not isinstance(item, dict):
            raise ResearchValidationError("Agent research contains malformed output items")
        if (item.get("status") not in (None, "completed") or item.get("error") is not None
                or item.get("incomplete_details") or item.get("finish_reason") in ("length", "max_tokens")
                or item.get("type") in ("error", "refusal", "incomplete")):
            raise ResearchValidationError("Agent research contains failed or truncated output")
    for item in output:
        if not isinstance(item.get("type"), str):
            raise ResearchValidationError("Agent research output type is missing")
        if item["type"] == "message":
            content = item.get("content")
            if not isinstance(content, list) or not content:
                raise ResearchValidationError("Agent research message has no content")
            for part in content:
                if (not isinstance(part, dict) or part.get("type") != "output_text"
                        or not isinstance(part.get("text"), str)
                        or part.get("finish_reason") in ("length", "max_tokens")
                        or part.get("status") not in (None, "completed") or part.get("error") is not None
                        or part.get("incomplete_details")):
                    raise ResearchValidationError("Agent research message is refused, truncated or malformed")
                answer_parts.append(part["text"])
        elif item["type"] == "search_results":
            results = item.get("results", [])
            if not isinstance(results, list):
                raise ResearchValidationError("Agent research sources are malformed")
            for result in results:
                if not isinstance(result, dict):
                    raise ResearchValidationError("Agent research source is malformed")
                title, url = result.get("title", ""), result.get("url", "")
                if not isinstance(title, str) or not isinstance(url, str):
                    raise ResearchValidationError("Agent research source fields are malformed")
                if url:
                    sources.append(f"- {title}: {url}")
    # Sources alone used to make an otherwise empty response look successful.
    text = validate_research_text("\n".join(answer_parts))
    return text + ("\n\nSources:\n" + "\n".join(sources) if sources else "")


def http_status(error):
    code = error.code if isinstance(error, urllib.error.HTTPError) else getattr(error, "status_code", None)
    if code is None:
        code = getattr(getattr(error, "response", None), "status_code", None)
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def transient_research_error(error):
    if isinstance(error, (BlockingDeadlineExceeded, ResearchDeadlineExceeded)):
        return False
    code = http_status(error)
    if code is not None:
        return code in (408, 429) or 500 <= code <= 599
    pending, seen = [error], set()
    while pending:
        cause = pending.pop()
        if id(cause) in seen:
            continue
        seen.add(id(cause))
        if isinstance(cause, ssl.SSLCertVerificationError):
            return False
        for field in ("reason", "__cause__", "__context__"):
            nested = getattr(cause, field, None)
            if isinstance(nested, BaseException):
                pending.append(nested)
    if isinstance(error, urllib.error.URLError):
        return not isinstance(error.reason, ssl.SSLCertVerificationError)
    if isinstance(error, (TimeoutError, ConnectionError, socket.gaierror, http.client.IncompleteRead)):
        return True
    if isinstance(error, OSError) and error.errno in {errno.ECONNABORTED, errno.ECONNREFUSED, errno.ECONNRESET,
                                                   errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EPIPE, errno.ETIMEDOUT}:
        return True
    # Public provider exception types, not matching arbitrary error-message text.
    import httpx
    import requests
    from openai import APIConnectionError
    if isinstance(error, requests.exceptions.SSLError):
        return False
    return isinstance(error, (httpx.NetworkError, httpx.TimeoutException,
                              requests.exceptions.ConnectionError, requests.exceptions.Timeout, APIConnectionError))


def retry_after_seconds(error):
    headers = getattr(error, "headers", None)
    if headers is None:
        headers = getattr(getattr(error, "response", None), "headers", None)
    if headers is None:
        return None
    value = headers.get("Retry-After") or headers.get("retry-after")
    if not isinstance(value, str):
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                return None
            seconds = date.timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0, seconds) if math.isfinite(seconds) and seconds >= 0 else None


def denial_cache(bot):
    if not hasattr(bot, "_research_denials"):
        bot._research_denials = {}
    return bot._research_denials


async def retry_research(operation, *, attempts, budget, phase, denied, provider):
    """Retry only completed transient attempts; a detached IO timeout is final."""
    if provider in denied:
        raise ResearchAccessDenied(denied[provider])
    deadline_at = time.monotonic() + budget
    async with asyncio.timeout(budget):
        for attempt in range(1, attempts + 1):
            if provider in denied:
                raise ResearchAccessDenied(denied[provider])
            try:
                return await operation(deadline_at)
            except Exception as error:
                code = http_status(error)
                after = retry_after_seconds(error)
                if isinstance(error, urllib.error.HTTPError):
                    error.close()
                if code in (401, 403):
                    denied[provider] = code
                if attempt == attempts or not transient_research_error(error):
                    logger.error("Research %s failed on attempt %s (HTTP %s, %s)",
                                 phase, attempt, code, type(error).__name__)
                    raise
                delay = min(30, 2 ** attempt) + random.uniform(0, 1)
                if after is not None:
                    delay = max(delay, after)
                if delay >= deadline_at - time.monotonic():
                    raise ResearchDeadlineExceeded("Research retry delay exceeds the remaining deadline") from error
                logger.warning("Research %s transient failure (HTTP %s, %s); retry %s/%s after %.2fs",
                               phase, code, type(error).__name__, attempt + 1, attempts, delay)
                await asyncio.sleep(delay)
                if time.monotonic() >= deadline_at:
                    raise ResearchDeadlineExceeded("Research deadline expired during backoff")


def call_agent_once(prompt, deadline_at, guard=None):
    check_blocking_cancelled()
    remaining = deadline_at - time.monotonic()
    if remaining <= 0:
        raise ResearchDeadlineExceeded("Agent deadline expired before sending")
    if guard is not None:
        guard.begin_attempt()
    try:
        request = urllib.request.Request(AGENT_URL,
            data=json.dumps({"preset": "fast", "input": prompt}).encode("utf-8"),
            headers={"Authorization": f"Bearer {os.environ['PERPLEXITY_API_KEY']}", "Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(request, timeout=min(180, remaining)) as response:
            try:
                data = json.loads(response.read().decode("utf-8"))
            except (ValueError, UnicodeError) as error:
                raise ResearchValidationError("Agent returned invalid JSON") from error
        # Before returning any research, enforce provider-reported cost.
        if guard is not None:
            guard.record_response(data)
        return parse_agent_response(data)
    except BaseException:
        # A transmitted request may have been billed despite a lost response.
        if guard is not None:
            guard.abandon_unknown_response()
        raise


async def agent_research(bot, prompt, *, phase):
    budget = bot.runtime_limits.agent_seconds
    guard = getattr(bot, "_automatic_perplexity_budget", None)

    async def attempt(deadline_at):
        if guard is not None:
            # No retries: a timed-out POST can already have been billed.
            from perplexity_run_budget import PerplexityAutoRunBudget
            if not isinstance(guard, PerplexityAutoRunBudget):
                raise ResearchValidationError("Invalid automatic Perplexity guard")
            return await bot._run_blocking(call_agent_once, prompt, deadline_at, guard, _timeout=budget)
        return await bot._run_blocking(call_agent_once, prompt, deadline_at, _timeout=budget)

    return await retry_research(attempt, attempts=1 if guard is not None else AGENT_ATTEMPTS, budget=budget,
                                phase=phase, denied=denial_cache(bot), provider="agent")


def research_model_with_safe_retry(model, bot):
    """Replace only the locked SDK researcher's indiscriminate retry decorator.

    All model parameters, transport settings and allowed_tries stay intact. The
    runtime's outer operation permit and model deadline still wrap invoke().
    """
    configured = copy.copy(model)
    method = getattr(configured, "_invoke_with_request_cost_time_and_token_limits_and_retry", None)
    single = getattr(getattr(method, "__func__", None), "__wrapped__", None)
    if single is None:
        raise ResearchValidationError("SDK research retry contract changed; no call authorized")

    async def controlled(prompt):
        async def attempt(deadline_at):
            return await single(configured, prompt)

        return await retry_research(attempt, attempts=configured.allowed_tries,
            budget=bot.runtime_limits.model_seconds, phase="model", denied=denial_cache(bot),
            provider=("model", configured.model))

    configured._invoke_with_request_cost_time_and_token_limits_and_retry = controlled
    return configured
