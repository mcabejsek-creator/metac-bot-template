"""Finish one frozen comment on an idle pass, without forecasting or paid work.

Only positively confirmed forecast receipts with a ready/explicitly rejected
comment qualify. In-flight and uncertain writes stay quarantined. Reading a
rotating slice of the existing registry also reaches already-closed questions;
no new index, journal reset, or assumption about an absent receipt is needed.
"""

import json

from forecasting_tools.data_models.data_organizer import DataOrganizer

from automatic_one_selection import ATOMIC_KINDS
from forecast_validation import restore_typed_question
from publication import restore_report
from publication_journal import PublicationUncertain
from question_context import utc_date, utc_now


MAX_RESOURCES_PER_PASS = 5
ROTATION_SECONDS = 15 * 60


def recovery_window(resources, *, now=None):
    """A bounded, time-rotating window; never repeatedly scan only old entries."""
    if not isinstance(resources, (set, frozenset)) or any(
        not isinstance(resource, str) or not resource for resource in resources
    ):
        raise ValueError("Comment recovery needs the existing authenticated registry")
    ordered = sorted(resources)
    if not ordered:
        return []
    moment = utc_date(utc_now() if now is None else now)
    start = (int(moment.timestamp()) // ROTATION_SECONDS * MAX_RESOURCES_PER_PASS) % len(ordered)
    return [ordered[(start + index) % len(ordered)]
            for index in range(min(MAX_RESOURCES_PER_PASS, len(ordered)))]


def recoverable_comment(steps):
    """Only the two-step atomic plan can result in one comment attempt."""
    if len(steps) != 2:
        return False
    if (steps[0]["state"] != "confirmed"
            or steps[1]["state"] not in {"ready", "rejected"}):
        return False
    operations = [json.loads(step["operation"]) for step in steps]
    return (operations[0].get("kind") == "forecast"
            and operations[1].get("kind") == "comment")


async def recover_one_comment(template_bot, *, now=None):
    """Use the existing durable plan, including after the question has closed.

Called only after both tournament inventories finish and no new eligible
forecast is available. Runtime construction binds the existing GitHub backend;
preflight must find its authenticated manifest and never initializes it.
"""
    if not template_bot.publish_reports_to_metaculus:
        return {"status": "disabled", "resources_checked": 0}
    publisher = template_bot._publication_runtime()
    store = publisher.store
    preflight = getattr(store, "preflight", None)
    if preflight is not None:
        await template_bot._publication_call(preflight)
    resources = await template_bot._publication_call(store.registered_resources)
    checked = 0
    for resource in recovery_window(resources, now=now):
        checked += 1
        key = await template_bot._publication_call(store.pending, resource)
        if key is None:
            continue
        if not isinstance(key, str) or key.split("/", 1)[0] != resource:
            raise PublicationUncertain("Saved recovery plan differs from its registered resource")
        steps = await template_bot._publication_call(store.steps, key)
        if not recoverable_comment(steps):
            continue
        saved = await template_bot._publication_call(store.report, key)
        question = restore_typed_question(json.loads(saved)["question"])
        if (ATOMIC_KINDS.get(type(question)) != question.question_type
                or question.group_question_option is not None
                or any(question.api_json.get(name) is not None for name in
                       ("notebook", "group_of_questions", "conditional"))):
            continue
        if template_bot._publication_resource(question) != resource:
            raise PublicationUncertain("Saved comment targets a different question or publication scope")
        report_type = DataOrganizer.get_report_type_for_question_type(type(question))
        report = restore_report(report_type, saved)
        # The drain re-reads the journal and revalidates the complete frozen
        # plan/hashes. Its explicit boundary can never send a forecast.
        await template_bot._drain_publication(
            publisher, key, report.question, comments_only=True,
        )
        finished = await template_bot._publication_call(store.steps, key)
        if (len(finished) != 2 or any(step["state"] != "confirmed" for step in finished)
                or [(step["position"], step["operation_hash"]) for step in finished]
                != [(step["position"], step["operation_hash"]) for step in steps]):
            raise PublicationUncertain("Frozen comment completion is not positively confirmed")
        return {"status": "comment_confirmed", "resources_checked": checked,
                "post_id": question.id_of_post, "question_id": question.id_of_question}
    return {"status": "no_recoverable_comment", "resources_checked": checked}
