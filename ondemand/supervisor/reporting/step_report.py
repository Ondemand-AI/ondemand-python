"""Step report — represents a single step's execution result."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from ondemand.supervisor.reporting.status import Status
from ondemand.supervisor.reporting.timed_report import TimedReport


@dataclass
class StepReport(TimedReport):
    """Execution report for a single workflow step.

    Contains timing, status, and step identification. Multiple StepReports
    compose the workflow section of a run report.
    """

    step_id: str = ""
    status: Status = Status.RUNNING

    def __json__(self) -> Dict[str, Any]:
        return {
            **super().__json__(),
            "step_id": self.step_id,
            "step_status": self.status.value,
        }

    def to_dict(self) -> Dict[str, Any]:
        return self.__json__()
