"""EXPLICITLY approved, bounded GitHub Actions GET-only diagnostic.

No scheduler, no Metaculus API, no token printed, no GitHub writes.
An empty queue report is NOT authorization to publish a forecast.
"""

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from github_publisher_queue_readonly import (
    GitHubPublisherQueueReader, GitHubRunEvidenceUnavailable,
)


APPROVAL = "YES_READ_ONLY_GITHUB_QUEUE"


def format_result(evidence):
    """Return safe public diagnostic fields (never a credential)."""
    return {
        "mode": "github_publisher_queue_get_only",
        "status": "no_current_publishers_observed" if evidence.clear_to_consider_dispatch else "blocked_by_publisher_queue",
        "checked_at_utc": evidence.checked_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "active_publisher_runs": evidence.active_publisher_runs,
        "queued_publisher_runs": evidence.queued_publisher_runs,
        "publishing_run_ids": list(evidence.publisher_run_ids),
        "known_stuck_run_34463156990_queued": evidence.known_stuck_queued,
        "automatic_publication_authorized": False,
    }


def run(*, env=None, reader=None):
    """Do not contact GitHub unless the EXACT explicit approval is present."""
    variables = env if env is not None else os.environ
    if variables.get("VRCHCAB_GITHUB_QUEUE_READ_APPROVAL") != APPROVAL:
        raise GitHubRunEvidenceUnavailable("Read-only GitHub diagnostic not approved")
    chosen = reader
    if chosen is None:
        chosen = GitHubPublisherQueueReader(token=variables.get("GITHUB_TOKEN"))
    return format_result(chosen.read())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-approved-readonly", action="store_true")
    args = parser.parse_args(argv)
    if not args.check_approved_readonly:
        parser.error("Diagnostic is opt-in; use --check-approved-readonly")
    try:
        result = run()
    except GitHubRunEvidenceUnavailable:
        print(json.dumps({
            "mode": "github_publisher_queue_get_only",
            "status": "not_verified",
            "automatic_publication_authorized": False,
        }, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
