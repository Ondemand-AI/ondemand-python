"""
Ondemand Supervisor

Provides step tracking, run reporting, status streaming, and HITL approvals
for automation agents on the Ondemand platform.

Quick Start::

    from ondemand.supervisor import supervised_step, step_scope, Status

    @supervised_step("Process Data")
    def process(self):
        with step_scope("Company A") as s:
            s.set_record_status(status=Status.SUCCEEDED, record_id="rec-1")
"""

from .connector import (
    supervised,
    supervised_step,
    connect_to_ondemand,
    send_manifest,
    get_streamer,
    OndemandStreamer,
)

from .default_instances import (
    shared_bus,
    supervise,
    step,
    step_scope,
    report_builder,
    fail_step,
    set_step_status,
    set_record_status,
    set_run_status,
    set_on_step_enter_callback,
    set_on_step_exit_callback,
)

from .manifest import (
    Manifest,
    build_manifest_step,
    update_manifest,
    load_manifest,
    build_dynamic_manifest,
    ManifestStep,
)

from .reporting.status import Status

__all__ = [
    # Main entry points
    "supervised",
    "supervised_step",
    "supervise",
    "step",
    "step_scope",
    # Status
    "Status",
    # Connection
    "connect_to_ondemand",
    "send_manifest",
    "get_streamer",
    "OndemandStreamer",
    # Report
    "shared_bus",
    "report_builder",
    "fail_step",
    "set_step_status",
    "set_record_status",
    "set_run_status",
    "set_on_step_enter_callback",
    "set_on_step_exit_callback",
    # Manifest
    "Manifest",
    "build_manifest_step",
    "update_manifest",
    "load_manifest",
    "build_dynamic_manifest",
    "ManifestStep",
]
