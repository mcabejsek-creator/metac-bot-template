"""Durable, fail-closed publication; no assumed server-side idempotency support.

Cooperating workers share a persistent local journal or the GitHub state backend,
with the same scope. Actions require the GitHub backend rather than ephemeral
local storage. An ambiguous write is quarantined, never retried automatically,
because a client cannot guarantee exactly-once delivery after losing the server's
acknowledgement.
"""

import asyncio
import contextvars
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

import requests
from pydantic import BaseModel
from forecasting_tools import ConditionalQuestion, MetaculusClient
from forecasting_tools.data_models.data_organizer import DataOrganizer

from publication_journal import (
    PublicationRejected, PublicationStore, PublicationUncertain, Publisher, canonical, digest,
)
from runtime_limits import check_blocking_cancelled
import question_context
from question_context import effective_close, utc_date
from forecast_validation import restore_typed_question, validate_operation, validate_plan


def restore_report(report_type, saved):
    """Restore a previously validated snapshot without renormalizing its numbers.

    SDK Pydantic after-validators can normalize MC probabilities on every load.
    Validate structure/types first, then restore original numerical leaf values
    using typed model copies. Fresh forecasts still use the SDK unchanged; the
    journal's frozen wire plan remains authoritative for all outbound writes.
    """
    raw = json.loads(saved)
    validating = raw
    if raw.get("question", {}).get("question_type") == "conditional":
        validating = {**raw, "question": restore_typed_question(raw["question"])}
    validated = report_type.model_validate(validating)

    def restore(value, source):
        if isinstance(value, BaseModel):
            return value.model_copy(update={name: restore(getattr(value, name), source[name])
                                            for name in type(value).model_fields if name in source})
        if isinstance(value, list) and isinstance(source, list):
            if len(value) != len(source):
                raise ValueError("Stored publication changed shape during validation")
            return [restore(item, original) for item, original in zip(value, source)]
        if isinstance(value, dict) and isinstance(source, dict):
            return {name: restore(item, source[name]) if name in source else item for name, item in value.items()}
        if isinstance(value, float) and isinstance(source, (int, float)) and not isinstance(source, bool):
            return float(source)
        return value

    return restore(validated, raw)


class CapturingClient:
    """Use SDK report/validation/payload logic, capturing writes instead of sending.

    Read methods delegate to the original client. Every write requires an active
    per-coroutine capture; accidental use outside the publication coordinator fails.
    """

    post_binary_question_prediction = MetaculusClient.post_binary_question_prediction
    post_numeric_question_prediction = MetaculusClient.post_numeric_question_prediction
    post_multiple_choice_question_prediction = MetaculusClient.post_multiple_choice_question_prediction

    def __init__(self, client):
        if not isinstance(client, MetaculusClient) and not getattr(client, "publication_single_attempt", False):
            raise TypeError("Custom publication clients must explicitly promise single-attempt writes")
        self.client = client
        self._operations = contextvars.ContextVar("publication_operations", default=None)

    def __getattr__(self, name):
        return getattr(self.client, name)

    @contextmanager
    def capture(self):
        operations = []
        token = self._operations.set(operations)
        try:
            yield operations
        finally:
            self._operations.reset(token)

    def _record(self, operation):
        operations = self._operations.get()
        if operations is None:
            raise RuntimeError("Write attempted outside a publication capture")
        operations.append(operation)

    def _post_question_prediction(self, question_id, forecast_payload):
        self._record({"kind": "forecast", "question_id": question_id, "payload": forecast_payload})

    def post_question_comment(self, post_id, comment_text, is_private=True, included_forecast=True):
        self._record({"kind": "comment", "post_id": post_id, "text": comment_text,
                      "is_private": is_private, "included_forecast": included_forecast})


class SingleAttemptWriter:
    """Write precisely once per journal claim, bypassing SDK's POST retry wrappers."""

    # Conservative HTTP rejection list. Timeouts, redirects, 408 and all 5xx remain
    # ambiguous. No automatic retry even for an explicit rejection in this run.
    REJECTED = frozenset({400, 401, 403, 404, 405, 413, 415, 422, 429})

    def __init__(self, client):
        if not isinstance(client, MetaculusClient) and not getattr(client, "publication_single_attempt", False):
            raise TypeError("Custom publication clients must explicitly promise single-attempt writes")
        self.client = client

    def __call__(self, operation):
        validate_operation(operation)
        if operation["kind"] not in ("forecast", "comment"):
            raise ValueError("Unknown publication operation")
        if not isinstance(self.client, MetaculusClient):
            # An injected local/custom transport must itself be single-attempt.
            # Production uses the explicit requests path below, not SDK retries.
            return self._custom(operation)
        if operation["kind"] == "forecast":
            endpoint = "/questions/forecast/"
            payload = [{"question": operation["question_id"], "source": "api", **operation["payload"]}]
        else:
            endpoint = "/comments/create/"
            payload = {"on_post": operation["post_id"], "text": operation["text"],
                       "is_private": operation["is_private"], "included_forecast": operation["included_forecast"]}
        # Prepare auth/throttling before attempting HTTP; a failure here is also
        # conservatively quarantined by Publisher, rather than risking a resend.
        self.client._sleep_between_requests()
        check_blocking_cancelled()
        response = requests.post(self.client.base_url + endpoint, json=payload,
                                 **self.client._get_auth_headers(), timeout=self.client.timeout,
                                 allow_redirects=False)
        try:
            if response.status_code in (200, 201, 204):
                return
            if response.status_code in self.REJECTED:
                raise PublicationRejected(response.status_code)
            raise PublicationUncertain(f"Uncertain publication HTTP status {response.status_code}")
        finally:
            response.close()

    def _custom(self, operation):
        if operation["kind"] == "comment":
            return self.client.post_question_comment(operation["post_id"], operation["text"],
                                                    is_private=operation["is_private"], included_forecast=operation["included_forecast"])
        payload = operation["payload"]
        if "probability_yes" in payload:
            return self.client.post_binary_question_prediction(operation["question_id"], payload["probability_yes"])
        if "continuous_cdf" in payload:
            return self.client.post_numeric_question_prediction(operation["question_id"], payload["continuous_cdf"])
        return self.client.post_multiple_choice_question_prediction(operation["question_id"], payload["probability_yes_per_category"])


class SafePublicationMixin:
    """Leave forecasting with ForecastBot; coordinate only its publication boundary."""

    def _publication_runtime(self):
        if not hasattr(self, "_publication_publisher"):
            original = self.metaculus_client
            default_db = Path(__file__).resolve().parent / ".cache" / "metaculus-publication.sqlite3"
            injected = getattr(self, "publication_store", None)
            backend = os.getenv("METACULUS_PUBLICATION_BACKEND", "local")
            if injected is not None:
                self._publication_store = injected
            elif backend == "github":
                from github_publication import configured_store
                self._publication_store = configured_store()
            elif backend != "local" or os.getenv("GITHUB_ACTIONS") == "true":
                raise RuntimeError("GitHub Actions publishing requires the durable github backend; no local fallback")
            else:
                self._publication_store = PublicationStore(os.getenv("METACULUS_PUBLICATION_DB", str(default_db)))
            self._publication_scope = getattr(self, "publication_scope", None) or os.getenv(
                "METACULUS_PUBLICATION_SCOPE", "default-bot")
            self._publication_endpoint = getattr(original, "base_url", "local")
            # Explicit reforecasts remain possible: one intent per run, stable
            # across GitHub rerun attempts, never inferred from changing payloads.
            self._publication_intent = getattr(self, "publication_intent", None) or os.getenv(
                "METACULUS_PUBLICATION_INTENT") or os.getenv("GITHUB_RUN_ID") or uuid.uuid4().hex
            self.metaculus_client = CapturingClient(original)
            self._publication_publisher = Publisher(self._publication_store, SingleAttemptWriter(original))
        return self._publication_publisher

    def _publication_resource(self, question):
        if question.id_of_post is None or question.id_of_question is None:
            raise ValueError("Durable publication requires both question and post IDs")
        return digest(canonical([self._publication_scope, self._publication_endpoint,
                                 question.question_type, question.id_of_question, question.id_of_post]))

    def _publication_key(self, question):
        intent = "initial" if self.skip_previously_forecasted_questions else self._publication_intent
        return self._publication_resource(question) + "/" + digest(intent)

    async def forecast_questions(self, questions, return_exceptions=False):
        if self.publish_reports_to_metaculus:
            self._publication_runtime()
            preflight = getattr(self._publication_store, "preflight", None)
            if preflight is not None:
                await self._publication_call(preflight)
            # SDK would skip an already-forecasted question even if its comment
            # was rejected. Restore its frozen report instead of researching anew.
            restored = []
            for question in questions:
                pending = await self._publication_call(self._publication_store.pending, self._publication_resource(question))
                restored.append(question.model_copy(update={"already_forecasted": False}) if pending is not None else question)
            questions = restored
        return await super().forecast_questions(questions, return_exceptions)

    async def _publication_call(self, function, *args):
        runner = getattr(self, "_run_blocking", None)
        if runner is None:
            return function(*args)
        return await runner(function, *args)

    @staticmethod
    def _require_forecast_window_open(question):
        """Refuse NEW forecast POSTs after their known, snapshot-based close time.

        Do not use this guard for comment-only recovery of an ALREADY confirmed
        forecast: comments may need to be recovered after forecast closing.
        This is not a fresh server-state GET and cannot detect a newly moved
        deadline that is absent from the saved API question snapshot.
        """
        targets = (
            (question.question_yes, question.question_no)
            if isinstance(question, ConditionalQuestion) else (question,)
        )
        now = utc_date(question_context.utc_now())
        for target in targets:
            start = utc_date(target.open_time)
            close = effective_close(target)
            if start is None or close is None or not start <= now < close:
                raise PublicationUncertain(
                    "Question forecast window is no longer verifiably open; no new POST"
                )

    async def _drain_publication(self, publisher, key, question, *, comments_only=False):
        # One blocking action per await. Cancellation of a claim waiter can
        # NEVER continue into a POST in a detached worker. Cancellation during
        # POST/finish leaves durable sending (or a confirmed checkpoint), and no
        # subsequent step is started. The synchronous Publisher API stays valid.
        if type(comments_only) is not bool:
            raise ValueError("Comment-only recovery flag must be boolean")
        store = publisher.store
        steps = await self._publication_call(store.steps, key)
        if not steps:
            raise ValueError("Missing publication plan")
        if any(step["state"] in ("sending", "uncertain") for step in steps):
            raise PublicationUncertain(f"Publication {key}: in flight or uncertain; do not resend")
        if all(step["state"] == "confirmed" for step in steps):
            return
        report_type = DataOrganizer.get_report_type_for_question_type(type(question))
        saved = await self._publication_call(store.report, key)
        report = restore_report(report_type, saved)
        operations = [json.loads(step["operation"]) for step in steps]
        if any(step["operation_hash"] != digest(canonical(operation)) for step, operation in zip(steps, operations)):
            raise ValueError("Frozen publication operation hash changed; do not send")
        validate_plan(question, report, operations)
        if comments_only and (
            len(steps) != 2
            or operations[0]["kind"] != "forecast"
            or steps[0]["state"] != "confirmed"
            or operations[1]["kind"] != "comment"
        ):
            raise PublicationUncertain("Comment-only recovery requires one already confirmed forecast")
        for step in steps:
            if step["state"] == "confirmed":
                continue
            if comments_only and json.loads(step["operation"]).get("kind") != "comment":
                raise PublicationUncertain("Comment-only recovery cannot send a forecast")
            # Recheck right before claiming/sending a NEW forecast. If a long
            # model job outlived the question deadline, leave the frozen
            # journal plan in ready/rejected state without ever attempting POST.
            # A confirmed forecast's unfinished comment may still be recovered.
            if json.loads(step["operation"]).get("kind") == "forecast":
                self._require_forecast_window_open(question)
            if not await self._publication_call(store.claim, key, step["position"]):
                continue
            try:
                await self._publication_call(publisher.writer, json.loads(step["operation"]))
            except asyncio.CancelledError:
                raise
            except TimeoutError as error:
                # The IO worker may still be in POST. Its occupied permit must
                # not be reused to checkpoint/retry; sending already blocks it.
                raise PublicationUncertain(f"Publication {key}, step {step['position']}: timed out; do not resend") from error
            except PublicationRejected:
                await self._publication_call(store.finish, key, step["position"], "rejected")
                raise
            except BaseException as error:
                await self._publication_call(store.finish, key, step["position"], "uncertain")
                if not isinstance(error, Exception):
                    raise
                raise PublicationUncertain(f"Publication {key}, step {step['position']}: outcome uncertain; do not resend") from error
            else:
                await self._publication_call(store.finish, key, step["position"], "confirmed")

    async def _run_individual_question(self, question):
        if not self.publish_reports_to_metaculus:
            return await super()._run_individual_question(question)
        publisher = self._publication_runtime()
        requested_key = self._publication_key(question)
        skip_previous = self.skip_previously_forecasted_questions
        require_saved = False
        if not skip_previous and os.getenv("GITHUB_ACTIONS") == "true":
            try:
                attempt = int(os.environ["GITHUB_RUN_ATTEMPT"])
                if attempt < 1:
                    raise ValueError()
            except (KeyError, ValueError) as error:
                raise RuntimeError("Reforecasting in Actions requires a valid GITHUB_RUN_ATTEMPT") from error
            # A failed binding PUT may leave no record. If another run finishes
            # the old plan before this rerun, absence is not permission to turn
            # an unrecorded recovery attempt into a new model forecast.
            require_saved = attempt > 1
        key, saved, created = await self._publication_call(
            self._publication_store.select_for_run, requested_key, skip_previous, require_saved)
        if saved is None:
            with self.metaculus_client.capture() as operations:
                report = await super()._run_individual_question(question)
            validate_plan(question, report, operations)
            key, saved, created = await self._publication_call(self._publication_store.prepare_for_run,
                requested_key, report.model_dump_json(exclude_computed_fields=True, serialize_as_any=True), operations, skip_previous, require_saved)
        await self._drain_publication(publisher, key, question)
        if created:
            return report
        report_type = DataOrganizer.get_report_type_for_question_type(type(question))
        return restore_report(report_type, saved)
