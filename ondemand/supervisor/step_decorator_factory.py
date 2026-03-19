"""Step decorator factory for marking functions as workflow steps.

Example::

    from ondemand.supervisor import step

    @step("extract_data")
    def extract(source):
        return fetch_data(source)

    @step("validate")
    def validate(data):
        return check_rules(data)
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Callable, Optional

from ondemand.supervisor.event_bus import EventBus, StepReportChangeEvent
from ondemand.supervisor.reporting.report_builder import ReportBuilder, StepReportBuilder
from ondemand.supervisor.reporting.status import Status
from ondemand.supervisor.reporting.timer import Timer
from ondemand.supervisor.step_context import StepContext, StepLifecycleCallbackType

logger = logging.getLogger(__name__)


def create_step_decorator(
    report_builder: ReportBuilder,
    event_bus: EventBus,
) -> Callable:
    """Create a @step decorator bound to the given report builder and event bus.

    Returns a decorator that can be used as ``@step("step_id")`` to mark
    functions as supervised workflow steps.
    """

    def returned_decorator(
        step_id: str,
        on_step_enter_callback: Optional[StepLifecycleCallbackType] = None,
        on_step_exit_callback: Optional[StepLifecycleCallbackType] = None,
    ) -> Callable:
        def inner_decorator(fn):
            @functools.wraps(fn)
            def wrapper(*fn_args, **fn_kwargs):
                return _run_wrapped_func(
                    fn,
                    step_id,
                    report_builder,
                    event_bus,
                    on_step_enter_callback,
                    on_step_exit_callback,
                    *fn_args,
                    **fn_kwargs,
                )
            return wrapper
        return inner_decorator

    return returned_decorator


def _run_wrapped_func(
    fn: Callable,
    step_id: str,
    report_builder: ReportBuilder,
    event_bus: EventBus,
    on_step_enter_callback: Optional[StepLifecycleCallbackType] = None,
    on_step_exit_callback: Optional[StepLifecycleCallbackType] = None,
    *fn_args: Any,
    **fn_kwargs: Any,
) -> Any:
    """Execute a function with step supervision: timing, reporting, and events."""
    caught_exception: Optional[Exception] = None
    final_status: Status

    on_enter = on_step_enter_callback or StepContext.get_on_step_enter_callback()
    on_exit = on_step_exit_callback or StepContext.get_on_step_exit_callback()

    if on_enter:
        try:
            on_enter(step_id)
        except Exception as ex:
            logger.warning("Error in on_enter_callback for step %s: %s", step_id, ex)

    func_timer = Timer()
    func_timer.start()

    builder = StepReportBuilder(
        step_id=step_id,
        start_time=func_timer.start_time,
        end_time=None,
        status=Status.RUNNING,
    )

    for report in builder.to_reports():
        event_bus.emit(StepReportChangeEvent(step_report=report))

    try:
        fn_result = fn(*fn_args, **fn_kwargs)
        final_status = Status.SUCCEEDED
    except Exception as ex:
        fn_result = None
        caught_exception = ex
        final_status = Status.FAILED

    timer_result = func_timer.end()
    builder.end_time = timer_result.end
    builder.status = final_status

    report_builder.add_step_report(builder)
    for report in builder.to_reports():
        event_bus.emit(StepReportChangeEvent(step_report=report))

    if on_exit:
        try:
            on_exit(step_id)
        except Exception as ex:
            logger.warning("Error in on_exit_callback for step %s: %s", step_id, ex)

    if caught_exception:
        raise caught_exception

    return fn_result
