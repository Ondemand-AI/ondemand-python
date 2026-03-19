"""Report builder — assembles step reports into a final run report."""

from __future__ import annotations

import datetime
import logging
import time
import warnings
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple, Union

from ondemand.supervisor.reporting.record import Record
from ondemand.supervisor.reporting.record_report import RecordReport
from ondemand.supervisor.reporting.report import Report
from ondemand.supervisor.reporting.status import Status
from ondemand.supervisor.reporting.step_report import StepReport
from ondemand.supervisor.reporting.timer import Timer

logger = logging.getLogger(__name__)

RecordId = str
StepId = str


@dataclass
class RecordAccumulator:
    """Manages records processed by a single step."""

    records_by_id: Dict[RecordId, Record] = field(default_factory=OrderedDict)

    def upsert(self, record: Record) -> None:
        """Insert or replace a record."""
        self.records_by_id[record.record_id] = record

    def exists(self, record_id: RecordId) -> bool:
        return record_id in self.records_by_id

    def soft_update(self, record: Record) -> None:
        """Update only if record doesn't exist or is still RUNNING."""
        if (
            self.exists(record.record_id)
            and self.records_by_id[record.record_id].status != Status.RUNNING
        ):
            return
        self.upsert(record)

    def to_reports(self, base_step_report: StepReport) -> List[RecordReport]:
        return [
            RecordReport.from_step_report(base_step_report, record)
            for record in self.records_by_id.values()
        ]

    def __iter__(self) -> Iterable[Record]:
        return iter(self.records_by_id.values())


@dataclass
class StepReportBuilder:
    """Builds a step report during the lifecycle of a step's execution.

    Accumulates records and timing, then produces a final StepReport
    via `to_reports()`.
    """

    step_id: str
    start_time: datetime.datetime
    status: Status
    end_time: Optional[datetime.datetime] = None
    _record_accumulator: RecordAccumulator = field(default_factory=RecordAccumulator)

    def update_from(self, other: StepReportBuilder) -> None:
        """Merge another builder's data into this one."""
        other_ids = {r.record_id for r in other.records}
        for record in self.records:
            if record.record_id in other_ids:
                logger.warning(
                    "Duplicate record '%s' in step '%s'",
                    record.record_id, self.step_id,
                )
        self.end_time = other.end_time
        self.status = other.status
        for record in other.records:
            self._record_accumulator.upsert(record)

    def to_reports(self) -> List[Union[StepReport, RecordReport]]:
        """Produce the final list of reports (records + step summary)."""
        step_report = self._to_report()
        record_reports: List[Union[StepReport, RecordReport]] = (
            self._record_accumulator.to_reports(step_report)
        )
        return record_reports + [step_report]

    def set_record_status(
        self,
        record_id: RecordId,
        status: Status,
        message: Optional[str] = None,
        metadata: Optional[dict] = None,
        is_soft_update: bool = False,
    ) -> None:
        """Set or update the status of a record within this step."""
        record = Record(record_id, status, message or "", metadata or {})
        if is_soft_update:
            self._record_accumulator.soft_update(record)
        else:
            self._record_accumulator.upsert(record)

    @property
    def records(self) -> Tuple[Record, ...]:
        return tuple(self._record_accumulator)

    def _to_report(self) -> StepReport:
        return StepReport(
            step_id=self.step_id,
            status=self.status,
            start_time=self.start_time,
            end_time=self.end_time,
        )


@dataclass
class ReportBuilder:
    """Assembles step-level reports into a complete run report.

    Used by the supervisor context to track step execution and produce
    the final Report at the end of a run.
    """

    timer: Timer = field(default_factory=Timer)
    _step_report_builders: List[StepReportBuilder] = field(default_factory=list)
    timer_start: float = field(default_factory=time.perf_counter)
    status: Optional[Status] = None
    status_message: Optional[str] = None
    _step_statuses_to_override: Dict[StepId, Status] = field(default_factory=dict)
    _records_to_override: Dict[StepId, Dict[RecordId, Record]] = field(
        default_factory=lambda: defaultdict(dict)
    )
    run_had_exception: bool = False
    _run_status_override: Optional[Status] = None

    def __post_init__(self):
        self.timer.start()

    def fail_step(self, step_id: str) -> None:
        """Mark a step as failed."""
        self.set_step_status(step_id=step_id, status=Status.FAILED)

    def set_step_status(self, step_id: str, status: Union[Status, str]) -> None:
        """Override a step's final status."""
        self._step_statuses_to_override[step_id] = Status(status)

    def find_index(self, step_report_builder: StepReportBuilder) -> Optional[int]:
        """Find the index of a matching step builder, or None."""
        return next(
            (
                i for i, s in enumerate(self._step_report_builders)
                if s.step_id == step_report_builder.step_id
            ),
            None,
        )

    def add_step_report(self, step_report_builder: StepReportBuilder) -> None:
        """Add a step report, merging with existing if the step ID matches."""
        index = self.find_index(step_report_builder)
        if index is not None:
            self._step_report_builders[index].update_from(step_report_builder)
        else:
            self._step_report_builders.append(step_report_builder)

    def set_record_status(
        self,
        step_id: str,
        record_id: str,
        status: Union[Status, str],
        message: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        """Override a record's status within a specific step."""
        if not isinstance(step_id, str):
            raise TypeError("step_id must be a string")
        if not isinstance(record_id, str):
            raise TypeError("record_id must be a string")
        self._records_to_override[step_id][record_id] = Record(
            record_id=record_id,
            status=Status(status),
            message=message or "",
            metadata=metadata or {},
        )

    def set_run_status(
        self, status: Union[Status, str], message: Optional[str] = None
    ) -> None:
        """Manually set the final run status."""
        if not message or not message.strip():
            warnings.warn(
                "set_run_status called without a message. "
                "This will become required in a future release.",
                DeprecationWarning,
                stacklevel=2,
            )
        self._run_status_override = Status(status)
        self.status_message = message

    def to_report(self) -> Report:
        """Build the final run report from all accumulated step data."""
        timed = self.timer.end()

        # Apply step and record status overrides
        for builder in self._step_report_builders:
            if builder.step_id in self._step_statuses_to_override:
                builder.status = self._step_statuses_to_override[builder.step_id]
            if builder.step_id in self._records_to_override:
                for record_id, record in self._records_to_override[builder.step_id].items():
                    builder.set_record_status(
                        record_id, record.status, record.message, record.metadata
                    )

        # Merge all step reports
        workflow = [
            report
            for step in self._step_report_builders
            for report in step.to_reports()
        ]

        # Determine final run status
        self.status = Status.FAILED if self.run_had_exception else Status.SUCCEEDED
        if self._run_status_override is not None:
            self.status = self._run_status_override

        return Report(
            start_time=timed.start,
            end_time=timed.end,
            workflow=workflow,
            status=self.status,
            status_message=self.status_message,
        )
