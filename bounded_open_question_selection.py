"""Bounded, fail-closed tournament post pagination for the forecasting publisher.

No POST or model access here. Parsing delegates to the pinned forecasting-tools
question parser, preserving group-question semantics and forecast history.
Nothing in this module runs automatically when imported.
"""
from datetime import datetime, timezone
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from question_context import effective_close, utc_date

API_BASE = "https://www.metaculus.com/api"
PAGE_SIZE = 100
MAX_PAGES_PER_TOURNAMENT = 5
MIN_GET_SPACING_SECONDS = 6.0
MAX_RESPONSE_BYTES = 15_000_000
SUPPORTED_TOURNAMENTS = (33121, "minibench")


class IncompleteQuestionScan(RuntimeError):
    """A partial inventory must never be handed to the publisher."""


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise IncompleteQuestionScan("Metaculus redirect rejected")


class BoundedPostReader:
    """Authenticated GET only: bounded payload, no redirects and no retries."""

    def __init__(self, client, *, clock=time.monotonic, sleep=time.sleep, opener=None):
        if getattr(client, "acting_user", None) is not None:
            raise IncompleteQuestionScan("Acting-user pagination not implemented safely")
        if getattr(client, "base_url", "").rstrip("/") != API_BASE:
            raise IncompleteQuestionScan("Unexpected Metaculus API host")
        token = getattr(client, "token", None)
        if not isinstance(token, str) or not token.strip():
            raise IncompleteQuestionScan("Missing Metaculus API credential")
        self._token = token.removeprefix("Token ").strip()
        self._clock, self._sleep = clock, sleep
        self._opener = opener or urllib.request.build_opener(_RejectRedirects())
        self._previous_start = None

    def __call__(self, tournament, offset):
        if tournament not in SUPPORTED_TOURNAMENTS:
            raise IncompleteQuestionScan("Unexpected tournament")
        if type(offset) is not int or offset < 0 or offset % PAGE_SIZE:
            raise IncompleteQuestionScan("Unexpected pagination offset")
        if self._previous_start is not None:
            gap = MIN_GET_SPACING_SECONDS - (self._clock() - self._previous_start)
            if gap > 0:
                self._sleep(gap)
        self._previous_start = self._clock()
        params = urllib.parse.urlencode((
            ("limit", str(PAGE_SIZE)), ("offset", str(offset)),
            ("order_by", "-published_time"), ("with_cp", "true"),
            ("include_conditional_cps", "true"), ("statuses", "open"),
            ("tournaments", str(tournament)),
        ))
        req = urllib.request.Request(
            API_BASE + "/posts/?" + params, method="GET",
            headers={"Authorization": "Token " + self._token,
                     "Accept": "application/json"},
        )
        try:
            with self._opener.open(req, timeout=18) as response:
                if response.status != 200:
                    raise IncompleteQuestionScan("Unexpected API status")
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise IncompleteQuestionScan("Response too large")
                data = json.loads(body)
        except urllib.error.HTTPError as exc:
            raise IncompleteQuestionScan(
                "Rate limited; no retry" if exc.code == 429 else "Metaculus GET failed"
            ) from None
        except (urllib.error.URLError, OSError, ValueError, UnicodeError) as exc:
            raise IncompleteQuestionScan("Metaculus GET/JSON failed") from None
        if not isinstance(data, dict) or "next" not in data:
            raise IncompleteQuestionScan("Unknown pagination envelope")
        rows = data.get("results")
        if not isinstance(rows, list) or len(rows) > PAGE_SIZE or any(
            not isinstance(row, dict) for row in rows
        ):
            raise IncompleteQuestionScan("Invalid post page")
        if data["next"] is not None and not isinstance(data["next"], str):
            raise IncompleteQuestionScan("Invalid next marker")
        return rows, bool(data["next"])


def _validate_history(post):
    if "notebook" in post:
        return False
    if "group_of_questions" in post:
        group = post["group_of_questions"]
        leaves = group.get("questions") if isinstance(group, dict) else None
    elif "conditional" in post:
        leaves = [post["conditional"]]
    else:
        leaves = [post.get("question")]
    if not isinstance(leaves, list) or not leaves:
        raise IncompleteQuestionScan("Missing forecastable subquestions")
    for leaf in leaves:
        if not isinstance(leaf, dict) or leaf.get("status") is None:
            raise IncompleteQuestionScan("Malformed question state")
        if leaf["status"] != "open":
            continue
        info = leaf.get("my_forecasts")
        if not isinstance(info, dict) or not isinstance(info.get("history"), list):
            raise IncompleteQuestionScan("Missing authoritative own-forecast history")
    return True


def _is_eligible(question, now):
    state = getattr(question, "state", None)
    state = getattr(state, "value", state)
    if state != "open":
        return False
    if not isinstance(getattr(question, "id_of_question", None), int):
        raise IncompleteQuestionScan("Missing subquestion ID")
    if not isinstance(getattr(question, "already_forecasted", None), bool):
        raise IncompleteQuestionScan("Unknown existing-forecast status")
    start = utc_date(getattr(question, "open_time", None))
    close = effective_close(question)
    if start is None or close is None:
        raise IncompleteQuestionScan("Missing question open/close time")
    return start <= now < close


def scan_open_tournament(client, tournament, *, read_page, now=None, max_pages=MAX_PAGES_PER_TOURNAMENT):
    """Return complete validated available questions or raise, never partial data.

    The CountlessLimitOffsetPagination API advertises 'next' beyond the end
    (Django count=infinity). Two consecutive raw empty pages establish the
    observed terminal condition; one empty page is NOT terminal, so sparse
    anomalies do not immediately hide later offset pages. A fully occupied
    page budget without an observed end is a hard failure.
    """
    if tournament not in SUPPORTED_TOURNAMENTS:
        raise IncompleteQuestionScan("Unsupported tournament")
    if type(max_pages) is not int or not 1 <= max_pages <= MAX_PAGES_PER_TOURNAMENT:
        raise IncompleteQuestionScan("Invalid GET budget")
    now = now or datetime.now(timezone.utc)
    now = utc_date(now)
    found = {}
    consecutive_empty_pages = 0
    for page in range(max_pages):
        rows, more = read_page(tournament, page * PAGE_SIZE)
        if not isinstance(rows, list) or type(more) is not bool:
            raise IncompleteQuestionScan("Invalid page transport response")
        if len(rows) > PAGE_SIZE or any(not isinstance(post, dict) for post in rows):
            raise IncompleteQuestionScan("Invalid post page")
        consecutive_empty_pages = consecutive_empty_pages + 1 if not rows else 0
        for post in rows:
            if not _validate_history(post):
                continue
            try:
                questions = client._post_json_to_questions_while_handling_groups(
                    post, "unpack_subquestions"
                )
            except Exception:
                raise IncompleteQuestionScan("SDK failed to parse a post") from None
            if not isinstance(questions, list):
                raise IncompleteQuestionScan("Unexpected SDK parser result")
            for question in questions:
                if not _is_eligible(question, now):
                    continue
                qid = question.id_of_question
                if qid in found:
                    if found[qid].already_forecasted != question.already_forecasted:
                        raise IncompleteQuestionScan("Conflicting question history across pages")
                    continue
                found[qid] = question
        if not more or consecutive_empty_pages >= 2:
            return list(found.values())
    raise IncompleteQuestionScan("Pagination did not reach two empty pages within fixed GET cap")


def deduplicate_tournaments(first, second):
    """Preserve original tournament order; reject inconsistent cross-list history."""
    seen = {q.id_of_question: q for q in first}
    other = []
    for question in second:
        old = seen.get(question.id_of_question)
        if old is not None:
            if old.already_forecasted != question.already_forecasted:
                raise IncompleteQuestionScan("Conflicting cross-tournament history")
        else:
            seen[question.id_of_question] = question
            other.append(question)
    return first, other
