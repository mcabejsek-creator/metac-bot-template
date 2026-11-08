"""Pure, offline selection for a future single-question autonomous tournament run.

This module never calls Metaculus, a model, a journal or a publication endpoint.
The caller must have completed BOTH bounded authenticated inventories. A run
may select at most one already-unforecasted, individual atomic post. This is
not a financial limit, historic eligibility guarantee or live authorization.
"""

from collections import Counter
from datetime import timedelta

from forecasting_tools import (
    BinaryQuestion, DateQuestion, MultipleChoiceQuestion, NumericQuestion,
)
from forecasting_tools.data_models.questions import DiscreteQuestion

from forecast_validation import validate_question, ForecastValidationError
from question_context import effective_close, utc_date, utc_now


MINIMUM_REMAINING = timedelta(hours=2)
ATOMIC_KINDS = {
    BinaryQuestion: "binary",
    MultipleChoiceQuestion: "multiple_choice",
    NumericQuestion: "numeric",
    DiscreteQuestion: "discrete",
    DateQuestion: "date",
}


class UnsafeAutomaticInventory(RuntimeError):
    """Never publish from an ambiguous or internally inconsistent snapshot."""


def select_one_automatic_candidate(seasonal, minibench, *, now=None):
    """Return (at-most-one season question, at-most-one MiniBench question).

    Prefer the eligible post closing soonest, but require a two-hour window
    for model work and checkpointing. Ties are deterministic: seasonal
    first, then public post ID. Group/conditional posts are excluded even
    when the SDK exposes individual atomic group leaves.

    Actual publication and cost activation must be authorized separately.
    """
    moment = utc_date(now if now is not None else utc_now())
    if not isinstance(seasonal, list) or not isinstance(minibench, list):
        raise UnsafeAutomaticInventory("Both full inventories must be present")

    all_questions = [(label, q) for label, batch in (
        ("seasonal", seasonal), ("minibench", minibench),
    ) for q in batch]
    posts = Counter()
    question_ids = set()

    for _, question in all_questions:
        post_id = getattr(question, "id_of_post", None)
        question_id = getattr(question, "id_of_question", None)
        if type(post_id) is not int or post_id <= 0:
            raise UnsafeAutomaticInventory("Invalid public post ID in inventory")
        if type(question_id) is not int or question_id <= 0 or question_id in question_ids:
            raise UnsafeAutomaticInventory("Missing or duplicated internal question ID")
        if type(getattr(question, "already_forecasted", None)) is not bool:
            raise UnsafeAutomaticInventory("Missing authoritative own-forecast status")
        question_ids.add(question_id)
        posts[post_id] += 1

    ranked = []
    for label, question in all_questions:
        post_id = question.id_of_post
        if question.already_forecasted is not False or posts[post_id] != 1:
            continue
        expected_kind = ATOMIC_KINDS.get(type(question))
        if expected_kind is None or question.question_type != expected_kind:
            continue
        # A grouped post is never a single atomic publication, even if its
        # first parsed member superficially matches a supported class.
        source = getattr(question, "api_json", None)
        if not isinstance(source, dict):
            raise UnsafeAutomaticInventory("Missing source question metadata")
        if "group_of_questions" in source or "conditional" in source:
            continue
        if getattr(question, "group_question_option", None) is not None:
            continue

        state = getattr(question, "state", None)
        if getattr(state, "value", state) != "open":
            continue
        try:
            opened = utc_date(question.open_time)
            closed = effective_close(question)
            if opened is None or closed is None:
                raise UnsafeAutomaticInventory("Unknown question timing")
            if not (opened <= moment and closed - moment >= MINIMUM_REMAINING):
                continue
            validate_question(question, publishing=True)
        except (ForecastValidationError, ValueError, TypeError, AttributeError) as error:
            raise UnsafeAutomaticInventory("Candidate failed source-format validation") from None

        ranked.append((closed, 0 if label == "seasonal" else 1, post_id, question))

    if not ranked:
        return [], []
    _, order, _, selected = min(ranked, key=lambda item: item[:3])
    return ([selected], []) if order == 0 else ([], [selected])
