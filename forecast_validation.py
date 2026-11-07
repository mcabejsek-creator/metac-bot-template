"""Question-bound validation; no repair, new forecasts, model calls or API reads.

SDK normalization/interpolation stays authoritative for accepted inputs. Raw
parser replies are checked before coercion, clamping and dictionary conversion.
"""

import copy
import json
import math
import re
from datetime import datetime, timezone

from forecasting_tools import (
    BinaryPrediction, BinaryQuestion, ConditionalPrediction, ConditionalQuestion,
    DatePercentile, DateQuestion, GeneralLlm, MultipleChoiceQuestion,
    NumericDistribution, NumericQuestion, Percentile, PredictedOptionList,
    PredictionAffirmed, structure_output as sdk_structure_output,
)
from forecasting_tools.data_models.numeric_report import NumericDefaults
from forecasting_tools.data_models.questions import DiscreteQuestion
from question_context import QuestionContext, TextSlots, measurement_data, timing_contract, validate_explanation

SUPPORTED = {BinaryQuestion: "binary", MultipleChoiceQuestion: "multiple_choice",
             NumericQuestion: "numeric", DiscreteQuestion: "discrete",
             DateQuestion: "date", ConditionalQuestion: "conditional"}
FLOAT_TOLERANCE = 1e-9


class ForecastValidationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ForecastValidationError(message)


def number(value):
    try:
        finite = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        finite = False
    require(finite, "Forecast values must be finite numbers, not booleans/strings")
    return value


def probability(value):
    require(0 <= number(value) <= 1, "Probability must be between zero and one")


def api_number(value):
    # The SDK accepts numeric strings in API scaling; model answers must still
    # contain JSON numbers. Do not mistake bools for numeric scaling either.
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError as error:
            raise ForecastValidationError("Invalid source API scaling number") from error
    return number(value)


def identifier(value):
    require(type(value) is int and value > 0, "Publication requires positive integer question/post IDs")


def timestamp(value):
    require(isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None,
            "Dates must have an explicit timezone")
    return number(value.timestamp())


def bounds(question):
    if isinstance(question, DateQuestion):
        return timestamp(question.lower_bound), timestamp(question.upper_bound)
    return number(question.lower_bound), number(question.upper_bound)


def validate_question(question, *, publishing=False):
    require(type(question) in SUPPORTED and question.question_type == SUPPORTED[type(question)], "Unsupported or inconsistent question type")
    if publishing:
        identifier(question.id_of_question)
        identifier(question.id_of_post)
    if isinstance(question, MultipleChoiceQuestion):
        options = question.options
        require(isinstance(options, list) and len(options) >= 2
                and all(isinstance(name, str) and name.strip() for name in options), "Question options must be nonempty strings")
        require(len(set(options)) == len(options), "Question options are duplicated")
    if isinstance(question, (NumericQuestion, DateQuestion)):
        lower, upper = bounds(question)
        require(lower < upper and math.isfinite(upper - lower), "Question bounds must be finite and increasing")
        require(type(question.open_lower_bound) is bool and type(question.open_upper_bound) is bool, "Question bound flags must be booleans")
        require(type(question.cdf_size) is int and question.cdf_size >= 2, "Question CDF size is invalid")
        if question.zero_point is not None:
            require(number(question.zero_point) < lower, "Log scale zero point must be below the lower bound")
    raw = question.api_json.get("question", {})
    require(isinstance(raw, dict), "Malformed source question snapshot")
    for field, expected in (("id", question.id_of_question), ("type", question.question_type)):
        if field in raw:
            if field == "id":
                identifier(raw[field])
            require(raw[field] == expected, "Question differs from its source API snapshot")
    if isinstance(question, MultipleChoiceQuestion) and "options" in raw:
        require(raw["options"] == question.options, "Question options differ from source API options")
    if raw.get("type") == "multiple_choice":
        require("options" in raw, "Source API question is missing answer options")
    if isinstance(question, (NumericQuestion, DateQuestion)):
        scaling = raw.get("scaling", {})
        require(isinstance(scaling, dict), "Malformed source scaling")
        if raw.get("type") in ("numeric", "discrete", "date"):
            require(all(field in raw for field in ("open_lower_bound", "open_upper_bound"))
                    and all(field in scaling for field in ("range_min", "range_max", "zero_point")),
                    "Source API question is missing explicit scaling/bound constraints")
        lower, upper = bounds(question)
        for field, expected in (("range_min", lower), ("range_max", upper), ("zero_point", question.zero_point)):
            if field in scaling:
                actual = None if scaling[field] is None else api_number(scaling[field])
                require(actual == expected, "Question scaling differs from source API scaling")
        for field in ("open_lower_bound", "open_upper_bound"):
            if field in raw:
                require(type(raw[field]) is bool and raw[field] == getattr(question, field), "Question bound flags differ from source API flags")
        if "inbound_outcome_count" in scaling:
            count = scaling["inbound_outcome_count"]
            count = 200 if count is None else count
            require(type(count) is int and count + 1 == question.cdf_size, "Question CDF size differs from API outcome count")
    if isinstance(question, ConditionalQuestion):
        for part in (question.parent, question.child, question.question_yes, question.question_no):
            require(not isinstance(part, ConditionalQuestion), "Nested conditionals are not supported")
            validate_question(part, publishing=publishing)
        if publishing:
            ids = [part.id_of_question for part in (question.parent, question.child, question.question_yes, question.question_no)]
            require(len(set(ids)) == len(ids), "Conditional question IDs overlap")


def validate_options(question, pairs, *, raw=False):
    validate_question(question)
    names = [name for name, _ in pairs]
    require(all(isinstance(name, str) and name.strip() for name in names), "Forecast option names must be nonempty strings")
    require(len(names) == len(set(names)), "Duplicate forecast option")
    require(len(names) == len(question.options) and set(names) == set(question.options), "Forecast options must match question options exactly")
    for _, value in pairs:
        probability(value)
    total = math.fsum(value for _, value in pairs)
    # Preserve SDK's existing <=1% input normalization. The wire must sum to 1.
    require(.99 <= total <= 1.01 if raw else abs(total - 1) <= FLOAT_TOLERANCE, "Option probabilities have an invalid sum")


def validate_percentiles(question, pairs, *, normalized=False):
    validate_question(question)
    require(len(pairs) >= 2, "A numeric forecast needs at least two percentiles")
    lower, upper = bounds(question)
    # SDK spreads repeated interior values downward by up to 1e-6, including
    # points just above a closed lower bound. Raw parser inputs get NO allowance.
    lower_epsilon, upper_epsilon = (1e-6, 1e-10) if normalized else (0, 0)
    for height, value in pairs:
        probability(height)
        number(value)
        require(question.open_lower_bound or value >= lower - lower_epsilon, "Percentile is below a closed lower bound")
        require(question.open_upper_bound or value <= upper + upper_epsilon, "Percentile is above a closed upper bound")
        require(question.zero_point is None or value > question.zero_point, "Percentile is outside the log scale domain")
    require(all(a[0] < b[0] and a[1] <= b[1] for a, b in zip(pairs, pairs[1:])), "Percentiles must increase and their values must not decrease")


def validate_cdf(values, question=None):
    require(isinstance(values, list) and len(values) >= 2, "CDF must contain at least two points")
    if question is not None:
        require(len(values) == question.cdf_size, "CDF length differs from question outcome count")
    for value in values:
        probability(value)
    changes = [b - a for a, b in zip(values, values[1:])]
    require(all(change >= 0 for change in changes), "CDF must not decrease")
    minimum = .01 / (len(values) - 1)
    maximum = NumericDefaults.get_max_pmf_value(len(values), include_wiggle_room=False)
    require(all(minimum - 1e-10 <= change <= maximum + 1e-10 for change in changes), "CDF bucket mass violates SDK/API density limits")
    if question is not None:
        require(values[0] >= .001 - 1e-10 if question.open_lower_bound else values[0] == 0,
                "CDF lower tail conflicts with the question bound")
        require(values[-1] <= .999 + 1e-10 if question.open_upper_bound else values[-1] == 1,
                "CDF upper tail conflicts with the question bound")


def numeric_payload(question, prediction):
    require(isinstance(prediction, NumericDistribution), "Expected a numeric distribution")
    require(all(type(getattr(prediction, field)) is bool for field in ("open_lower_bound", "open_upper_bound", "is_date")),
            "Distribution flags must be booleans")
    lower, upper = bounds(question)
    require((prediction.lower_bound, prediction.upper_bound, prediction.zero_point,
             prediction.open_lower_bound, prediction.open_upper_bound, prediction.is_date)
            == (lower, upper, question.zero_point, question.open_lower_bound, question.open_upper_bound,
                isinstance(question, DateQuestion)), "Distribution scaling/bounds differ from the question")
    validate_percentiles(question, [(p.percentile, p.value) for p in prediction.declared_percentiles], normalized=True)
    # Match NumericReport's existing None-size fallback without changing the report.
    effective = prediction
    if prediction.cdf_size is None:
        effective = NumericDistribution.from_question(prediction.declared_percentiles, question)
    require(effective.cdf_size == question.cdf_size, "Distribution CDF size differs from the question")
    cdf = effective.get_cdf()
    values = [point.percentile for point in cdf]
    validate_cdf(values, question)
    locations = [number(point.value) for point in cdf]
    require(len(locations) == question.cdf_size and all(a < b for a, b in zip(locations, locations[1:])), "CDF locations must be finite and increasing")
    axis_tolerance = max(math.ulp(lower), math.ulp(upper), (upper - lower) * 1e-12)
    require(math.isclose(locations[0], lower, rel_tol=0, abs_tol=axis_tolerance)
            and math.isclose(locations[-1], upper, rel_tol=0, abs_tol=axis_tolerance), "CDF locations differ from question bounds")
    source_grid = question.api_json.get("question", {}).get("scaling", {}).get("continuous_range")
    if source_grid is not None:
        require(isinstance(source_grid, list) and len(source_grid) == len(locations), "API CDF grid length differs")
        require(all(math.isclose(api_number(x), y, rel_tol=0, abs_tol=axis_tolerance) for x, y in zip(source_grid, locations)), "CDF locations differ from API grid")
    return values


def validate_prediction(question, prediction):
    validate_question(question)
    if isinstance(question, BinaryQuestion):
        probability(prediction)
    elif isinstance(question, MultipleChoiceQuestion):
        require(isinstance(prediction, PredictedOptionList), "Expected a multiple-choice prediction")
        validate_options(question, [(option.option_name, option.probability) for option in prediction.predicted_options])
    elif isinstance(question, (NumericQuestion, DateQuestion)):
        numeric_payload(question, prediction)
    else:
        require(isinstance(prediction, ConditionalPrediction), "Expected a conditional prediction")
        for part, value in ((question.parent, prediction.parent), (question.child, prediction.child)):
            if not isinstance(value, PredictionAffirmed):
                validate_prediction(part, value)
        validate_prediction(question.question_yes, prediction.prediction_yes)
        validate_prediction(question.question_no, prediction.prediction_no)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "Duplicate JSON field in forecast")
        result[key] = value
    return result


def parser_json(text):
    require(isinstance(text, str), "Parser must return text")
    match = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)  # Same extraction as locked SDK.
    require(match is not None, "Parser returned no JSON")
    return json.loads(match.group(0), object_pairs_hook=unique_object,
                      parse_constant=lambda _: require(False, "Non-finite JSON forecast"))


def fields(value, expected):
    require(isinstance(value, dict) and set(value) == set(expected), "Forecast JSON has missing or unexpected fields")


def raw_parser_prediction(question, output_type, data):
    validate_question(question)
    if output_type is BinaryPrediction:
        fields(data, ("prediction_in_decimal",))
        probability(data["prediction_in_decimal"])
    elif output_type is PredictedOptionList:
        fields(data, ("predicted_options",))
        require(isinstance(data["predicted_options"], list), "Options must be a list")
        pairs = []
        for option in data["predicted_options"]:
            fields(option, ("option_name", "probability"))
            require(isinstance(option["option_name"], str), "Option name must be a string")
            pairs.append((option["option_name"], option["probability"]))
        validate_options(question, pairs, raw=True)
    else:
        require(output_type in (list[Percentile], list[DatePercentile]) and isinstance(data, list), "Unexpected parser output type")
        pairs = []
        for point in data:
            fields(point, ("percentile", "value"))
            value = point["value"]
            if output_type == list[DatePercentile]:
                require(isinstance(value, str), "Date forecasts must use ISO dates/times")
                date = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    date = date.replace(tzinfo=timezone.utc)
                value = timestamp(date)
            pairs.append((point["percentile"], value))
        validate_percentiles(question, pairs)


async def structure_output(*args, question, **kwargs):
    """Same SDK schema/samples/retries; preserve context and validate before coercion."""
    model = kwargs["model"]
    require(isinstance(model, GeneralLlm), "Prediction parsing requires the configured LLM")
    output_type = kwargs.get("output_type", args[1] if len(args) > 1 else None)
    guarded = copy.copy(model)
    slots = TextSlots()
    args = list(args)
    source = args[0] if args else kwargs.get("text_to_structure")
    require(isinstance(source, str) and bool(source.strip()), "Cannot parse empty forecast reasoning")
    if args:
        args[0] = slots.add(source)
    else:
        kwargs["text_to_structure"] = slots.add(source)
    if kwargs.get("additional_instructions"):
        kwargs["additional_instructions"] = slots.add(kwargs["additional_instructions"])
    packet = QuestionContext.capture(question).render()

    async def invoke(prompt, *invoke_args, **invoke_kwargs):
        prompt = slots.restore(prompt) + "\n\n" + packet
        text = await model.invoke(prompt, *invoke_args, **invoke_kwargs)
        raw_parser_prediction(question, output_type, parser_json(text))
        return text

    guarded.invoke = invoke
    result = await sdk_structure_output(*args, **{**kwargs, "model": guarded})
    if output_type == list[DatePercentile]:
        result = [p if p.value.tzinfo is not None else p.model_copy(update={"value": p.value.replace(tzinfo=timezone.utc)}) for p in result]
    return result


class ForecastValidationMixin:
    async def _run_individual_question(self, question):
        validate_question(question)
        return await super()._run_individual_question(question)

    async def _make_prediction(self, question, research):
        result = await super()._make_prediction(question, research)
        validate_prediction(question, result.prediction_value)
        return result

    async def _aggregate_predictions(self, predictions, question):
        require(bool(predictions), "Cannot aggregate an empty prediction set")
        for prediction in predictions:
            validate_prediction(question, prediction)
        result = await super()._aggregate_predictions(predictions, question)
        validate_prediction(question, result)
        return result


def question_contract(question):
    validate_question(question)
    contract = [question.question_type, question.id_of_question, question.id_of_post,
                question.question_text, question.resolution_criteria, question.fine_print, question.unit_of_measure]
    contract.append(timing_contract(question))
    contract.extend([question.background_info, measurement_data(question)])
    if isinstance(question, MultipleChoiceQuestion):
        contract.append(question.options)
    elif isinstance(question, (NumericQuestion, DateQuestion)):
        contract.extend([*bounds(question), question.zero_point, question.open_lower_bound,
                         question.open_upper_bound, question.cdf_size])
        grid = question.api_json.get("question", {}).get("scaling", {}).get("continuous_range")
        contract.append(None if grid is None else [api_number(value) for value in grid])
    elif isinstance(question, ConditionalQuestion):
        contract.extend(question_contract(q) for q in (question.parent, question.child, question.question_yes, question.question_no))
    return contract


def restore_typed_question(raw):
    """Restore conditional subtypes; legacy base-only snapshots must prove type.

    A complete embedded API snapshot can recover fields dropped by old Pydantic
    serialization. Missing evidence blocks recovery; never infer from a forecast.
    """
    require(isinstance(raw, dict), "Saved question must be an object")
    kind = raw.get("question_type") or raw.get("api_json", {}).get("question", {}).get("type")
    types = {kind: cls for cls, kind in SUPPORTED.items()}
    require(kind in types, "Saved conditional question subtype cannot be established")
    cls = types[kind]
    data = dict(raw)
    if cls is ConditionalQuestion:
        for field in ("parent", "child", "question_yes", "question_no"):
            data[field] = restore_typed_question(data.get(field))
    elif cls in (NumericQuestion, DiscreteQuestion, DateQuestion, MultipleChoiceQuestion):
        needed = ("options",) if cls is MultipleChoiceQuestion else ("lower_bound", "upper_bound", "open_lower_bound", "open_upper_bound", "cdf_size")
        if not all(field in data for field in needed):
            from forecasting_tools.data_models.data_organizer import DataOrganizer
            source = data.get("api_json")
            require(isinstance(source, dict) and "question" in source and "id" in source,
                    "Legacy conditional snapshot lacks its question constraints")
            recovered = DataOrganizer.get_question_from_post_json(copy.deepcopy(source))
            require(type(recovered) is cls, "Legacy conditional subtype conflicts with API snapshot")
            data = {**recovered.model_dump(), **data}
    return cls.model_validate(data)


def validate_operation(operation):
    require(isinstance(operation, dict), "Publication operation must be an object")
    if operation.get("kind") == "comment":
        fields(operation, ("kind", "post_id", "text", "is_private", "included_forecast"))
        identifier(operation["post_id"])
        require(isinstance(operation["text"], str) and bool(operation["text"].strip()), "Comment must contain text")
        validate_explanation(operation["text"])
        require(type(operation["is_private"]) is bool and type(operation["included_forecast"]) is bool, "Comment flags must be booleans")
        return
    fields(operation, ("kind", "question_id", "payload"))
    require(operation["kind"] == "forecast", "Unknown publication operation")
    identifier(operation["question_id"])
    payload = operation["payload"]
    require(isinstance(payload, dict) and len(payload) == 1, "Forecast must have exactly one payload type")
    if "probability_yes" in payload:
        require(.001 <= number(payload["probability_yes"]) <= .999, "Published binary probability is outside SDK/API bounds")
    elif "probability_yes_per_category" in payload:
        values = payload["probability_yes_per_category"]
        require(isinstance(values, dict) and len(values) >= 2
                and all(isinstance(name, str) and name.strip() for name in values), "MC payload options are invalid")
        for value in values.values():
            probability(value)
        require(abs(math.fsum(values.values()) - 1) <= FLOAT_TOLERANCE, "MC payload must sum to one")
    elif "continuous_cdf" in payload:
        validate_cdf(payload["continuous_cdf"])
    else:
        raise ForecastValidationError("Unsupported publication payload type")


def expected_payload(question, prediction):
    if isinstance(question, (NumericQuestion, DateQuestion)):
        return {"continuous_cdf": numeric_payload(question, prediction)}
    validate_prediction(question, prediction)
    if isinstance(question, BinaryQuestion):
        return {"probability_yes": prediction}
    if isinstance(question, MultipleChoiceQuestion):
        return {"probability_yes_per_category": prediction.to_dict()}
    raise ForecastValidationError("A conditional needs separate branch payloads")


def validate_plan(question, report, operations):
    """Validate the entire immutable plan before its first new outbound write."""
    validate_question(question, publishing=True)
    require(question_contract(question) == question_contract(report.question), "Saved report question constraints changed; inspect the frozen plan")
    if isinstance(question, ConditionalQuestion):
        require(isinstance(report.prediction, ConditionalPrediction), "Expected a conditional prediction")
        for part, value in ((question.parent, report.prediction.parent), (question.child, report.prediction.child)):
            if not isinstance(value, PredictionAffirmed):
                validate_prediction(part, value)
        targets = [(question.question_yes, report.prediction.prediction_yes),
                   (question.question_no, report.prediction.prediction_no)]
    else:
        targets = [(question, report.prediction)]
    require(isinstance(operations, list) and len(operations) == 2 * len(targets), "Publication plan has missing/extra steps")
    for index, (target, prediction) in enumerate(targets):
        forecast, comment = operations[2 * index:2 * index + 2]
        validate_operation(forecast)
        validate_operation(comment)
        require(forecast["kind"] == "forecast" and forecast["question_id"] == target.id_of_question,
                "Forecast targets the wrong question")
        require(forecast["payload"] == expected_payload(target, prediction), "Frozen wire forecast differs from its saved report")
        require(comment["kind"] == "comment" and comment["post_id"] == target.id_of_post
                and comment["text"] == report.explanation and comment["is_private"] is True
                and comment["included_forecast"] is True, "Comment differs from the SDK report publication contract")
