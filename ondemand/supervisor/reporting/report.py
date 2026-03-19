"""Run report — the final output of a supervised execution."""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

from ondemand.supervisor.reporting.record_report import RecordReport
from ondemand.supervisor.reporting.status import Status
from ondemand.supervisor.reporting.step_report import StepReport
from ondemand.supervisor.reporting.timed_report import TimedReport
from ondemand.utils.json import JSONEncoder

__version__ = "1.0.0"


@dataclass
class Report(TimedReport):
    """Final execution report for a complete run.

    Contains timing, status, and the full workflow of step/record reports.
    One Report is produced per run.
    """

    workflow: List[Union[StepReport, RecordReport]] = field(default_factory=list)
    status: Optional[Status] = None
    status_message: Optional[str] = None

    def __json__(self) -> Dict[str, Any]:
        return {
            **super().__json__(),
            "supervisor_version": __version__,
            "workflow": [step.__json__() for step in self.workflow],
            "status": self.status.value if self.status else None,
            "status_message": self.status_message,
        }

    def to_dict(self) -> Dict[str, Any]:
        return self.__json__()

    def write(self, filename: Union[str, pathlib.Path]) -> None:
        """Write the report as JSON to a file."""
        path = pathlib.Path(filename)
        with path.open("w") as out:
            json.dump(self.__json__(), out, cls=JSONEncoder)
