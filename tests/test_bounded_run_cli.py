"""Real subprocess checks for the shorter unattended supervisor deadline."""

import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BoundedRunCliTests(unittest.TestCase):
    def run_cli(self, *arguments):
        return subprocess.run(
            [sys.executable, "-B", "-m", "bounded_run", *arguments],
            cwd=ROOT, capture_output=True, text=True, timeout=5,
        )

    def test_short_limit_preserves_child_python_flags(self):
        result = self.run_cli("--seconds", "2", "--", sys.executable, "-B", "-c",
                              "print('SHORT_LIMIT_CHILD_EXECUTED')")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SHORT_LIMIT_CHILD_EXECUTED", result.stdout)

    def test_deadline_terminates_waiting_child(self):
        result = self.run_cli("--seconds", "0.1", "--", sys.executable, "-B", "-c",
                              "import time; time.sleep(30)")
        self.assertEqual(result.returncode, 124, result.stderr)

    def test_invalid_or_longer_deadline_never_starts_child(self):
        for value in ("0", "-1", "nan", "inf", "10801"):
            with self.subTest(seconds=value):
                result = self.run_cli("--seconds", value, "--", sys.executable, "-B", "-c",
                                      "print('MUST_NOT_RUN')")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn("MUST_NOT_RUN", result.stdout)


if __name__ == "__main__":
    unittest.main()
