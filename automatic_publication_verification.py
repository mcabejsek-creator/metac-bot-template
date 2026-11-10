"""One authenticated readback after a new automatic V2 forecast.

This only observes Metaculus; failure never clears a journal claim or retries
publication. The caller establishes an empty own history before forecasting.
"""

import json
import os
import urllib.error
import urllib.request

from runtime_limits import check_blocking_cancelled


MAX_RESPONSE_BYTES = 1_000_000


class PublicationVerificationUnavailable(RuntimeError):
    """The forecast may exist; missing readback evidence is not permission to resend."""


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise PublicationVerificationUnavailable("Metaculus readback redirect rejected")


def _same_question(record, question_id):
    return (isinstance(record, dict) and bool(record)
            and (record.get("question_id") is None
                 or (type(record["question_id"]) is int and record["question_id"] == question_id)))


def record_verified_publication(evidence, *, environ=None):
    """Allow an immediate successor only after the authenticated readback.

    This emits a fixed, non-sensitive GitHub step output. It never requests
    work itself; failed/uncertain publications cannot produce this signal.
    """
    if (not isinstance(evidence, dict) or any(
            type(evidence.get(field)) is not int or evidence[field] <= 0
            for field in ("post_id", "question_id", "own_forecast_count"))):
        raise PublicationVerificationUnavailable("Verified publication evidence is required")
    environ = os.environ if environ is None else environ
    path = environ.get("GITHUB_OUTPUT")
    if environ.get("GITHUB_ACTIONS") == "true" and path:
        with open(path, "a", encoding="utf-8") as output:
            output.write("verified_forecasts=1\n")


def verify_published_question(question):
    """Return public identity/count evidence from exactly one GET, without retry."""
    post_id, question_id = (getattr(question, field, None) for field in ("id_of_post", "id_of_question"))
    if any(type(value) is not int or value <= 0 for value in (post_id, question_id)):
        raise PublicationVerificationUnavailable("Readback needs exact positive post and question IDs")
    token = os.environ.get("METACULUS_TOKEN", "")
    if not token or any(ch.isspace() for ch in token):
        raise PublicationVerificationUnavailable("Metaculus readback credential unavailable")
    request = urllib.request.Request(
        f"https://www.metaculus.com/api/posts/{post_id}/", method="GET",
        headers={"Authorization": "Token " + token, "Accept": "application/json",
                 "Cache-Control": "no-cache", "User-Agent": "vrchcab-automatic-publication-readback"},
    )
    check_blocking_cancelled()
    try:
        opener = urllib.request.build_opener(_RejectRedirects())
        with opener.open(request, timeout=15) as response:
            if response.status != 200:
                raise PublicationVerificationUnavailable("Metaculus readback did not return success")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise PublicationVerificationUnavailable("Metaculus readback exceeds size limit")
    except PublicationVerificationUnavailable:
        raise
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, TimeoutError):
        raise PublicationVerificationUnavailable("Metaculus readback failed; publication must not be retried") from None
    try:
        post = json.loads(raw)
    except (ValueError, UnicodeError):
        raise PublicationVerificationUnavailable("Malformed Metaculus readback JSON") from None
    if not isinstance(post, dict) or type(post.get("id")) is not int or post["id"] != post_id:
        raise PublicationVerificationUnavailable("Metaculus readback post identity differs")
    if any(post.get(key) is not None for key in ("group_of_questions", "conditional", "notebook")):
        raise PublicationVerificationUnavailable("Metaculus readback is not an atomic question")
    detail = post.get("question")
    if not isinstance(detail, dict) or type(detail.get("id")) is not int or detail["id"] != question_id:
        raise PublicationVerificationUnavailable("Metaculus readback question identity differs")
    own = detail.get("my_forecasts")
    history = own.get("history") if isinstance(own, dict) else None
    if (not isinstance(history, list) or not history
            or not all(_same_question(record, question_id) for record in history)):
        raise PublicationVerificationUnavailable("New own forecast is not verified in Metaculus history")
    latest = own.get("latest")
    if latest is not None and not _same_question(latest, question_id):
        raise PublicationVerificationUnavailable("Latest own forecast has inconsistent identity")
    return {"post_id": post_id, "question_id": question_id, "own_forecast_count": len(history)}
