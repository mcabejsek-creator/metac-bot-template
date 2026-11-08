"""Source-controlled worker settings shared by main and the offline controller.

This object describes code configuration, not the identity or approval of a
running worker. It must never be treated as a runtime attestation.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkerConfiguration:
    research_reports_per_question: int
    predictions_per_research_report: int
    researcher: str
    agent_phases: tuple[str, str]

    def __post_init__(self):
        for count in (self.research_reports_per_question, self.predictions_per_research_report):
            if type(count) is not int or count < 1:
                raise ValueError("Worker ensemble counts must be positive integers")
        if type(self.researcher) is not str or self.researcher != "perplexity-agent":
            raise ValueError("This worker configuration requires Perplexity Agent research")
        if (type(self.agent_phases) is not tuple or len(self.agent_phases) != 2
                or any(type(phase) is not str or not phase.strip() for phase in self.agent_phases)
                or len(set(self.agent_phases)) != 2):
            raise ValueError("Worker research needs two distinct immutable phase names")

    @property
    def agent_requests_per_atomic_question(self):
        """No retries: each report executes both phases in main.run_research."""
        return self.research_reports_per_question * len(self.agent_phases)


WORKER_CONFIG = WorkerConfiguration(
    research_reports_per_question=2,
    predictions_per_research_report=3,
    researcher="perplexity-agent",
    agent_phases=("initial", "fact-check"),
)
