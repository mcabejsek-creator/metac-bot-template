"""Lossless question data at stage boundaries; no inferred dates or probabilities."""

import contextvars
import copy
import json
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from forecasting_tools import GeneralLlm, clean_indents

TARGET_PERIOD_NOTE = "Target period: specified by the question text and resolution criteria/fine print; not inferred from submission close or resolution schedule."
MAX_COMMENT_CHARACTERS = 150000  # Existing locked SDK limit, not a new parameter.
TRUNCATION_SUFFIX = "The comment size exceeded max size and has been truncated"
_summary_context = contextvars.ContextVar("summary_question_context", default=None)


class ContextIntegrityError(ValueError):
    pass


def utc_now():
    return datetime.now(timezone.utc)


def utc_date(value):
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ContextIntegrityError("Question timing must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def effective_close(question):
    raw = question.api_json.get("question", {}).get("actual_close_time")
    if raw is None:
        return utc_date(question.close_time)
    try:
        if isinstance(raw, bool):
            raise ValueError("Boolean close time")
        if isinstance(raw, (float, int)):
            if not math.isfinite(raw):
                raise ValueError("Non-finite close time")
            parsed = question._parse_api_date(raw)
        elif isinstance(raw, str):
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            # API dates without a zone follow the same UTC convention as SDK.
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        elif isinstance(raw, datetime):
            parsed = raw
        else:
            raise ValueError("Unsupported close time")
        return utc_date(parsed)
    except (ValueError, TypeError, OverflowError, OSError) as error:
        raise ContextIntegrityError("Invalid authoritative actual_close_time; do not fall back to scheduled close") from error


def timing_contract(question):
    return [None if date is None else date.isoformat() for date in (
        utc_date(question.open_time), utc_date(question.close_time), effective_close(question),
        utc_date(question.scheduled_resolution_time), utc_date(question.actual_resolution_time))]


def measurement_data(question):
    data = {"question_type": question.question_type, "units": question.unit_of_measure,
            "group_question_option": question.group_question_option}
    group = question.api_json.get("group_of_questions")
    if group is not None:
        if not isinstance(group, dict) or not isinstance(group.get("questions"), list):
            raise ContextIntegrityError("Malformed original group context")
        members = [part for part in group["questions"] if isinstance(part, dict) and part.get("id") == question.id_of_question]
        if len(members) != 1:
            raise ContextIntegrityError("Cannot identify original group member context")
        # SDK expansion replaces member prose with shared group prose. Retain
        # both original sources, explicitly labelled, without guessing precedence
        # or including community/my_forecasts data from the API snapshot.
        fields = ("title", "label", "description", "resolution_criteria", "fine_print", "unit")
        data["original_shared_group_text"] = {key: group[key] for key in fields if key in group}
        data["original_group_member_text"] = {key: members[0][key] for key in fields if key in members[0]}
    if hasattr(question, "lower_bound"):
        for field in ("lower_bound", "upper_bound", "zero_point", "open_lower_bound", "open_upper_bound",
                      "cdf_size", "nominal_lower_bound", "nominal_upper_bound"):
            value = getattr(question, field, None)
            if isinstance(value, datetime):
                value = utc_date(value).isoformat()
            if isinstance(value, float) and not math.isfinite(value):
                raise ContextIntegrityError("Non-finite question bound metadata")
            data[field] = value
        data["nominal_bounds_meaning"] = "Display/likely-range hints only; closed hard bounds use lower_bound/upper_bound"
    return data


def measurement_context(question, now):
    text = "Measurement metadata (unabridged):\n" + json.dumps(measurement_data(question), ensure_ascii=False, allow_nan=False, indent=2)
    timing = dict(zip(("open_time", "scheduled_close_time", "effective_close_time",
                       "scheduled_resolution_time", "actual_resolution_time"), timing_contract(question)))
    text += "\nTiming metadata (UTC, unabridged):\n" + json.dumps(timing, ensure_ascii=False, indent=2)
    if question.question_type == "conditional":
        text += "\nConditional question components:\n" + "\n\n".join(
            f"{name}:\n{QuestionContext.capture(part, now).render()}" for name, part in question.get_all_subquestions().items())
    return text


@dataclass(frozen=True)
class QuestionContext:
    question_text: str
    criteria: str | None
    fine_print: str | None
    background: str | None
    options: tuple[str, ...] | None
    timing: tuple[str | None, ...]
    now: datetime
    counts: tuple[int | None, int | None]
    measurement: str

    @classmethod
    def capture(cls, question, now=None):
        now = utc_date(utc_now() if now is None else now)
        for field in ("question_text", "resolution_criteria", "fine_print", "background_info", "unit_of_measure"):
            value = getattr(question, field)
            if value is not None and not isinstance(value, str):
                raise ContextIntegrityError("Question context text has an invalid type")
        if not question.question_text.strip():
            raise ContextIntegrityError("Question context is missing its question text")
        counts = (question.num_forecasters, question.num_predictions)
        if any(value is not None and (type(value) is not int or value < 0) for value in counts):
            raise ContextIntegrityError("Question context has invalid participation counts")
        return cls(question.question_text, question.resolution_criteria, question.fine_print,
                   question.background_info, tuple(question.options) if hasattr(question, "options") else None,
                   tuple(timing_contract(question)), now, counts, measurement_context(question, now))

    def render(self):
        opened, scheduled_close, close, resolution, actual_resolution = self.timing

        def days(value):
            return "unknown" if value is None else (datetime.fromisoformat(value).date() - self.now.date()).days

        elapsed = "unknown" if opened is None else (self.now.date() - datetime.fromisoformat(opened).date()).days
        total = "unknown" if opened is None or close is None else (datetime.fromisoformat(close).date() - datetime.fromisoformat(opened).date()).days
        return "\n".join([
            "Question contract (unabridged source data):", "Forecast question:", self.question_text,
            "Answer options (if applicable):", json.dumps(self.options, ensure_ascii=False),
            "Background information supplied by Metaculus:", self.background if self.background is not None else "Not supplied",
            "Resolution criteria:", self.criteria if self.criteria is not None else "Not supplied",
            "Fine print:", self.fine_print if self.fine_print is not None else "Not supplied",
            TARGET_PERIOD_NOTE,
            f"Today (UTC): {self.now.date()}", f"Open time: {opened}", f"Scheduled close time: {scheduled_close}",
            f"Close time (effective submission deadline): {close}", f"Scheduled resolution time: {resolution}",
            f"Actual resolution time: {actual_resolution}", f"Days until close: {days(close)}",
            f"Days until scheduled resolution: {days(resolution)}", f"Days since open: {elapsed}",
            f"Total open duration in days: {total}", f"Current forecasters: {self.counts[0]}",
            f"Current predictions: {self.counts[1]}", self.measurement])


class TextSlots:
    """Keep source strings outside SDK whitespace/Markdown transformations."""
    def __init__(self):
        self.values = {}

    def add(self, value):
        if not isinstance(value, str):
            raise ContextIntegrityError("Source context must be text")
        key = "CONTEXT_SLOT_" + uuid.uuid4().hex
        while key in value:
            key = "CONTEXT_SLOT_" + uuid.uuid4().hex
        self.values[key] = value
        return key

    def restore(self, text):
        for key, value in self.values.items():
            text = text.replace(key, value)
        return text


def lossless_prompt(text, question=None, values=()):
    protected = list(values)
    if question is not None:
        protected.extend(getattr(question, field) for field in (
            "question_text", "resolution_criteria", "fine_print", "background_info", "unit_of_measure"))
    slots = TextSlots()
    values = sorted({value for value in protected if isinstance(value, str) and value}, key=len, reverse=True)
    if values:
        # One pass: a short unit such as "m" must not rewrite a previously
        # inserted opaque marker, or a substring of another source field.
        text = re.sub("|".join(re.escape(value) for value in values), lambda match: slots.add(match.group()), text)
    return slots.restore(clean_indents(text))


def validate_explanation(text):
    if len(text) > MAX_COMMENT_CHARACTERS or text.rstrip().endswith(TRUNCATION_SUFFIX):
        raise ContextIntegrityError("Full explanation cannot be published without losing context; do not truncate")


def source_block(text):
    # MarkdownTree does not track code fences; it recognises '#' at column zero.
    # Four-space literal blocks retain every source line without new report sections.
    return "\n".join("    " + line for line in text.split("\n"))


class QuestionContextMixin:
    def _context_now(self):
        return utc_now()

    def _clean_prompt(self, text, question=None, values=()):
        return lossless_prompt(text, question, values)

    def _question_context(self, question, now=None):
        return QuestionContext.capture(question, self._context_now() if now is None else now).render()

    def get_llm(self, purpose="default", guarantee_type=None):
        model = super().get_llm(purpose, guarantee_type)
        context = _summary_context.get()
        if purpose != "summarizer" or context is None or not isinstance(model, GeneralLlm):
            return model
        slots, packet = context
        copied = copy.copy(model)

        async def invoke(prompt, *args, **kwargs):
            return await model.invoke(slots.restore(prompt) + "\n\n" + packet, *args, **kwargs)

        copied.invoke = invoke
        return copied

    async def summarize_research(self, question, research):
        if not self.enable_summarize_research:
            return await super().summarize_research(question, research)
        packet, slots = self._question_context(question), TextSlots()
        token = _summary_context.set((slots, packet))
        try:
            summary = await super().summarize_research(question, slots.add(research))
        finally:
            _summary_context.reset(token)
        if self.use_research_summary_to_forecast:
            if not isinstance(summary, str) or not summary.strip():
                raise ContextIntegrityError("Cannot forecast from an empty research summary")
            # Keep the requested summary, plus the actual verified evidence. No
            # heuristic extraction can prove that a short summary kept each row.
            return summary + "\n\n" + packet + "\n\nUnabridged verified research:\n" + research
        return summary

    def _create_unified_explanation(self, question, research_prediction_collections, aggregated_prediction,
                                    final_cost, time_spent_in_minutes):
        slots = TextSlots()
        prepared = [item.model_copy(update={
            "research_report": slots.add(source_block(item.research_report)),
            "summary_report": slots.add(source_block(item.summary_report)),
            "predictions": [prediction.model_copy(update={"reasoning": slots.add(source_block(prediction.reasoning))})
                            for prediction in item.predictions]}) for item in research_prediction_collections]
        copy_question = question.model_copy(update={"question_text": slots.add(source_block(question.question_text))})
        text = super()._create_unified_explanation(copy_question, prepared, aggregated_prediction,
                                                   final_cost, time_spent_in_minutes)
        validate_explanation(text)
        # Preserve the SDK's three top-level sections for its report readers.
        if "# RESEARCH\n" not in text:
            raise ContextIntegrityError("SDK report section contract changed")
        text = text.replace("# RESEARCH\n", "# RESEARCH\n\n## Question contract\n" +
                            slots.add(source_block(self._question_context(question))) + "\n", 1)
        text = slots.restore(text)
        validate_explanation(text)
        return text
