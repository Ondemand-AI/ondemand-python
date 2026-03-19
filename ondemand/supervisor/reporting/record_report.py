"""Record report — links a processed record to its step execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from ondemand.supervisor.reporting.record import Record
from ondemand.supervisor.reporting.status import Status
from ondemand.supervisor.reporting.step_report import StepReport
from ondemand.supervisor.reporting.timed_report import TimedReport


@dataclass
class RecordReport(TimedReport):
    """Report for a single record processed within a step."""

    step_id: str = ""
    status: Status = Status.RUNNING
    record: Optional[Record] = None

    @classmethod
    def from_step_report(cls, step_report: StepReport, record: Record) -> RecordReport:
        """Create a RecordReport from an existing StepReport and Record."""
        return cls(
            start_time=step_report.start_time,
            end_time=step_report.end_time,
            step_id=step_report.step_id,
            status=step_report.status,
            record=record,
        )

    def __json__(self) -> Dict[str, Any]:
        return {
            **super().__json__(),
            "step_id": self.step_id,
            "step_status": self.status.value,
            "record": self.record.to_dict() if self.record else None,
        }

    def to_dict(self) -> Dict[str, Any]:
        return self.__json__()
