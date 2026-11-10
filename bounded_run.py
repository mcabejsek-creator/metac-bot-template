"""POSIX CLI supervisor: hard wall clock, graceful TERM, then process-group KILL.

No SDK/model imports. Use from Actions inside Poetry's environment so the child
uses exactly the same interpreter and locked dependencies.
"""

import argparse
import os
import signal
import subprocess
import sys
import time

from runtime_limits import RuntimeLimits


def supervise(command, *, seconds=RuntimeLimits().run_seconds, grace=60):
    stop_signal = None

    def forward(signum, frame):
        nonlocal stop_signal
        stop_signal = signum

    def send(signum):
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            pass

    child, previous = None, {}
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, forward)
        started = time.monotonic()
        child = subprocess.Popen(command, start_new_session=True)
        while True:
            remaining = seconds - (time.monotonic() - started)
            if stop_signal is not None or remaining <= 0:
                status = 128 + stop_signal if stop_signal is not None else 124
                break
            try:
                return child.wait(timeout=min(remaining, .5))
            except subprocess.TimeoutExpired:
                pass
        # Repeated cancellation must not interrupt cleanup or orphan the child.
        for signum in previous:
            signal.signal(signum, signal.SIG_IGN)
        send(signal.SIGTERM)
        try:
            child.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
        # Kill remaining group members even if the leader has already exited.
        send(signal.SIGKILL)
        if child.poll() is None:
            child.wait()
        return status
    finally:
        if child is not None and child.poll() is None:
            send(signal.SIGKILL)
            child.wait()
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=RuntimeLimits().run_seconds,
                        help="Hard deadline in seconds; may only shorten the default run limit")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not 0 < args.seconds <= RuntimeLimits().run_seconds:
        parser.error("--seconds must be positive and no greater than the default run limit")
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("A child command is required")
    return supervise(command, seconds=args.seconds)


if __name__ == "__main__":
    sys.exit(main())

