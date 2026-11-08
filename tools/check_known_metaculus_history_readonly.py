"""Explicitly approved one-GET Metaculus history check; never publishes."""

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metaculus_one_post_history_readonly import (
    HistoryEvidenceUnavailable, SinglePostHistoryReader,
)

APPROVAL = "YES_ONE_HISTORY_GET"


def run(*, env=None, reader=None):
    vars = os.environ if env is None else env
    if vars.get("VRCHCAB_ONE_HISTORY_GET_APPROVAL") != APPROVAL:
        raise HistoryEvidenceUnavailable("Explicit single post history GET approval required")
    service = reader if reader is not None else SinglePostHistoryReader(
        token=vars.get("METACULUS_TOKEN_V2"),
    )
    return service.read().public_report()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--one-approved-get", action="store_true")
    args = parser.parse_args(argv)
    if not args.one_approved_get:
        parser.error("Manual approval and --one-approved-get required")
    try:
        report = run()
    except HistoryEvidenceUnavailable:
        # Do not expose HTTP errors or any unredacted remote response.
        print(json.dumps({
            "mode": "one_post_bot_history_get_only",
            "status": "not_verified",
            "all_historic_forecasts_reconciled": False,
            "github_stuck_run_reconciled": False,
            "publication_authorized": False,
        }, sort_keys=True))
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
