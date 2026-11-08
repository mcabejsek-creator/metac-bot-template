"""Opt-in public-post preview from TWO already-completed tournament scans.

This is a presentation adapter around the existing deterministic, fail-closed
automatic_one_selection selector. It does not fetch anything itself, does NOT
run research, does NOT publish, and does NOT authorize later publication.
No internal question ID, title, history or forecast values are exported.
"""

from automatic_one_selection import (
    UnsafeAutomaticInventory, select_one_automatic_candidate,
)
from question_context import effective_close, utc_date


class OperatorCandidateUnavailable(RuntimeError):
    """Never return an unverified or partial recommended post."""


def preview_operator_candidate(seasonal, minibench, *, now):
    """At most ONE public post ID, only after both full scans have succeeded."""
    season, mini = select_one_automatic_candidate(seasonal, minibench, now=now)
    if len(season) + len(mini) > 1:
        raise OperatorCandidateUnavailable("Selector returned multiple candidates")
    result = {
        "candidate_found": bool(season or mini),
        "both_tournament_inventories_required": True,
        "operator_must_reverify_before_any_live_post": True,
        "published_by_this_check": False,
        "model_calls": 0,
        "post_requests": 0,
        "publication_authorized": False,
    }
    if not season and not mini:
        return result
    label = "Fall FutureEval 2026" if season else "MiniBench"
    selected = season[0] if season else mini[0]
    post_id = selected.id_of_post
    if type(post_id) is not int or post_id <= 0:
        raise OperatorCandidateUnavailable("Selected public post identity invalid")
    close = utc_date(effective_close(selected))
    if close is None:
        raise OperatorCandidateUnavailable("Selected candidate deadline unverified")
    result.update({
        "public_post_id": post_id,
        "public_question_url": f"https://www.metaculus.com/questions/{post_id}/",
        "competition": label,
        "question_type": selected.question_type,
        "effective_close_utc": close.isoformat().replace("+00:00", "Z"),
    })
    return result
