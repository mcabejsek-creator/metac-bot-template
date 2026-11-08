"""One-post authenticated Metaculus OWN forecast history, bounded GET only.

Historical user-authorized first live forecast target: public post #46347.
No discovery or arbitrary post ID is accepted here. One authenticated GET,
no POST, no model calls, no journal or GitHub writes, no fallback account.
Even seeing a forecast does NOT settle all old history or old GitHub runs.
All modules in this PR are dormant until separately approved manual workflow.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import urllib.error
import urllib.request

POST_ID = 46347
URL = f"https://www.metaculus.com/api/posts/{POST_ID}/"
MAX_BYTES = 1_000_000


class HistoryEvidenceUnavailable(RuntimeError):
    """Never infer a definite no-publication outcome from incomplete API data."""


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise HistoryEvidenceUnavailable("Metaculus API redirect not accepted")


@dataclass(frozen=True)
class PostHistoryEvidence:
    checked_at: datetime
    forecast_present_in_token_history: bool
    forecast_history_count: int

    def public_report(self):
        return {
            "mode": "one_post_bot_history_get_only",
            "scope": "single_previously_authorized_first_live_post",
            "checked_at_utc": self.checked_at.isoformat().replace("+00:00", "Z"),
            "forecast_present_in_token_history": self.forecast_present_in_token_history,
            "observed_forecast_history_records": self.forecast_history_count,
            "all_historic_forecasts_reconciled": False,
            "github_stuck_run_reconciled": False,
            "absence_is_proof_of_no_prior_post": False,
            "publication_authorized": False,
            "model_calls": 0,
            "metaculus_post_requests": 0,
            "github_writes": 0,
            "authenticated_metaculus_get_attempts": 1,
        }


class SinglePostHistoryReader:
    """Exact post GET; no retries or redirects; no other credentials accepted."""

    def __init__(self, *, token, opener=None, clock=None):
        if not isinstance(token, str) or not token or token.strip() != token:
            raise HistoryEvidenceUnavailable("Metaculus V2 token required")
        if any(ch.isspace() for ch in token):
            raise HistoryEvidenceUnavailable("Invalid Metaculus V2 token format")
        self._token = token
        self._opener = opener if opener is not None else urllib.request.build_opener(_RejectRedirects())
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)

    def read(self):
        req = urllib.request.Request(
            URL,
            method="GET",
            headers={
                "Authorization": "Token " + self._token,
                "Accept": "application/json",
                "User-Agent": "vrchcab-single-post-history-readonly",
            },
        )
        try:
            with self._opener.open(req, timeout=15) as response:
                if response.status != 200:
                    raise HistoryEvidenceUnavailable("Unexpected Metaculus history response")
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise HistoryEvidenceUnavailable("Metaculus history response too large")
        except HistoryEvidenceUnavailable:
            raise
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, TimeoutError):
            raise HistoryEvidenceUnavailable("Metaculus history GET failed; no retries") from None
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            raise HistoryEvidenceUnavailable("Invalid Metaculus history response") from None
        if not isinstance(payload, dict) or type(payload.get("id")) is not int or payload["id"] != POST_ID:
            raise HistoryEvidenceUnavailable("Unexpected Metaculus post identity")
        # Official API may supply keys with null values for restricted fields.
        if any(payload.get(k) is not None for k in ("group_of_questions", "conditional", "notebook")):
            raise HistoryEvidenceUnavailable("Requested post is not verified atomic")
        question = payload.get("question")
        if not isinstance(question, dict) or type(question.get("id")) is not int or question["id"] <= 0:
            raise HistoryEvidenceUnavailable("Missing verified atomic question")
        my = question.get("my_forecasts")
        if not isinstance(my, dict) or not isinstance(my.get("history"), list):
            raise HistoryEvidenceUnavailable("Missing authenticated own-forecast history")
        history = my["history"]
        if not all(isinstance(h, dict) and
                   (h.get("question_id") is None or
                    (type(h.get("question_id")) is int and h["question_id"] == question["id"]))
                   for h in history):
            raise HistoryEvidenceUnavailable("Malformed or cross-question history")
        latest = my.get("latest")
        if latest is not None and not isinstance(latest, dict):
            raise HistoryEvidenceUnavailable("Malformed latest forecast")
        if latest is not None and len(history) == 0:
            raise HistoryEvidenceUnavailable("Latest/history inconsistency")
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise HistoryEvidenceUnavailable("Read-only audit timestamp unavailable")
        return PostHistoryEvidence(
            checked_at=now.astimezone(timezone.utc),
            forecast_present_in_token_history=len(history) > 0,
            forecast_history_count=len(history),
        )
