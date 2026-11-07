import io
import unittest
from unittest.mock import patch

from . import safety
from .run import OfflineResult
from .support import KnownBugAssertion, known_bug


class RunnerTests(unittest.TestCase):
    def run_probe(self, case):
        class ProbeResult(OfflineResult):
            strict_known_bugs = False

        with patch.object(safety, "unexpected_attempts", []):
            return unittest.TextTestRunner(stream=io.StringIO(), resultclass=ProbeResult).run(
                unittest.defaultTestLoader.loadTestsFromTestCase(case)
            )

    def test_only_explicit_contract_assertion_is_an_expected_failure(self):
        class Probe(unittest.TestCase):
            @known_bug("PROBE")
            def test_contract(self):
                raise KnownBugAssertion("Deliberate contract failure")

        result = self.run_probe(Probe)
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(len(result.expectedFailures), 1)

    def test_unexpected_exception_cannot_hide_as_a_known_bug(self):
        class Probe(unittest.TestCase):
            @known_bug("PROBE")
            def test_contract(self):
                raise RuntimeError("Unexpected setup/model error")

        result = self.run_probe(Probe)
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.expectedFailures, [])

    def test_unexpected_fix_requires_test_promotion(self):
        class Probe(unittest.TestCase):
            @known_bug("PROBE")
            def test_contract(self):
                pass

        result = self.run_probe(Probe)
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.unexpectedSuccesses), 1)

    def test_caught_guard_violation_still_fails_runner(self):
        class Probe(unittest.TestCase):
            @known_bug("PROBE")
            def test_contract(self):
                try:
                    safety.forbidden("probe.only")()
                except safety.OfflineViolation:
                    pass
                raise KnownBugAssertion("Must not conceal the safety violation")

        result = self.run_probe(Probe)
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.errors), 1)
