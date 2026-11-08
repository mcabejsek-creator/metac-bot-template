"""Manual, opt-in and strictly read-only FutureEval/MiniBench inventory preflight.

Never imports the application bot, opens a publication journal, invokes an LLM,
posts a forecast, or emits question IDs, private history, titles, or credentials.

This program NEVER runs via a schedule and has no network activity at import time.
The calling workflow must explicitly supply the V2 token and an approval input.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from forecasting_tools import BinaryQuestion

# Running as a script from tools/ must still import the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bounded_open_question_selection import (
    BoundedPostReader,
    IncompleteQuestionScan,
    MAX_PAGES_PER_TOURNAMENT,
    deduplicate_tournaments,
    scan_open_tournament,
)
from question_context import effective_close, utc_date

FUTUREEVAL = 33121
MINIBENCH = "minibench"
MAX_TOTAL_GETS = MAX_PAGES_PER_TOURNAMENT * 2


class RequestBudget:
    """One shared budget across BOTH competition scans, never reset on failure."""

    def __init__(self, reader):
        self.reader = reader
        self.calls = 0

    def __call__(self, tournament, offset):
        if self.calls >= MAX_TOTAL_GETS:
            raise IncompleteQuestionScan("Manual preflight exceeded aggregate GET cap")
        self.calls += 1  # counted before any possible request or error
        return self.reader(tournament, offset)


def _summary(questions, now):
    unforecasted = [question for question in questions if question.already_forecasted is False]
    return {
        "open_with_valid_timing_and_history": len(questions),
        "previously_forecasted_in_token_history": len(questions) - len(unforecasted),
        "no_forecast_in_token_history": len(unforecasted),
        "unforecasted_closing_within_24_hours": sum(
            0 <= (effective_close(question) - now).total_seconds() <= 86400
            for question in unforecasted
        ),
    }


def _candidate_check(first, second, candidate_id):
    """Confirm an operator-supplied ID without disclosing the ID or forecast history.

    This is an optional snapshot-only check. It must not be mistaken for
    tournament eligibility, a full historic reconciliation, or live POST approval.
    """
    if type(candidate_id) is not int or candidate_id <= 0:
        raise IncompleteQuestionScan("Invalid operator-supplied candidate ID")
    matches = [q for q in (*first, *second) if q.id_of_question == candidate_id]
    matched = len(matches) == 1
    q = matches[0] if matched else None
    eligible_binary = (
        matched
        and isinstance(q, BinaryQuestion)
        and q.question_type == "binary"
        and q.already_forecasted is False
    )
    return {
        "present_once_in_validated_open_inventory": matched,
        "binary_with_empty_token_forecast_history_in_snapshot": bool(eligible_binary),
        "historical_outcomes_fully_reconciled": False,
        "competition_participation_verified": False,
        "publish_time_status_verified": False,
        "live_publication_authorized": False,
        "operator_supplied_question_id_not_logged": True,
    }


def inspect(client, read_page, *, now=None, candidate_id=None):
    """Run full bounded scans before reporting any partial results."""
    if candidate_id is not None and (type(candidate_id) is not int or candidate_id <= 0):
        raise IncompleteQuestionScan("Invalid operator-supplied candidate ID")
    timestamp = utc_date(now or datetime.now(timezone.utc))
    budget = RequestBudget(read_page)
    first = scan_open_tournament(client, FUTUREEVAL, read_page=budget, now=timestamp)
    second = scan_open_tournament(client, MINIBENCH, read_page=budget, now=timestamp)
    first, second = deduplicate_tournaments(first, second)
    report = {
        "mode": "strictly_read_only_manual_preflight",
        "snapshot_utc": timestamp.isoformat(),
        "completed_both_inventories": True,
        "tournament_participation_eligibility_verified": False,
        "authenticated_get_attempts": budget.calls,
        "maximum_get_attempts": MAX_TOTAL_GETS,
        "model_calls": 0,
        "post_requests": 0,
        "writes_to_publication_journal": 0,
        "private_ids_titles_history_not_logged": True,
        "forecasts_not_published": True,
        "competition_counts": {
            "Fall FutureEval 2026": _summary(first, timestamp),
            "MiniBench": _summary(second, timestamp),
        },
        "limitations": (
            "Only open status, validated timing and authenticated token history are checked; "
            "tournament participation and publishing eligibility are NOT established. "
            "Questions may open, close, or change history after this snapshot. "
            "No actual model/publication/checkpoint end-to-end validation is performed."
        ),
    }
    if candidate_id is not None:
        report["operator_selected_candidate"] = _candidate_check(first, second, candidate_id)
    return report


def self_check():
    # Synthetic zero-network regression of both-phase scan, prior-history skip,
    # cross-tournament dedup and the request budget.
    now = datetime(2026, 11, 8, 4, 0, tzinfo=timezone.utc)

    def q(qid, already=False, close_soon=False):
        return SimpleNamespace(
            state="open",
            id_of_question=qid,
            already_forecasted=already,
            open_time=now - timedelta(days=1),
            close_time=now + (timedelta(hours=2) if close_soon else timedelta(days=2)),
            api_json={"question": {}},
        )

    def post(question):
        return {
            "question": {
                "status": "open",
                "my_forecasts": {"history": [{"synthetic": True}]
                                 if question.already_forecasted else []},
            },
            "_synthetic_object": question,
        }

    class FakeClient:
        def _post_json_to_questions_while_handling_groups(self, row, mode):
            assert mode == "unpack_subquestions"
            return [row["_synthetic_object"]]

    reads = []
    pattern = {
        (FUTUREEVAL, 0): ([post(q(111, close_soon=True)), post(q(112, already=True))], True),
        (FUTUREEVAL, 100): ([], True),
        (FUTUREEVAL, 200): ([], True),
        (MINIBENCH, 0): ([post(q(111, close_soon=True)), post(q(113))], True),
        (MINIBENCH, 100): ([], True),
        (MINIBENCH, 200): ([], True),
    }

    def reader(tournament, offset):
        reads.append((tournament, offset))
        return pattern[(tournament, offset)]

    report = inspect(FakeClient(), reader, now=now)
    assert report["authenticated_get_attempts"] == 6
    assert len(reads) == 6
    a = report["competition_counts"]["Fall FutureEval 2026"]
    b = report["competition_counts"]["MiniBench"]
    assert (a["open_with_valid_timing_and_history"], a["previously_forecasted_in_token_history"],
            a["no_forecast_in_token_history"], a["unforecasted_closing_within_24_hours"]) == (2, 1, 1, 1)
    assert (b["open_with_valid_timing_and_history"], b["previously_forecasted_in_token_history"],
            b["no_forecast_in_token_history"]) == (1, 0, 1)
    serialized = json.dumps(report)
    assert "synthetic" not in serialized and '"111"' not in serialized
    assert '"112"' not in serialized and '"113"' not in serialized
    assert report["tournament_participation_eligibility_verified"] is False
    # The opt-in candidate check never exposes the user-supplied ID.
    with_candidate = inspect(FakeClient(), reader, now=now, candidate_id=111)
    check = with_candidate["operator_selected_candidate"]
    assert check["present_once_in_validated_open_inventory"] is True
    assert check["binary_with_empty_token_forecast_history_in_snapshot"] is False
    assert check["live_publication_authorized"] is False
    assert "111" not in json.dumps(with_candidate)
    assert with_candidate["authenticated_get_attempts"] == 6
    try:
        inspect(FakeClient(), reader, now=now, candidate_id=-1)
    except IncompleteQuestionScan:
        pass
    else:
        raise AssertionError("Invalid candidate ID did not fail closed")

    # A failure while scanning the SECOND competition must not emit a partial
    # successful inventory, and no extra network attempt may be retried.
    calls_on_failure = []
    def broken_second(tournament, offset):
        calls_on_failure.append((tournament, offset))
        if tournament == MINIBENCH:
            raise IncompleteQuestionScan("Synthetic second-project failure")
        return pattern[(tournament, offset)]
    try:
        inspect(FakeClient(), broken_second, now=now)
    except IncompleteQuestionScan:
        pass
    else:
        raise AssertionError("Incomplete second inventory was treated as success")
    assert calls_on_failure == [
        (FUTUREEVAL, 0), (FUTUREEVAL, 100), (FUTUREEVAL, 200),
        (MINIBENCH, 0),
    ]

    # Enforce one global 10-GET cap in a purely in-memory transport.
    bounded = RequestBudget(lambda *_: ([], True))
    for index in range(MAX_TOTAL_GETS):
        bounded(FUTUREEVAL, index * 100)
    try:
        bounded(FUTUREEVAL, MAX_TOTAL_GETS * 100)
    except IncompleteQuestionScan:
        pass
    else:
        raise AssertionError("Request budget allowed an 11th GET")
    assert bounded.calls == MAX_TOTAL_GETS
    print("Offline manual-preflight self-check passed; no network, no secrets, no model or POST", flush=True)


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--self-check", action="store_true")
    mode.add_argument("--run-approved-readonly", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    # The real path runs only when invoked explicitly by the manual workflow.
    # No bypass via importing the module (main guard below).
    if os.getenv("VRCHCAB_READONLY_PREFLIGHT_APPROVED") != "YES_READ_ONLY_MAX_TEN_GETS":
        print("Preflight stopped: explicit approval marker required", file=sys.stderr)
        sys.exit(2)
    token = os.environ.get("METACULUS_TOKEN", "")
    if not token or len(token.strip()) < 32 or any(c.isspace() for c in token.strip()):
        print("Preflight stopped: missing or malformed credential", file=sys.stderr)
        sys.exit(2)
    try:
        # Optional operator input, never emitted to logs. It does not authorize
        # an API request; the existing explicit GET approval is still mandatory.
        raw_candidate = os.getenv("VRCHCAB_CANDIDATE_QUESTION_ID", "").strip()
        candidate_id = None
        if raw_candidate:
            if not raw_candidate.isascii() or not raw_candidate.isdecimal() or int(raw_candidate) <= 0:
                raise IncompleteQuestionScan("Invalid candidate input")
            candidate_id = int(raw_candidate)
        from forecasting_tools import MetaculusClient
        client = MetaculusClient(token=token.strip().removeprefix("Token ").strip())
        reader = BoundedPostReader(client)
        print("Starting approved GET-only, at-most-ten-request manual preflight", flush=True)
        report = inspect(client, reader, candidate_id=candidate_id)
        print(json.dumps(report, sort_keys=True), flush=True)
    except Exception:
        # Never log raw API payloads, question titles, URLs, tokens or private history.
        print("READ_ONLY_PREFLIGHT_FAILED_CLOSED (details suppressed)", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
