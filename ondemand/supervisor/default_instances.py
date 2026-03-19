"""Default supervisor instances for use across an agent's modules.

Provides pre-configured singleton instances of the core supervisor
components so that step decorators, context managers, and event handlers
can share state without explicit wiring.

Typical usage in robots::

    from ondemand.supervisor import step_scope, step, supervise

    @step("extract")
    def extract():
        pass

    with supervise():
        extract()

    # Or with context manager:
    with step_scope("validate") as s:
        s.set_record_status(status="succeeded", record_id="rec-1")
"""

import logging
import pathlib
import warnings
from typing import Optional, Union

from ondemand.supervisor.event_bus import EventBus
from ondemand.supervisor.main_context import MainContext
from ondemand.supervisor.manifest import Manifest
from ondemand.supervisor.reporting.report_builder import ReportBuilder
from ondemand.supervisor.step_context import StepContext, StepLifecycleCallbackType
from ondemand.supervisor.step_decorator_factory import create_step_decorator
from ondemand.supervisor.streaming.streamer import Streamer

logger = logging.getLogger(__name__)

# Shared event bus for all supervisor components
shared_bus = EventBus()

# Shared report builder
report_builder = ReportBuilder()

# Pre-configured @step decorator
step = create_step_decorator(report_builder=report_builder, event_bus=shared_bus)

# Convenience functions
fail_step = report_builder.fail_step
set_step_status = report_builder.set_step_status
set_record_status = report_builder.set_record_status
set_run_status = report_builder.set_run_status


def set_on_step_enter_callback(callback: StepLifecycleCallbackType) -> None:
    """Register a callback invoked when any step starts."""
    StepContext.set_on_step_enter_callback(callback)


def set_on_step_exit_callback(callback: StepLifecycleCallbackType) -> None:
    """Register a callback invoked when any step ends."""
    StepContext.set_on_step_exit_callback(callback)


class step_scope(StepContext):
    """Context manager for tracking a step using default shared instances.

    Example::

        with step_scope("process_company") as s:
            for invoice in invoices:
                process(invoice)
                s.set_record_status(
                    status="succeeded",
                    record_id=invoice.id,
                )
    """

    def __init__(
        self,
        step_id: str,
        on_context_enter: Optional[StepLifecycleCallbackType] = None,
        on_context_exit: Optional[StepLifecycleCallbackType] = None,
    ):
        super().__init__(
            builder=report_builder,
            step_id=step_id,
            event_bus=shared_bus,
            on_context_enter=on_context_enter,
            on_context_exit=on_context_exit,
        )


class supervise(MainContext):
    """Context manager for supervising an entire agent run.

    Uses the shared report builder and event bus. Handles manifest
    loading, report generation, and status streaming.

    Example::

        with supervise():
            initialize()
            process()
            teardown()
    """

    def __init__(
        self,
        manifest: Union[Manifest, str, pathlib.Path] = "manifest.yaml",
        output_dir: Union[str, pathlib.Path] = "output/",
        is_robocorp_multistep_run: bool = False,
        *args,
        **kwargs,
    ):
        super().__init__(
            report_builder=report_builder,
            manifest=manifest,
            output_dir=output_dir,
            upload_uri=None,
            event_bus=shared_bus,
            is_robocorp_multistep_run=is_robocorp_multistep_run,
            *args,
            **kwargs,
        )
