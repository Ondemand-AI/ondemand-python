"""Step context manager for supervising individual workflow steps.

Use `step_scope` (a subclass with default instances) as a context manager
to track step execution, timing, and record processing.

Example::

    from ondemand.supervisor import step_scope

    with step_scope("extract_data") as s:
        for item in items:
            process(item)
            s.set_record_status(
                status=Status.SUCCEEDED,
                record_id=item.id,
                message=f"Processed {item.name}",
            )
"""

from __future__ import annotations

import logging
from types import TracebackType
from typing import Callable, Optional, Type, Union

from ondemand.supervisor.event_bus import EventBus, StepReportChangeEvent
from ondemand.supervisor.reporting.report_builder import ReportBuilder, StepReportBuilder
from ondemand.supervisor.reporting.status import Status
from ondemand.supervisor.reporting.timer import Timer

logger = logging.getLogger(__name__)

StepLifecycleCallbackType = Callable[[str], None]


class StepContext:
    """Context manager for tracking a single step's execution.

    Handles timing, status reporting, record tracking, and event emission.
    On exit, the step report is finalized and added to the parent ReportBuilder.

    Args:
        builder: The ReportBuilder accumulating all step reports.
        step_id: Identifier for this step (e.g., "extract_data", "1.1").
        event_bus: Bus for emitting step lifecycle events.
        on_context_enter: Optional callback when step starts.
        on_context_exit: Optional callback when step ends.
    """

    _on_step_enter_callback: Optional[StepLifecycleCallbackType] = None
    _on_step_exit_callback: Optional[StepLifecycleCallbackType] = None

    def __init__(
        self,
        builder: ReportBuilder,
        step_id: str,
        event_bus: EventBus,
        on_context_enter: Optional[StepLifecycleCallbackType] = None,
        on_context_exit: Optional[StepLifecycleCallbackType] = None,
    ):
        self.step_id = str(step_id)
        self.report_builder = builder
        self.timer = Timer()
        self._status_override: Optional[Status] = None
        self.event_bus = event_bus
        self.step_report_builder: StepReportBuilder
        self.on_context_enter = on_context_enter
        self.on_context_exit = on_context_exit

    @classmethod
    def get_on_step_enter_callback(cls) -> Optional[StepLifecycleCallbackType]:
        return cls._on_step_enter_callback

    @classmethod
    def set_on_step_enter_callback(cls, callback: StepLifecycleCallbackType) -> None:
        cls._on_step_enter_callback = callback

    @classmethod
    def get_on_step_exit_callback(cls) -> Optional[StepLifecycleCallbackType]:
        return cls._on_step_exit_callback

    @classmethod
    def set_on_step_exit_callback(cls, callback: StepLifecycleCallbackType) -> None:
        cls._on_step_exit_callback = callback

    def __enter__(self):
        callback = self.on_context_enter or StepContext._on_step_enter_callback
        if callback and callable(callback):
            try:
                callback(self.step_id)
            except Exception as e:
                logger.error("Error in on_step_enter_callback: %s", e)

        start_time = self.timer.start()
        self.step_report_builder = StepReportBuilder(
            step_id=self.step_id,
            start_time=start_time,
            status=Status.RUNNING,
        )

        for report in self.step_report_builder.to_reports():
            self.event_bus.emit(StepReportChangeEvent(step_report=report))

        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> bool:
        timed_info = self.timer.end()

        if self._status_override:
            step_status = self._status_override
        else:
            step_status = Status.FAILED if exc_type else Status.SUCCEEDED

        self.step_report_builder.end_time = timed_info.end
        self.step_report_builder.status = step_status

        for report in self.step_report_builder.to_reports():
            self.event_bus.emit(StepReportChangeEvent(step_report=report))

        self.report_builder.add_step_report(self.step_report_builder)

        callback = self.on_context_exit or StepContext._on_step_exit_callback
        if callback and callable(callback):
            try:
                callback(self.step_id)
            except Exception as e:
                logger.error("Error in on_step_exit_callback: %s", e)

        return False

    def error(self) -> None:
        """Mark this step as failed."""
        self.set_status(Status.FAILED)

    def set_status(self, status: Union[str, Status]) -> None:
        """Override this step's final status."""
        self._status_override = Status(status)

    def set_record_status(
        self,
        status: Union[str, Status],
        record_id: str,
        message: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        """Set or update a record's status within this step.

        Args:
            status: Record status (Status enum or string).
            record_id: Unique identifier for the record.
            message: Short description (max 120 chars).
            metadata: Key-value data for analysis.
        """
        if not isinstance(record_id, str):
            raise TypeError("record_id must be a string")
        if message is not None and not isinstance(message, str):
            raise ValueError("message must be a string")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("metadata must be a dict")
        self.step_report_builder.set_record_status(
            record_id, Status(status), message or "", metadata or {}
        )
