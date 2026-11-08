"""One real authenticated source adapter for a future autonomous preflight.

This returns ONLY GitHub queue observations. All other funding, operator,
journal and billing booleans in IndependentReadings remain False by default.
It does NOT assert authorization, dispatch a workflow, write a journal,
contact Metaculus or call a model. Other independent sources must still
be implemented and authenticated before autonomous operation.
"""

from autonomous_orchestrator import IndependentReadings
from github_publisher_queue_readonly import (
    GitHubPublisherQueueReader, PublisherRunEvidence,
    GitHubRunEvidenceUnavailable,
)


class GitHubQueuePreflightSource:
    """Read GitHub Actions evidence without inventing provider/approval flags."""

    def __init__(self, reader):
        if not isinstance(reader, GitHubPublisherQueueReader):
            raise GitHubRunEvidenceUnavailable("Authenticated GitHub queue reader required")
        self._reader = reader

    def read(self):
        evidence = self._reader.read()
        if not isinstance(evidence, PublisherRunEvidence):
            raise GitHubRunEvidenceUnavailable("Invalid queue evidence")
        return IndependentReadings(
            github_checked_at=evidence.checked_at,
            active_publishers=evidence.active_publisher_runs,
            queued_publishers=evidence.queued_publisher_runs,
        )
