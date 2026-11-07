"""Deterministic clock, fixtures, model text and wholly local publication sink."""

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from . import safety

import main as app
from forecasting_tools import BinaryQuestion, DateQuestion, MultipleChoiceQuestion, NumericQuestion
from publication import PublicationStore

safety.guard_models()

NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "historical_cases.json").read_text())


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


class KnownBugAssertion(AssertionError):
    pass


def known_bug(issue):
    def annotate(function):
        function.known_bug = issue
        return function
    return annotate


def question(case_id):
    data = dict(FIXTURES[case_id]["question"])
    kind = data.pop("kind")
    cls = {"binary": BinaryQuestion, "numeric": NumericQuestion,
           "multiple_choice": MultipleChoiceQuestion, "date": DateQuestion}[kind]
    return cls.model_validate(data)


def agent_response(text):
    return {"status": "completed", "output": [{"type": "message", "content": [
        {"type": "output_text", "text": text}
    ]}]}


class JsonResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        if isinstance(self.body, bytes):
            return self.body
        return json.dumps(self.body).encode()


class LocalMetaculus:
    """In-memory sink, never a real transport or a fake credential binding."""

    publication_single_attempt = True

    def __init__(self, fail_comment=False):
        self.forecasts = []
        self.comments = []
        self.fail_comment = fail_comment

    def post_binary_question_prediction(self, question_id, prediction):
        self.forecasts.append((question_id, prediction))

    def post_numeric_question_prediction(self, question_id, cdf):
        self.forecasts.append((question_id, list(cdf)))

    def post_multiple_choice_question_prediction(self, question_id, probabilities):
        self.forecasts.append((question_id, dict(probabilities)))

    def post_question_comment(self, post_id, text, **kwargs):
        if self.fail_comment:
            raise RuntimeError("Simulated comment failure after forecast write")
        self.comments.append((post_id, text))


class OfflineTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # The managed offline sandbox may reject even AF_UNIX self-pipe sends
        # from worker threads. Poll the test loop's ready queue without sockets;
        # production uses asyncio's normal thread-safe wakeup, unchanged.
        loop = asyncio.get_running_loop()
        handle = None

        def tick():
            nonlocal handle
            handle = loop.call_later(.001, tick)

        tick()
        self.addCleanup(lambda: handle.cancel())

    def setUp(self):
        # Each fixture starts a new synthetic run. Tests of actual reruns set
        # attempt 2+ explicitly; an outer CI attempt must not relabel fresh DBs.
        self.start_patch(patch.dict("os.environ", {"GITHUB_RUN_ATTEMPT": "1"}))
        self.start_patch(patch.object(app, "datetime", FrozenDateTime))
        self.start_patch(patch("question_context.utc_now", return_value=NOW))
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.publication_store = PublicationStore(Path(temporary.name) / "journal.sqlite3")

    def start_patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def require_fixed(self, condition, message):
        if not condition:
            raise KnownBugAssertion(message)

    def accelerate_parser_retries(self):
        original = asyncio.sleep

        async def fast(delay, *args, **kwargs):
            return await original(0 if delay == 1 else delay, *args, **kwargs)

        self.start_patch(patch.object(asyncio, "sleep", fast))

    def bot(self, **overrides):
        config = dict(
            research_reports_per_question=2,
            predictions_per_research_report=3,
            enable_summarize_research=False,
            publish_reports_to_metaculus=False,
            folder_to_save_reports_to=None,
            skip_previously_forecasted_questions=True,
            llms={"default": "openrouter/openai/gpt-4o", "researcher": "no_research",
                  "parser": "openrouter/openai/gpt-4o-mini", "summarizer": "openrouter/openai/gpt-4o-mini"},
            metaculus_client=LocalMetaculus(),
        )
        config.update(overrides)
        bot = app.SummerTemplateBot2026(**config)
        bot.publication_store = self.publication_store
        bot.publication_scope = "offline-test-bot"
        return bot

    def model(self, parser_json='{"prediction_in_decimal": 0.65}'):
        calls = []

        async def invoke(model, prompt, *args, **kwargs):
            calls.append(str(prompt))
            await asyncio.sleep(0)
            if "You are a data analyst" in str(prompt):
                return parser_json
            if "Please summarize" in str(prompt):
                return "Synthetic summary."
            return "Synthetic reasoning. Probability of YES: 65%"

        self.start_patch(patch.object(app.GeneralLlm, "invoke", invoke))
        return calls

    def agent(self, bodies=None, error=None):
        """Replace both HTTP and worker scheduling; no real threads or requests."""
        import os
        import urllib.request
        calls = []
        bodies = list([agent_response("Synthetic research."), agent_response("Verified synthetic research.")]
                      if bodies is None else bodies)

        def urlopen(request, **kwargs):
            calls.append({"payload": json.loads(request.data), **kwargs})
            if error:
                raise error
            if not bodies:
                raise AssertionError("Unexpected extra Agent request")
            body = bodies.pop(0)
            if isinstance(body, BaseException):
                raise body
            return JsonResponse(body)

        async def inline_worker(function, *args, on_finished, **kwargs):
            try:
                return function(*args, **kwargs)
            finally:
                on_finished()

        self.start_patch(patch.dict(os.environ, {"PERPLEXITY_API_KEY": "TEST_ONLY_NOT_A_REAL_CREDENTIAL"}))
        self.start_patch(patch.object(urllib.request, "urlopen", urlopen))
        self.start_patch(patch("runtime_limits.daemon_call", inline_worker))
        return calls
