"""Strictly opt-in, two-GET history audit for Bot Testing Area and Fall 2026 Cup.

Uses the existing offline-tested, fail-closed project-scoped inventory implementation.
This script NEVER sends forecasts, uses models, or prints raw question IDs/values.
Intentionally excludes earlier Cup seasons; do not infer global history from results.
"""
import argparse
import sys

import project_history_inventory_readonly as audited

TARGET_PROJECTS = (
    ("Bot testing area", "bot-testing-area"),
    ("Metaculus Cup Fall 2026", "33108"),
)


def configure():
    audited.PROJECTS = TARGET_PROJECTS
    audited.MAX_REQUESTS = 2
    if audited.ENDPOINT != "/api/questions/bulk-forecast-read/" or audited.BOT_ID != 307558:
        raise RuntimeError("Unexpected audit implementation; no requests allowed")
    if len(TARGET_PROJECTS) != 2:
        raise RuntimeError("Unexpected number of projects")


def self_check():
    configure()
    audited.self_check()
    for _, project in TARGET_PROJECTS:
        req = audited.build_request(project, "a" * 48)
        if req.get_method() != "GET":
            raise RuntimeError("Only GET is permitted")
    print("Offline: exactly two project GET targets, no live credentials or network")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    one = parser.add_mutually_exclusive_group(required=True)
    one.add_argument("--self-check", action="store_true")
    one.add_argument("--run-two-extra-project-gets", action="store_true")
    args = parser.parse_args()
    try:
        if args.self_check:
            self_check()
        else:
            configure()
            audited.run_live()  # Explicit opt-in only, min 6s gap, fail closed.
    except RuntimeError as err:
        print("Extra project inventory stopped safely: " + str(err), file=sys.stderr)
        sys.exit(1)
