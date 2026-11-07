import json
import subprocess
import sys
import time
from pathlib import Path

from .support import OfflineTestCase


WORKER = """
import sys, time
from pathlib import Path
import tests  # Install network/credential guards before SDK import.
from tests import safety
from publication_journal import PublicationStore, PublicationUncertain, Publisher

database, ready, gate, output = map(Path, sys.argv[1:])
store = PublicationStore(database)
store.prepare('shared/initial', '{}', [{'kind': 'forecast'}, {'kind': 'comment'}])
ready.touch()
until = time.monotonic() + 5
while not gate.exists():
    if time.monotonic() > until:
        raise RuntimeError('Local worker rendezvous timed out')
    time.sleep(.005)

def local_write(operation):
    with output.open('a') as stream:
        stream.write(operation['kind'] + '\\n')
    time.sleep(.03)

try:
    Publisher(store, local_write).drain('shared/initial')
except PublicationUncertain:
    code = 2  # Contention is safely blocked, not blindly retried.
else:
    code = 0
if safety.unexpected_attempts:
    raise RuntimeError('Child attempted an unmocked live boundary')
sys.exit(code)
"""


class ProcessPublicationTests(OfflineTestCase):
    def test_independent_processes_share_atomic_claims(self):
        directory = self.publication_store.path.parent
        gate = directory / "start"
        output = directory / "fake-server-writes"
        workers = []
        try:
            for i in range(2):
                workers.append(subprocess.Popen(
                    [sys.executable, "-B", "-c", WORKER, str(self.publication_store.path),
                     str(directory / f"ready-{i}"), str(gate), str(output)],
                    cwd=Path(__file__).resolve().parents[1],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            deadline = time.monotonic() + 5
            while not all((directory / f"ready-{i}").exists() for i in range(2)):
                if any(p.poll() is not None for p in workers):
                    diagnostics = [p.communicate()[1] for p in workers if p.poll() is not None]
                    self.fail("Local publication worker failed to initialize: " + "\n".join(diagnostics))
                if time.monotonic() > deadline:
                    self.fail("Local publication workers did not reach the rendezvous within 5 seconds")
                time.sleep(.005)
            gate.touch()
            for worker in workers:
                stdout, stderr = worker.communicate(timeout=5)
                self.assertIn(worker.returncode, (0, 2), stdout + stderr)
            self.assertTrue(any(p.returncode == 0 for p in workers))
            self.assertEqual(output.read_text().splitlines(), ["forecast", "comment"])
            self.assertEqual([s["state"] for s in self.publication_store.steps("shared/initial")], ["confirmed", "confirmed"])
        finally:
            for worker in workers:
                if worker.poll() is None:
                    worker.kill()
                worker.communicate()
