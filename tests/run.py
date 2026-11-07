"""Run with: .venv/bin/python -B -m tests.run [--strict-known-bugs]."""

import argparse
import json
import signal
import sys
import unittest
from pathlib import Path

from . import safety
from .support import KnownBugAssertion


class TestWatchdogExpired(BaseException):
    """Cannot be mistaken for an application's retryable TimeoutError."""


class OfflineResult(unittest.TextTestResult):
    """Only a deliberate contract assertion can be an expected known failure."""

    strict_known_bugs = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.passed = []

    def startTest(self, test):
        super().startTest(test)
        self._attempt_count = len(safety.unexpected_attempts)
        if hasattr(signal, "setitimer"):
            signal.signal(signal.SIGALRM, self._timeout)
            signal.setitimer(signal.ITIMER_REAL, 10)

    @staticmethod
    def _timeout(signum, frame):
        raise TestWatchdogExpired("Offline test exceeded 10-second harness watchdog")

    def stopTest(self, test):
        if hasattr(signal, "setitimer"):
            signal.setitimer(signal.ITIMER_REAL, 0)
        if len(safety.unexpected_attempts) != self._attempt_count:
            exc = safety.OfflineViolation("Test attempted an unmocked live boundary")
            self.addError(test, (type(exc), exc, None))
        super().stopTest(test)

    @staticmethod
    def bug(test):
        return getattr(getattr(test, test._testMethodName), "known_bug", None)

    def addFailure(self, test, err):
        if self.bug(test) and err[0] is KnownBugAssertion and not self.strict_known_bugs:
            self.addExpectedFailure(test, err)
        else:
            super().addFailure(test, err)

    def addSuccess(self, test):
        if self.bug(test):
            # A fix must explicitly promote its regression test to a passing test.
            self.addUnexpectedSuccess(test)
        else:
            self.passed.append(test.id())
            super().addSuccess(test)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict-known-bugs", action="store_true")
    parser.add_argument("--report", type=Path, help="Write machine-readable results to this local JSON path")
    args = parser.parse_args()
    OfflineResult.strict_known_bugs = args.strict_known_bugs
    root = Path(__file__).resolve().parents[1]
    suite = unittest.defaultTestLoader.discover(str(root / "tests"), top_level_dir=str(root))
    result = unittest.TextTestRunner(verbosity=2, resultclass=OfflineResult).run(suite)
    print(f"\nOffline guard: {len(safety.unexpected_attempts)} unexpected live attempts.")
    for test, _ in result.expectedFailures:
        print(f"KNOWN BUG {OfflineResult.bug(test)}: {test.id()}")
    if safety.unexpected_attempts:
        print("Blocked boundaries:", sorted(set(safety.unexpected_attempts)))
    if args.report:
        args.report.write_text(json.dumps({
            "tests_run": result.testsRun,
            "passed": result.passed,
            "known_failures": [{"test": test.id(), "issue": OfflineResult.bug(test)} for test, _ in result.expectedFailures],
            "failures": [test.id() for test, _ in result.failures],
            "errors": [test.id() for test, _ in result.errors],
            "unexpected_successes": [test.id() for test in result.unexpectedSuccesses],
            "skipped": [test.id() for test, _ in result.skipped],
            "unexpected_live_attempts": safety.unexpected_attempts,
        }, indent=2) + "\n")
    return 0 if result.wasSuccessful() and not safety.unexpected_attempts else 1


if __name__ == "__main__":
    sys.exit(main())
