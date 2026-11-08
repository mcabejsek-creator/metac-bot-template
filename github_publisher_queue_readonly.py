"""Bounded authenticated GET-only evidence for GitHub publisher queue states.

NO GitHub workflows dispatch, no POST/PUT, no Metaculus, no paid models.
Unreliable/incomplete repository-run information always fails closed.
The September 2026 stuck queued publishing run is intentionally NOT excluded.
A read-only snapshot is not an atomic dispatch lock: use the existing
publication concurrency lock and durable intent CAS in any future publisher.
This module is deliberately NOT invoked by existing production workflows.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import urllib.error
import urllib.parse
import urllib.request

REPO = "mcabejsek-creator/metac-bot-template"
API_ROOT = f"https://api.github.com/repos/{REPO}"
PUBLISHING_WORKFLOW_ID = 347996133
KNOWN_STUCK_RUN_ID = 34463156990
SCANNED_STATES = ("in_progress", "queued", "requested", "waiting", "pending")
MAX_RUNS_PER_STATE = 100
MAX_BODY_BYTES = 131072
_SHA40 = re.compile(r"[a-f0-9]{40}")


class GitHubRunEvidenceUnavailable(RuntimeError):
    """Never claim zero runs on missing or unreliable GitHub API data."""


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise GitHubRunEvidenceUnavailable("GitHub run evidence redirected")


@dataclass(frozen=True)
class PublisherRunEvidence:
    checked_at: datetime
    active_publisher_runs: int
    queued_publisher_runs: int
    publisher_run_ids: tuple[int, ...]
    known_stuck_queued: bool

    def __post_init__(self):
        if not isinstance(self.checked_at, datetime) or self.checked_at.tzinfo is None:
            raise GitHubRunEvidenceUnavailable("Run evidence needs an aware UTC timestamp")
        if (type(self.active_publisher_runs) is not int
                or type(self.queued_publisher_runs) is not int
                or self.active_publisher_runs < 0
                or self.queued_publisher_runs < 0):
            raise GitHubRunEvidenceUnavailable("Invalid publisher run counts")

    @property
    def clear_to_consider_dispatch(self):
        return self.active_publisher_runs == 0 and self.queued_publisher_runs == 0


class GitHubPublisherQueueReader:
    """Use 5 allowlisted status-filtered GitHub GETs, no retries.

    Access token must be scoped to repository Actions READ; never log it.
    A paginated/partial/racing response is NOT trustworthy: fail closed.
    This source alone cannot activate anything.
    """

    def __init__(self, *, token, opener=None, clock=None):
        if not isinstance(token, str) or not token or token != token.strip():
            raise GitHubRunEvidenceUnavailable("GitHub Actions read credential required")
        self._token = token
        self._opener = opener if opener is not None else urllib.request.build_opener(_NoRedirects())
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)

    def _get_json(self, url):
        if url not in _STATUS_URLS.values() and url != _STUCK_RUN_URL:
            raise GitHubRunEvidenceUnavailable("GitHub URL not in fixed read allowlist")
        request = urllib.request.Request(
            url, method="GET",
            headers={
                "Authorization": "Bearer " + self._token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "vrchcab-publisher-status-readonly",
            },
        )
        try:
            with self._opener.open(request, timeout=12) as response:
                if response.status != 200:
                    raise GitHubRunEvidenceUnavailable("Unexpected GitHub Actions response")
                raw = response.read(MAX_BODY_BYTES + 1)
                if len(raw) > MAX_BODY_BYTES:
                    raise GitHubRunEvidenceUnavailable("GitHub Actions status body too large")
        except GitHubRunEvidenceUnavailable:
            raise
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, TimeoutError):
            raise GitHubRunEvidenceUnavailable("GitHub Actions GET failed; no retries") from None
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("Object required")
            return data
        except (ValueError, UnicodeError):
            raise GitHubRunEvidenceUnavailable("Malformed GitHub Actions status") from None

    def read(self):
        counted = set()
        active = set()
        queued = set()
        for requested_status in SCANNED_STATES:
            page = self._get_json(_STATUS_URLS[requested_status])
            amount = page.get("total_count")
            runs = page.get("workflow_runs")
            if (type(amount) is not int or amount < 0
                    or type(runs) is not list
                    or amount != len(runs) or len(runs) > MAX_RUNS_PER_STATE):
                # Includes mid-query list change, missing pagination and
                # counts >100. No false "zero queued" from partial pages.
                raise GitHubRunEvidenceUnavailable("GitHub run inventory incomplete")
            for run in runs:
                if not isinstance(run, dict):
                    raise GitHubRunEvidenceUnavailable("Malformed workflow run")
                ident = run.get("id")
                workflow = run.get("workflow_id")
                state = run.get("status")
                if (type(ident) is not int or ident <= 0
                        or type(workflow) is not int or workflow <= 0
                        or not isinstance(state, str) or state != requested_status
                        or ident in counted):
                    raise GitHubRunEvidenceUnavailable("Conflicting GitHub run snapshot")
                counted.add(ident)
                if workflow == PUBLISHING_WORKFLOW_ID:
                    if requested_status == "in_progress":
                        active.add(ident)
                    else:
                        queued.add(ident)

        stamped = self._clock()
        if (not isinstance(stamped, datetime) or stamped.tzinfo is None
                or stamped.utcoffset() is None):
            raise GitHubRunEvidenceUnavailable("GitHub evidence timestamp unknown")
        return PublisherRunEvidence(
            checked_at=stamped.astimezone(timezone.utc),
            active_publisher_runs=len(active),
            queued_publisher_runs=len(queued),
            publisher_run_ids=tuple(sorted(active | queued)),
            known_stuck_queued=KNOWN_STUCK_RUN_ID in queued,
        )

    def get_known_stuck_run(self):
        """OPTIONAL extra GET for support diagnostics, never a cancellation."""
        run = self._get_json(_STUCK_RUN_URL)
        if run.get("id") != KNOWN_STUCK_RUN_ID or type(run.get("status")) is not str:
            raise GitHubRunEvidenceUnavailable("Stuck-run API result mismatched")
        return {"run_id": KNOWN_STUCK_RUN_ID, "status": run["status"]}


_STATUS_URLS = {
    state: f"{API_ROOT}/actions/runs?status={state}&per_page={MAX_RUNS_PER_STATE}"
    for state in SCANNED_STATES
}
_STUCK_RUN_URL = f"{API_ROOT}/actions/runs/{KNOWN_STUCK_RUN_ID}"
