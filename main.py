import argparse
import asyncio
import logging
from datetime import datetime, timezone
from typing import Literal

import dotenv

# Runtime helpers (env validation, banners, dependency-warning suppression).
from bot_helpers import (
    check_environment,
    print_run_summary_banner,
    print_startup_banner,
    silence_noisy_dependencies,
)

silence_noisy_dependencies()

from forecasting_tools import (
    AskNewsSearcher,
    BinaryQuestion,
    ForecastBot,
    GeneralLlm,
    MetaculusClient,
    MetaculusQuestion,
    MultipleChoiceQuestion,
    NumericDistribution,
    NumericQuestion,
    DateQuestion,
    DatePercentile,
    Percentile,
    ConditionalQuestion,
    ConditionalPrediction,
    PredictionTypes,
    PredictionAffirmed,
    BinaryPrediction,
    PredictedOptionList,
    ReasonedPrediction,
    SmartSearcher,
    clean_indents,
    structure_output,
)

dotenv.load_dotenv()
logger = logging.getLogger(__name__)


class SummerTemplateBot2026(ForecastBot):
    """
    This is the template bot for Summer 2026 Metaculus AI Tournament.
    This is a copy of what is used by Metaculus to run the Metac Bots in our benchmark, provided as a template for new bot makers.
    This template is given as-is, and is use-at-your-own-risk.
    We have covered most test cases in forecasting-tools it may be worth double checking key components locally.
    So far our track record has been 1 mentionable bug per season (affecting forecasts for 1-2% of total questions)

    Main changes since Fall:
    - Additional prompting has been added to numeric questions to emphasize putting pecentile values in the correct order.
    - Support for conditional and date questions has been added
    - Note: Summer AIB will not use date/conditional questions, so these are only for forecasting on the main site as you wish.

    The main entry point of this bot is `bot.forecast_on_tournament(tournament_id)` in the parent class.
    See the script at the bottom of the file for more details on how to run the bot.
    Ignoring the finer details, the general flow is:
    - Load questions from Metaculus
    - For each question
        - Execute run_research a number of times equal to research_reports_per_question
        - Execute respective run_forecast function `predictions_per_research_report * research_reports_per_question` times
        - Aggregate the predictions
        - Submit prediction (if publish_reports_to_metaculus is True)
    - Return a list of ForecastReport objects

    Alternatively, you can use the MetaculusClient to make a custom filter of questions to forecast on
    and forecast them with `bot.forecast_questions(questions)`

    Only the research and forecast functions need to be implemented in ForecastBot subclasses,
    though you may want to override other ForecastBot functions.
    In this example, you can change the prompts to be whatever you want since,
    structure_output uses an LLM to intelligently reformat the output into the needed structure.

    By default (i.e. 'tournament' mode), when you run this script, it will forecast on any open questions in the
    primary bot tournament and MiniBench. If you want to forecast on only one or the other, you can remove one
    of them from the 'tournament' mode code at the bottom of the file.

    You can experiment with what models work best with your bot by using the `llms` parameter when initializing the bot.
    You can initialize the bot with any number of models. For example,
    ```python
    my_bot = MyBot(
        ...
        llms={  # choose your model names or GeneralLlm llms here, otherwise defaults will be chosen for you
            "default": GeneralLlm(
                model="openrouter/openai/gpt-4o", # "anthropic/claude-sonnet-4-20250514", etc (see docs for litellm)
                temperature=0.3,
                timeout=40,
                allowed_tries=2,
            ),
            "summarizer": "openai/gpt-4o-mini",
            "researcher": "asknews/news-summaries",
            "parser": "openai/gpt-4o-mini",
        },
    )
    ```

    Then you can access the model in custom functions like this:
    ```python
    research_strategy = self.get_llm("researcher", "model_name"
    if research_strategy == "asknews/news-summaries":
        ...
    # OR
    summarizer = await self.get_llm("summarizer", "llm").invoke(prompt)
    # OR
    reasoning = await self.get_llm("default", "llm").invoke(prompt)
    ```

    If you end up having trouble with rate limits and want to try a more sophisticated rate limiter try:
    ```python
    from forecasting_tools import RefreshingBucketRateLimiter
    rate_limiter = RefreshingBucketRateLimiter(
        capacity=2,
        refresh_rate=1,
    ) # Allows 1 request per second on average with a burst of 2 requests initially. Set this as a class variable
    await self.rate_limiter.wait_till_able_to_acquire_resources(1) # 1 because it's consuming 1 request (use more if you are adding a token limit)
    ```
    Additionally OpenRouter has large rate limits immediately on account creation
    """

    _max_concurrent_questions = (
        1  # Set this to whatever works for your search-provider/ai-model rate limits
    )
    _concurrency_limiter = asyncio.Semaphore(_max_concurrent_questions)
    _structure_output_validation_samples = 2

    ##################################### RESEARCH #####################################

    async def run_research(self, question: MetaculusQuestion) -> str:
        async with self._concurrency_limiter:
            research = ""
            researcher = self.get_llm("researcher")

            prompt = clean_indents(
                f"""
                You are a research assistant to a highly skilled superforecaster.
                The superforecaster will give you a question they intend to forecast on.

                Produce a concise but rigorous research report. Focus on:
                - the most recent and reliable information directly relevant to the resolution criteria,
                - current quantitative data and the strongest available primary or authoritative sources,
                - historical base rates and comparable past cases,
                - when part of the relevant period has already elapsed, actively seek historical or conditional base rates for the remaining portion of the period, rather than relying only on full-period averages or general historical context,
                - if no polling exists for the exact locality or population in the question, actively search for recent polling on the same issue in the closest relevant geographic population (for example the same state or region). Clearly label such polling as a proxy rather than direct evidence, but do not omit it when it is materially informative,
                - evidence supporting the most likely outcome,
                - strong contrary evidence and reasons the apparent consensus could be wrong,
                - information that would materially distinguish between possible outcomes,
                - uncertainty, missing information, and plausible surprises.

                Actively seek diverse evidence rather than repeating several articles that report the same underlying facts.
                Prefer directly relevant sources over loosely related background material.
                Only include sources that are directly relevant to the exact forecast question or its resolution criteria.
                Do not include articles merely because they mention the same country, politician, institution, technology, or general topic.
                Exclude generic background, opinion pieces, unrelated political news, and articles whose facts would not materially change the forecast.
                When the resolution criteria name a specific resolution source, tracker, database, spreadsheet, or official page, first try to retrieve the current value or status from that exact source. Do not substitute polls, news reports, proxy metrics, or secondary summaries unless the exact resolution source is unavailable; if it is unavailable, state that explicitly.
                Prioritize evidence in this order:
                1. primary and official sources directly addressing the event or decision being forecast,
                2. high-quality reporting containing direct statements from the relevant decision-makers,
                3. reliable reporting on concrete actions, deadlines, legal constraints, or institutional procedures,
                4. historical base rates or closely comparable cases when genuinely useful.

                For every source, ask: "Could this information reasonably move the forecast probability?"
                If the answer is no, exclude it.

                It is better to return fewer highly relevant sources than many weakly related sources.
                For cumulative-count questions, verify the current count directly from the primary resolution source whenever possible. Enumerate the qualifying items or events and check that the stated count matches that list. If the metric cannot decrease, explicitly state the current confirmed minimum and that outcomes below it are impossible.
                If there is little directly relevant evidence, say so explicitly rather than padding the research with tangential material.

                When reporting is politically contentious or highly framed, distinguish carefully between:
                - direct primary evidence,
                - media interpretation or framing,
                - rhetorical statements, threats, or speculation,
                - concrete administrative or legal actions,
                - and actions that are actually feasible before the resolution deadline.

                Do not infer intent or plans from headlines alone.
                Prefer primary documents, court filings, official statements, and independently corroborated facts over commentary or partisan framing.
                When possible, compare reporting from sources with different editorial perspectives.

                Prioritize facts that can materially change the forecast. Do not merely summarize general background.
                Do not produce the final forecast yourself.

                Question:
                {question.question_text}
                Background information supplied by Metaculus:
                {question.background_info}
                Question timing and current state:
                - Open time: {question.open_time}
                - Close time: {self._get_effective_close_time(question)}
                - Scheduled resolution time: {question.scheduled_resolution_time}
                - Current forecasters: {question.num_forecasters}
                - Current predictions: {question.num_predictions}
- Today: {datetime.now(timezone.utc).date()}
- Days since open: {(datetime.now(timezone.utc).date() - question.open_time.date()).days if question.open_time else "unknown"}
- Days until close: {(self._get_effective_close_time(question).date() - datetime.now(timezone.utc).date()).days if self._get_effective_close_time(question) else "unknown"}
- Total open duration in days: {(self._get_effective_close_time(question).date() - question.open_time.date()).days if question.open_time and self._get_effective_close_time(question) else "unknown"}
Treat these timing values as authoritative. If "Days since open" is zero or positive, the question is already open and the current forecaster count is not pre-open data.
Preserve the resolution formula exactly as written in the resolution criteria. Do not reinterpret a final total as an increase from the current value and do not invent a different denominator or numerator.
When the resolution depends on the latest N polls, observations, reports, or other items from a specific source, verify that you have identified the full required set of N items from that source before reasoning about the current state. If necessary, include older items outside the current month or immediate time window rather than incorrectly treating the set as incomplete.
When using tables, time series, or multi-year projections, preserve the exact pairing between each date/year and its value. Verify every quoted date-value or year-value pair directly from the source before using it. Never assign a value from one year or period to another, and never infer a missing target-period value from an adjacent row. If the exact target-period value is unavailable, state explicitly that it is unavailable rather than substituting a nearby value.
When a source reports multiple related metrics, identify the exact metric used by the resolution criteria and do not substitute a broader, adjacent, or similarly named metric. Explicitly verify that every quoted number measures the same resolution metric before using it in calculations.                
When the resolution criteria can be satisfied through more than one event, attempt, or pathway before the deadline, identify all materially plausible qualifying pathways. Do not assume that the most imminent or most discussed event is the only opportunity unless the evidence establishes that.
When identifying the current state or closest observed benchmark, "observed" means an actual realized value from the resolution source, or from an explicitly equivalent source using the same definition and methodology. Never call a forecast, projection, estimate, scenario, target, or value from a different source an observed benchmark. If the future-period resolved value does not yet exist, use the latest realized value from the same resolution series if available; otherwise state that no compatible observed benchmark exists. Forecasts and projections belong in the expert-forecast section, not in the current-state benchmark.                
For questions about whether a specific person or organization will perform an action or attend an event, actively search for recent directly comparable instances involving the same subject and context, and summarize the observed frequency or pattern when the evidence supports it. Do not stop at whether the future action is currently confirmed.
For questions about the number of rare events occurring within a future event or period, actively determine the historical per-event distribution over comparable past events, including how often zero events occurred and whether multiple events occurred. Do not infer frequency from a cumulative historical total or only from years in which the event occurred.
When the resolution source provides downloadable structured historical data such as CSV, JSON, spreadsheets, or an API, actively retrieve or query that data and calculate the comparable historical rates or period distributions needed for the forecast. Do not conclude that a historical base rate is unavailable merely because it is not shown in a search result or webpage excerpt if the underlying structured dataset or API is accessible.
For questions resolving to a future value of a tracker, classification count, ranking, or other repeatedly updated metric, actively look for recent prior snapshots of the exact same metric from the same source to estimate the observed pace and direction of change. If comparable historical snapshots are unavailable, state this explicitly and do not infer a trend from deadlines or general expectations alone.
For questions whose future value is governed by explicit rules, resets, thresholds, or state transitions, actively identify those mechanics and any rule-defined minimum, maximum, or reset value, and use historical frequencies or durations of the relevant transitions when available.
For questions asking whether a numeric threshold will be reached or crossed within a future period, calculate the distance from the closest relevant observed value to the threshold and actively look for historical volatility, comparable moves, or threshold-crossing frequencies over similar time horizons for the exact resolution metric.
For legislative, regulatory, judicial, or other procedural questions with a deadline, identify the remaining formal steps, the applicable procedural calendar, and any mechanisms such as extensions, extraordinary sessions, expedited procedures, or alternative qualifying paths that could still produce the resolution outcome before the deadline. Also search for the latest substantive evidence of negotiations, prioritization, scheduling, or vote support.
When the question asks how many entities currently or eventually fall into a category, actively obtain the current count for that exact category from the official source, including raw data, APIs, downloadable files, or machine-readable map/WMS data when available, rather than stopping at a qualitative description.
This question's outcome will be determined by the specific criteria below:
                {question.resolution_criteria}

                {question.fine_print}
                """
            )

            if isinstance(researcher, GeneralLlm):
                research = await researcher.invoke(prompt)

            elif researcher == "perplexity-agent":
                import json
                import os
                import urllib.request

                def call_perplexity_agent(agent_prompt):
                    request = urllib.request.Request(
                        "https://api.perplexity.ai/v1/agent",
                        data=json.dumps(
                            {
                                "preset": "fast",
                                "input": agent_prompt,
                            }
                        ).encode("utf-8"),
                        headers={
                            "Authorization": f"Bearer {os.environ['PERPLEXITY_API_KEY']}",
                            "Content-Type": "application/json",
                        },
                        method="POST",
                    )

                    with urllib.request.urlopen(request, timeout=180) as response:
                        data = json.loads(response.read().decode("utf-8"))

                    answer_parts = []
                    sources = []

                    for item in data.get("output", []):
                        if item.get("type") == "message":
                            for content in item.get("content", []):
                                if content.get("type") == "output_text":
                                    answer_parts.append(content.get("text", ""))

                        elif item.get("type") == "search_results":
                            for result in item.get("results", []):
                                title = result.get("title", "")
                                url = result.get("url", "")
                                if url:
                                    sources.append(f"- {title}: {url}")

                    research_text = "\n".join(answer_parts)

                    if sources:
                        research_text += "\n\nSources:\n" + "\n".join(sources)

                    return research_text
                research = await asyncio.to_thread(call_perplexity_agent, prompt)

                fact_check_prompt = clean_indents(
                    f"""
                    You are a meticulous fact-checker reviewing research for a forecasting question.

                    Forecast question:
                    {question.question_text}

                    Resolution criteria:
                    {question.resolution_criteria}

                    Original research:
                    {research}

                    Verify the research against reliable sources, prioritizing primary and official sources.

                    Pay special attention to:
                    - every numerical claim,
                    - every date-value and year-value pairing,
                    - tables and multi-year projections,
                    - whether a value is observed, estimated, projected, or forecast,
                    - whether the quoted metric exactly matches the resolution metric,
                    When comparing numerical values, explicitly compare the exact metric required by the resolution criteria. Do not treat a larger lead, margin, ratio, or net support as a larger raw value when the resolution depends on the raw value itself.
                    - calculations derived from source values.
                    Never assign a value from one year, date, category, or metric to another.
                    Never infer a missing target-period value from an adjacent table row.
                    If an exact target-period value is not visible in a source summary, actively search the primary source's tables, appendices, downloadable data, or supplemental files before concluding that the value is unavailable.
                    If a primary-source table, appendix, downloadable dataset, or supplemental file provides an exact target-period value, treat that value as authoritative over conflicting summaries, snippets, secondary sources, or narrative descriptions, and do not later replace it with a less specific or conflicting figure.
                    If a numerical claim cannot be verified, remove it or explicitly state that it could not be verified.
                    If the original research contains an error, correct it using the source.
                    Preserve useful qualitative evidence when it remains supported.

                    Return a corrected, self-contained research report for the forecaster.
                    Do not discuss the fact-checking process itself and do not produce a forecast.
                    """
                )

                research = await asyncio.to_thread(
                    call_perplexity_agent,
                    fact_check_prompt,
                )

            elif not researcher or researcher == "None" or researcher == "no_research":
                research = ""

            else:
                research = await self.get_llm("researcher", "llm").invoke(prompt)

            logger.info(f"Found Research for URL {question.page_url}:\n{research}")
            return research

    ##################################### BINARY QUESTIONS #####################################

    async def _run_forecast_on_binary(
        self, question: BinaryQuestion, research: str
    ) -> ReasonedPrediction[float]:
        prompt = clean_indents(
            f"""
            You are a professional forecaster interviewing for a job.

            Your interview question is:
            {question.question_text}

            Question background:
            {question.background_info}


            This question's outcome will be determined by the specific criteria below. These criteria have not yet been satisfied:
            {question.resolution_criteria}

            {question.fine_print}


            Your research assistant says:
            {research}

            Today is {datetime.now().strftime("%Y-%m-%d")}.
            Question timing from Metaculus (authoritative):
- Close time: {self._get_effective_close_time(question)}
- Scheduled resolution time: {question.scheduled_resolution_time}
- Days until close: {(self._get_effective_close_time(question).date() - datetime.now(timezone.utc).date()).days if self._get_effective_close_time(question) else "unknown"}
- Days until scheduled resolution: {(question.scheduled_resolution_time.date() - datetime.now(timezone.utc).date()).days if question.scheduled_resolution_time else "unknown"}

Treat these timing values as authoritative. Do not confuse the close time with the scheduled resolution time.
The close time is only the deadline for submitting forecasts and is not evidence about whether the real-world outcome can still occur, unless the resolution criteria explicitly tie the outcome to the close time. Never use zero or expired days until close as evidence for Yes or No; assess the remaining opportunity using the scheduled resolution time or the actual deadline stated in the resolution criteria.
            First, explicitly determine from the question and resolution criteria what a Yes outcome means and what a No outcome means. Do not reverse these labels based on which candidate, side, or outcome is currently leading. Before assigning probabilities, verify that every Yes scenario actually satisfies the Yes resolution criteria and every No scenario does not.
            Do not add any motive, purpose, threshold, exclusion, or other requirement that is not explicitly present in the resolution criteria or fine print. Treat the fine print as binding.
            Strictly distinguish resolution requirements from real-world causal, political, legal, or procedural conditions. Such conditions may affect the probability of the outcome, but they are not requirements for resolution unless the resolution criteria or fine print explicitly make them so.
            Before answering you write:
            (a) State the exact authoritative number of days remaining until close and until scheduled resolution using the values provided above. Do not convert these values into months or years, and do not recalculate the interval independently.
            (b) The status quo outcome if nothing changed.
            (c) A brief description of a scenario that results in a No outcome.
            (d) A brief description of a scenario that results in a Yes outcome.
            Before assigning the final probability, identify the closest directly comparable historical case or cases in the research and use them quantitatively where possible. If only one comparable case is available, treat it as a single precedent, not as a general historical pattern. Never write "historically", "usually", or similar language unless multiple comparable cases in the research support that claim.
            You write your rationale weighing evidence by relevance and reliability. Do not automatically favor Yes or No as a "status quo" outcome. For elections, contests, threshold questions, competitions, and other one-off events, apply status-quo reasoning only if a genuine persistent baseline exists. Do not treat indirect proxy evidence, such as the average of all entries, as direct evidence about the winner or threshold unless the research supports that relationship.
            Do not treat proxy indicators, related metrics, individual polls, or adjacent measures as if they were the resolution metric. If the exact resolution metric, threshold, or time series is unavailable, preserve that uncertainty. Do not infer a trend in the resolution metric from proxy evidence unless the research explicitly supports that relationship.
            Use only factual claims, historical patterns, standings, performance information, reports, and other evidence that are explicitly supported by the question or the research above. Do not introduce outside facts from memory or assumptions. If the research says such evidence is unavailable, treat it as unavailable.
            {self._get_conditional_disclaimer_if_necessary(question)}

            The last thing you write is your final answer as: "Probability of YES: ZZ%", 0-100. This must always be the probability that the question resolves Yes, never the probability of No. If your reasoning estimates the probability of No, convert it to 100% minus that probability before giving the final answer.
            """
        )

        return await self._binary_prompt_to_forecast(question, prompt)

    async def _binary_prompt_to_forecast(
        self,
        question: BinaryQuestion,
        prompt: str,
    ) -> ReasonedPrediction[float]:
        reasoning = await self.get_llm("default", "llm").invoke(prompt)
        logger.info(f"Reasoning for URL {question.page_url}: {reasoning}")
        binary_prediction: BinaryPrediction = await structure_output(
            reasoning,
            BinaryPrediction,
            model=self.get_llm("parser", "llm"),
            num_validation_samples=self._structure_output_validation_samples,
        )
        decimal_pred = max(0.01, min(0.99, binary_prediction.prediction_in_decimal))

        logger.info(
            f"Forecasted URL {question.page_url} with prediction: {decimal_pred}."
        )
        return ReasonedPrediction(prediction_value=decimal_pred, reasoning=reasoning)

    ##################################### MULTIPLE CHOICE QUESTIONS #####################################

    async def _run_forecast_on_multiple_choice(
        self, question: MultipleChoiceQuestion, research: str
    ) -> ReasonedPrediction[PredictedOptionList]:
        prompt = clean_indents(
            f"""
            You are a professional forecaster interviewing for a job.

            Your interview question is:
            {question.question_text}

            The options are: {question.options}


            Background:
            {question.background_info}

            {question.resolution_criteria}

            {question.fine_print}


            Your research assistant says:
            {research}
Before assigning probabilities, explicitly determine the exact resolution condition for each answer option and map evidence only to the option it directly supports. Do not confuse influencing, supporting, enabling, or acting as a coalition partner or kingmaker with satisfying an option that requires actually leading, winning, controlling, or otherwise meeting a specific resolution condition.
Use only factual claims and historical patterns supported by the question or the research above.
Do not invent base rates, historical tendencies, polling patterns, endorsements, precedents, or other evidence.
If the research says that a useful base rate or comparison is unavailable, treat it as unavailable.
In elections and other competitive events, do not treat the current leader as a "status quo" outcome merely because they are currently ahead.
            When the resolution criteria specify an exact time window, only count events that can occur within that window. Do not infer the number remaining in the resolution window by subtracting completed events from a broader-period total if some remaining events fall outside the window.
            Today is {datetime.now().strftime("%Y-%m-%d")}.
Question timing from Metaculus (authoritative):
- Close time: {question.close_time}
- Scheduled resolution time: {question.scheduled_resolution_time}
- Days until close: {(question.close_time.date() - datetime.now(timezone.utc).date()).days if question.close_time else "unknown"}
- Days until scheduled resolution: {(question.scheduled_resolution_time.date() - datetime.now(timezone.utc).date()).days if question.scheduled_resolution_time else "unknown"}

Treat these timing values as authoritative. Do not calculate or infer a different remaining time horizon. The question may resolve earlier if its resolution criteria allow it, but never invent a later deadline.
            Before answering you write:
            (a) State the exact remaining days until scheduled resolution from the authoritative value above, and note if the resolution criteria allow the outcome to become known earlier.
            (b) Determine whether a literal status quo outcome is meaningful for this question. For a new discrete selection, election, award, or annual choice, do not treat the previous winner or previous year's category as the status quo unless the resolution criteria explicitly make the outcome persistent.
            (c) A description of an scenario that results in an unexpected outcome.

            {self._get_conditional_disclaimer_if_necessary(question)}
            You write your rationale weighing evidence by relevance and reliability. Apply status-quo reasoning only when the question genuinely has a persistent status quo; do not use it automatically in elections or competitive events. Leave some moderate probability on plausible options to account for unexpected outcomes.
            If the research contains betting-market odds, forecasts, or other quantitative estimates for the same outcome, use them as quantitative evidence where relevant rather than ignoring them, while accounting for their limitations.
            Your final probabilities across all options must sum to exactly 100%. Check the arithmetic before giving the final answer.
            The last thing you write is your final probabilities for the N options in this order {question.options} as:
            Option_A: Probability_A
            Option_B: Probability_B
            ...
            Option_N: Probability_N
            """
        )
        return await self._multiple_choice_prompt_to_forecast(question, prompt)

    async def _multiple_choice_prompt_to_forecast(
        self,
        question: MultipleChoiceQuestion,
        prompt: str,
    ) -> ReasonedPrediction[PredictedOptionList]:
        parsing_instructions = clean_indents(
            f"""
            Make sure that all option names are one of the following:
            {question.options}

            The text you are parsing may prepend these options with some variation of "Option" which you should remove if not part of the option names I just gave you.
            Additionally, you may sometimes need to parse a 0% probability. Please do not skip options with 0% but rather make it an entry in your final list with 0% probability.
            """
        )
        reasoning = await self.get_llm("default", "llm").invoke(prompt)
        logger.info(f"Reasoning for URL {question.page_url}: {reasoning}")
        predicted_option_list: PredictedOptionList = await structure_output(
            text_to_structure=reasoning,
            output_type=PredictedOptionList,
            model=self.get_llm("parser", "llm"),
            num_validation_samples=self._structure_output_validation_samples,
            additional_instructions=parsing_instructions,
        )

        logger.info(
            f"Forecasted URL {question.page_url} with prediction: {predicted_option_list}."
        )
        return ReasonedPrediction(
            prediction_value=predicted_option_list, reasoning=reasoning
        )

    ##################################### NUMERIC QUESTIONS #####################################

    async def _run_forecast_on_numeric(
        self, question: NumericQuestion, research: str
    ) -> ReasonedPrediction[NumericDistribution]:
        upper_bound_message, lower_bound_message = (
            self._create_upper_and_lower_bound_messages(question)
        )
        prompt = clean_indents(
            f"""
            You are a professional forecaster interviewing for a job.

            Your interview question is:
            {question.question_text}

            Background:
            {question.background_info}

            {question.resolution_criteria}

            {question.fine_print}

            Units for answer: {question.unit_of_measure if question.unit_of_measure else "Not stated (please infer this)"}

            Your research assistant says:
            {research}

            Today is {datetime.now().strftime("%Y-%m-%d")}.
            Question timing from Metaculus (authoritative):
- Open time: {question.open_time}
- Close time: {question.close_time}
- Scheduled resolution time: {question.scheduled_resolution_time}
- Current forecasters: {question.num_forecasters}
- Current predictions: {question.num_predictions}
- Days until close: {(question.close_time.date() - datetime.now(timezone.utc).date()).days if question.close_time else "unknown"}
- Days until scheduled resolution: {(question.scheduled_resolution_time.date() - datetime.now(timezone.utc).date()).days if question.scheduled_resolution_time else "unknown"}
- Days since open: {(datetime.now(timezone.utc).date() - question.open_time.date()).days if question.open_time else "unknown"}
The current forecaster count is observed AFTER the question opened. Do not describe it as pre-open data. "Days since open" is the elapsed participation period for the current count.
- Total open duration in days: {(question.close_time.date() - question.open_time.date()).days if question.open_time and question.close_time else "unknown"}
IMPORTANT: "Days until close" and "Days until scheduled resolution" above are the authoritative REMAINING time from today. Use those exact numbers. Do not confuse them with the question's total open duration. A figure such as 730 days may describe the TOTAL open duration and must NOT be used as the remaining time.
Do not confuse the resolution metric with the current participation trend. Current forecasters divided by total open duration is only the final resolved value if no more forecasters join. If discussing the current arrival rate, use days since open, and do not extrapolate an early rate linearly unless justified by evidence.
            {lower_bound_message}
            {upper_bound_message}
Respect all hard logical bounds implied by the current state and the resolution criteria. For cumulative counts or totals that cannot decrease, no forecast percentile may imply a final value below the minimum already guaranteed by the current observed value, unless the resolution source explicitly allows downward revisions or corrections.
            Formatting Instructions:
            - Please notice the units requested and give your answer in these units (e.g. whether you represent a number as 1,000,000 or 1 million).
            - Never use scientific notation.
            - All reported percentile values must be strictly increasing: P10 < P20 < P40 < P60 < P80 < P90. Never output identical percentile values. Even when substantial probability mass is concentrated at a hard lower or upper bound, keep at most the nearest percentile exactly at that bound and make every subsequent percentile strictly higher, or strictly lower when approaching an upper bound.

          Before answering you write:
(a) State the exact remaining days until close and until scheduled resolution from the authoritative values above.
(b) Determine whether a literal "nothing changed" outcome is meaningful for this resolution metric. If the current observed state can directly be carried forward to the resolved value, calculate that value. If the question concerns a future period or quantity that does not yet exist, state that no literal "nothing changed" outcome is defined; instead identify the closest compatible observed benchmark without treating it as the future resolved value and without extrapolating a trend. An observed benchmark must be an actual realized value from the resolution source, or from an explicitly equivalent source using the same definition and methodology. Never call a forecast, projection, estimate, scenario, target, or differently defined metric an observed benchmark. If no compatible observed benchmark exists, state that clearly.

(c) First determine whether any true hard lower or upper bound exists. A hard bound exists only if it is logically or mathematically impossible for the resolved value to cross it given the resolution criteria and current observed state. Historical values, creator-provided likely ranges, forecasts, estimates, past minima or maxima, and recent observations are not hard bounds by themselves. For a cumulative total measured over a period already in progress, any already realized amount that is part of the final resolution total is a true hard lower bound unless the resolution source can revise that amount downward. A partial-period cumulative value is not merely a benchmark; it is a lower bound on the final cumulative total. If no true hard bound exists, explicitly state that no hard bound is established and do not constrain the forecast percentiles by one.
For cumulative metrics, any already-realized confirmed cumulative value is a hard lower bound unless the resolution criteria explicitly allow the value to be revised downward; never place any forecast percentile below that confirmed minimum.
For questions resolving to a maximum/highest or minimum/lowest value over a period, any already-observed qualifying extreme is also the literal "nothing changed" outcome and a hard bound: an observed maximum is a hard lower bound, while an observed minimum is a hard upper bound.
For count questions over a fixed known set of entities, the hard lower bound is 0 and the hard upper bound is the total number of eligible entities, unless the resolution criteria define a different universe.
For any non-negative count question, the hard lower bound is 0 unless the resolution criteria explicitly allow negative values. A hard upper bound exists only when the number of possible entities, opportunities, or events is mathematically fixed.
For percentage or proportion questions, the hard lower bound is 0% and the hard upper bound is 100%, unless the resolution criteria explicitly define a narrower mathematically possible range.
For differences, margins, spreads, or leads between two percentages or proportions, the hard bounds are -100 to +100 percentage points unless the resolution criteria imply a narrower mathematically possible range.
(d) If a genuine quantitative trend over time is explicitly supported by the research, describe what would happen if it continued. Do not infer a trend from a single snapshot, one prior result, a first-round result, or a proxy metric. If no supported trend exists, state that clearly.
(e) Relevant expert forecasts, market expectations, polls, or external projections only if explicitly present in the research. Do not relabel the research assistant's own estimate as expert or market consensus. If none are available, state that clearly.
(f) A brief description of an unexpected scenario that results in a low outcome.
(g) A brief description of an unexpected scenario that results in a high outcome.
            {self._get_conditional_disclaimer_if_necessary(question)}
            You remind yourself that good forecasters are humble and set wide 90/10 confidence intervals to account for unknown unknowns.

            The last thing you write is your final answer as:
            "
            Percentile 10: XX (lowest number value)
            Percentile 20: XX
            Percentile 40: XX
            Percentile 60: XX
            Percentile 80: XX
            Percentile 90: XX (highest number value)
            "
            """
        )
        return await self._numeric_prompt_to_forecast(question, prompt)

    async def _numeric_prompt_to_forecast(
        self,
        question: NumericQuestion,
        prompt: str,
    ) -> ReasonedPrediction[NumericDistribution]:
        reasoning = await self.get_llm("default", "llm").invoke(prompt)
        logger.info(f"Reasoning for URL {question.page_url}: {reasoning}")
        parsing_instructions = clean_indents(
            f"""
            The text given to you is trying to give a forecast distribution for a numeric question.
            - This text is trying to answer the numeric question: "{question.question_text}".
            - When parsing the text, please make sure to give the values (the ones assigned to percentiles) in terms of the correct units.
            - The units for the forecast are: {question.unit_of_measure}
            - If a percentile value is written as a bare number and is on the same numerical scale as the question's lower and upper bounds, preserve that number exactly as written. Do not rescale it based on units mentioned elsewhere in the reasoning. Only convert a percentile value when that percentile explicitly states a different unit.
            - Your work will be shown publicly with these units stated verbatim after the numbers your parse.
            - As an example, someone else guessed that the answer will be between {question.lower_bound} {question.unit_of_measure} and {question.upper_bound} {question.unit_of_measure}, so the numbers parsed from an answer like this would be verbatim "{question.lower_bound}" and "{question.upper_bound}".
            - If the answer doesn't give the answer in the correct units, you should parse it in the right units. For instance if the answer gives numbers as $500,000,000 and units are "B $" then you should parse the answer as 0.5 (since $500,000,000 is $0.5 billion).
            - If percentiles are not explicitly given (e.g. only a single value is given) please don't return a parsed output, but rather indicate that the answer is not explicitly given in the text.
            - Turn any values that are in scientific notation into regular numbers.
            """
        )
        percentile_list: list[Percentile] = await structure_output(
            reasoning,
            list[Percentile],
            model=self.get_llm("parser", "llm"),
            additional_instructions=parsing_instructions,
            num_validation_samples=self._structure_output_validation_samples,
        )
        prediction = NumericDistribution.from_question(percentile_list, question)
        prediction.get_cdf()
        logger.info(
            f"Forecasted URL {question.page_url} with prediction: {prediction.declared_percentiles}."
        )
        return ReasonedPrediction(prediction_value=prediction, reasoning=reasoning)

    ##################################### DATE QUESTIONS #####################################

    async def _run_forecast_on_date(
        self, question: DateQuestion, research: str
    ) -> ReasonedPrediction[NumericDistribution]:
        upper_bound_message, lower_bound_message = (
            self._create_upper_and_lower_bound_messages(question)
        )
        prompt = clean_indents(
            f"""
            You are a professional forecaster interviewing for a job.

            Your interview question is:
            {question.question_text}

            Background:
            {question.background_info}

            {question.resolution_criteria}

            {question.fine_print}

            Your research assistant says:
            {research}

            Today is {datetime.now().strftime("%Y-%m-%d")}.

            {lower_bound_message}
            {upper_bound_message}

            Formatting Instructions:
            - This is a date question, and as such, the answer must be expressed in terms of dates.
            - The dates must be written in the format of YYYY-MM-DD. If hours matter, please append the date with the hour in UTC and military time: YYYY-MM-DDTHH:MM:SSZ.No other formatting is allowed.
            - Always start with a lower date chronologically and then increase from there.
            - Do NOT forget this. The dates must be written in chronological order starting at the earliest time at percentile 10 and increasing from there.
            Before answering you write:
            (a) The time left until the outcome to the question is known.
            (b) The outcome if nothing changed.
            (c) The outcome if the current trend continued.
            (d) The expectations of experts and markets.
            (e) A brief description of an unexpected scenario that results in a low outcome.
            (f) A brief description of an unexpected scenario that results in a high outcome.

            {self._get_conditional_disclaimer_if_necessary(question)}
            You remind yourself that good forecasters are humble and set wide 90/10 confidence intervals to account for unknown unknowns.

            The last thing you write is your final answer as:
            "
            Percentile 10: YYYY-MM-DD (oldest date)
            Percentile 20: YYYY-MM-DD
            Percentile 40: YYYY-MM-DD
            Percentile 60: YYYY-MM-DD
            Percentile 80: YYYY-MM-DD
            Percentile 90: YYYY-MM-DD (newest date)
            "
            """
        )
        forecast = await self._date_prompt_to_forecast(question, prompt)
        return forecast

    async def _date_prompt_to_forecast(
        self,
        question: DateQuestion,
        prompt: str,
    ) -> ReasonedPrediction[NumericDistribution]:
        reasoning = await self.get_llm("default", "llm").invoke(prompt)
        logger.info(f"Reasoning for URL {question.page_url}: {reasoning}")
        parsing_instructions = clean_indents(
            f"""
            The text given to you is trying to give a forecast distribution for a date question.
            - This text is trying to answer the question: "{question.question_text}".
            - As an example, someone else guessed that the answer will be between {question.lower_bound} and {question.upper_bound}, so the numbers parsed from an answer like this would be verbatim "{question.lower_bound}" and "{question.upper_bound}".
            - The output is given as dates/times please format it into a valid datetime parsable string. Assume midnight UTC if no hour is given.
            - If percentiles are not explicitly given (e.g. only a single value is given) please don't return a parsed output, but rather indicate that the answer is not explicitly given in the text.
            """
        )
        date_percentile_list: list[DatePercentile] = await structure_output(
            reasoning,
            list[DatePercentile],
            model=self.get_llm("parser", "llm"),
            additional_instructions=parsing_instructions,
            num_validation_samples=self._structure_output_validation_samples,
        )

        percentile_list = [
            Percentile(
                percentile=percentile.percentile,
                value=percentile.value.timestamp(),
            )
            for percentile in date_percentile_list
        ]
        prediction = NumericDistribution.from_question(percentile_list, question)
        logger.info(
            f"Forecasted URL {question.page_url} with prediction: {prediction.declared_percentiles}."
        )
        return ReasonedPrediction(prediction_value=prediction, reasoning=reasoning)
    def _get_effective_close_time(
        self, question: MetaculusQuestion
    ) -> datetime | None:
        actual_close_raw = question.api_json.get("question", {}).get("actual_close_time")
        if actual_close_raw is not None:
            return question._parse_api_date(actual_close_raw)
        return question.close_time

    def _create_upper_and_lower_bound_messages(
        self, question: NumericQuestion | DateQuestion
    ) -> tuple[str, str]:
        if isinstance(question, NumericQuestion):
            if question.nominal_upper_bound is not None:
                upper_bound_number = question.nominal_upper_bound
            else:
                upper_bound_number = question.upper_bound
            if question.nominal_lower_bound is not None:
                lower_bound_number = question.nominal_lower_bound
            else:
                lower_bound_number = question.lower_bound
            unit_of_measure = question.unit_of_measure
        elif isinstance(question, DateQuestion):
            upper_bound_number = question.upper_bound.date().isoformat()
            lower_bound_number = question.lower_bound.date().isoformat()
            unit_of_measure = ""
        else:
            raise ValueError()

        if question.open_upper_bound:
            upper_bound_message = f"The question creator thinks the number is likely not higher than {upper_bound_number} {unit_of_measure}."
        else:
            upper_bound_message = f"The outcome can not be higher than {upper_bound_number} {unit_of_measure}."

        if question.open_lower_bound:
            lower_bound_message = f"The question creator thinks the number is likely not lower than {lower_bound_number} {unit_of_measure}."
        else:
            lower_bound_message = f"The outcome can not be lower than {lower_bound_number} {unit_of_measure}."
        return upper_bound_message, lower_bound_message

    ##################################### CONDITIONAL QUESTIONS #####################################

    async def _run_forecast_on_conditional(
        self, question: ConditionalQuestion, research: str
    ) -> ReasonedPrediction[ConditionalPrediction]:
        parent_info, full_research = await self._get_question_prediction_info(
            question.parent, research, "parent"
        )
        child_info, full_research = await self._get_question_prediction_info(
            question.child, research, "child"
        )
        yes_info, full_research = await self._get_question_prediction_info(
            question.question_yes, full_research, "yes"
        )
        no_info, full_research = await self._get_question_prediction_info(
            question.question_no, full_research, "no"
        )
        full_reasoning = clean_indents(
            f"""
            ## Parent Question Reasoning
            {parent_info.reasoning}
            ## Child Question Reasoning
            {child_info.reasoning}
            ## Yes Question Reasoning
            {yes_info.reasoning}
            ## No Question Reasoning
            {no_info.reasoning}
        """
        )
        full_prediction = ConditionalPrediction(
            parent=parent_info.prediction_value,  # type: ignore
            child=child_info.prediction_value,  # type: ignore
            prediction_yes=yes_info.prediction_value,  # type: ignore
            prediction_no=no_info.prediction_value,  # type: ignore
        )
        return ReasonedPrediction(
            reasoning=full_reasoning, prediction_value=full_prediction
        )

    async def _get_question_prediction_info(
        self, question: MetaculusQuestion, research: str, question_type: str
    ) -> tuple[ReasonedPrediction[PredictionTypes | PredictionAffirmed], str]:
        from forecasting_tools.data_models.data_organizer import DataOrganizer

        previous_forecasts = question.previous_forecasts
        if (
            question_type in ["parent", "child"]
            and previous_forecasts
            and question_type not in self.force_reforecast_in_conditional
        ):
            # TODO: add option to not affirm current parent/child forecasts, create new forecast
            previous_forecast = previous_forecasts[-1]
            current_utc_time = datetime.now(timezone.utc)
            if (
                previous_forecast.timestamp_end is None
                or previous_forecast.timestamp_end > current_utc_time
            ):
                pretty_value = DataOrganizer.get_readable_prediction(previous_forecast)  # type: ignore
                prediction = ReasonedPrediction(
                    prediction_value=PredictionAffirmed(),
                    reasoning=f"Already existing forecast reaffirmed at {pretty_value}.",
                )
                return (prediction, research)  # type: ignore
        info = await self._make_prediction(question, research)
        full_research = self._add_reasoning_to_research(research, info, question_type)
        return info, full_research  # type: ignore

    def _add_reasoning_to_research(
        self,
        research: str,
        reasoning: ReasonedPrediction[PredictionTypes],
        question_type: str,
    ) -> str:
        from forecasting_tools.data_models.data_organizer import DataOrganizer

        question_type = question_type.title()
        return clean_indents(
            f"""
            {research}
            ---
            ## {question_type} Question Information
            You have previously forecasted the {question_type} Question to the value: {DataOrganizer.get_readable_prediction(reasoning.prediction_value)}
            This is relevant information for your current forecast, but it is NOT your current forecast, but previous forecasting information that is relevant to your current forecast.
            The reasoning for the {question_type} Question was as such:
            ```
            {reasoning.reasoning}
            ```
            This is absolutely essential: do NOT use this reasoning to re-forecast the {question_type} question.
            """
        )

    def _get_conditional_disclaimer_if_necessary(
        self, question: MetaculusQuestion
    ) -> str:
        if question.conditional_type not in ["yes", "no"]:
            return ""
        return clean_indents(
            """
            As you are given a conditional question with a parent and child, you are to only forecast the **CHILD** question, given the parent question's resolution.
            You never re-forecast the parent question under any circumstances, but you use probabilistic reasoning, strongly considering the parent question's resolution, to forecast the child question.
            """
        )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    parser = argparse.ArgumentParser(description="Run the template forecasting bot")
    parser.add_argument(
        "--mode",
        type=str,
        choices=["tournament", "metaculus_cup", "test_questions"],
        default="tournament",
        help="What to forecast on (default: tournament)",
    )
    args = parser.parse_args()
    run_mode: Literal["tournament", "metaculus_cup", "test_questions"] = args.mode

    check_environment(strict=True)
    publish_to_metaculus = True
    print_startup_banner(run_mode, will_publish=publish_to_metaculus)

    # Configure the bot. The `llms=` block below is commented out to use
    # whichever default models forecasting-tools picks based on your env vars;
    # uncomment and edit to pin specific models.
    template_bot = SummerTemplateBot2026(
        research_reports_per_question=2,
predictions_per_research_report=3,
        use_research_summary_to_forecast=False,
        publish_reports_to_metaculus=publish_to_metaculus,
        folder_to_save_reports_to=None,
        skip_previously_forecasted_questions=True,
        extra_metadata_in_explanation=True,
                llms={
    "default": GeneralLlm(
        model="openrouter/openai/gpt-4o",
        temperature=0.3,
        timeout=40,
        allowed_tries=2,
    ),
    "summarizer": "openrouter/openai/gpt-4o-mini",
    "researcher": "perplexity-agent",
    "parser": "openrouter/openai/gpt-4o-mini",
},
)

    # Per-mode tournament URL shown in the summary banner footer. These
    # piggyback on the forecasting_tools SDK constants and need updating
    # whenever those rotate seasons.
    TOURNAMENT_URLS = {
        "tournament": "https://www.metaculus.com/tournament/summer-futureeval-2026/",
        "metaculus_cup": "https://www.metaculus.com/tournament/metaculus-cup-summer-2025/",
        "test_questions": "https://www.metaculus.com/tournament/bot-testing-area/",
    }

    # Dispatch on mode. Each branch produces a list of ForecastReport (or
    # exceptions, since return_exceptions=True) which then flows into the
    # summary printers below.
    client = MetaculusClient()
    if run_mode == "tournament":
        seasonal_tournament_reports = asyncio.run(
            template_bot.forecast_on_tournament(
                33121, return_exceptions=True
            )
        )
        minibench_reports = asyncio.run(
            template_bot.forecast_on_tournament(
                client.CURRENT_MINIBENCH_ID, return_exceptions=True
            )
        )
        forecast_reports = seasonal_tournament_reports + minibench_reports
    elif run_mode == "metaculus_cup":
        # The Metaculus Cup may be uninitialized near the start of a season
        # (Jan/May/Sep). AXC_2025_TOURNAMENT_ID = 32564 and
        # AI_2027_TOURNAMENT_ID = "ai-2027" are also valid targets here.
        template_bot.skip_previously_forecasted_questions = False
        forecast_reports = asyncio.run(
            template_bot.forecast_on_tournament(
                client.CURRENT_METACULUS_CUP_ID, return_exceptions=True
            )
        )
    elif run_mode == "test_questions":
        # The bot-testing-area tournament contains all question types and is
        # the recommended target for smoke-testing your bot.
        # https://www.metaculus.com/tournament/bot-testing-area/
        template_bot.skip_previously_forecasted_questions = False
        forecast_reports = asyncio.run(
            template_bot.forecast_on_tournament(
                "bot-testing-area", return_exceptions=True
            )
        )

    template_bot.log_report_summary(forecast_reports)
    print_run_summary_banner(
        forecast_reports,
        will_publish=publish_to_metaculus,
        tournament_url=TOURNAMENT_URLS.get(run_mode),
    )
