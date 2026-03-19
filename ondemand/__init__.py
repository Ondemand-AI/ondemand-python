"""
Ondemand — Automation agent toolkit for the Ondemand platform.

Provides supervised execution, step tracking, dynamic manifests,
artifact management, HITL approvals, and R2 storage integration.

    from ondemand import supervised_step, request_approval, Status

    @supervised_step("Process Data")
    def process(self):
        # your automation logic
        pass
"""

from .supervisor import (
    supervised,
    supervised_step,
    connect_to_ondemand,
    send_manifest,
    build_manifest_step,
    update_manifest,
    ManifestStep,
    step_scope,
    Status,
)

from .shared.approval import request_approval, ApprovalRequestError

__all__ = [
    "supervised",
    "supervised_step",
    "connect_to_ondemand",
    "send_manifest",
    "build_manifest_step",
    "update_manifest",
    "ManifestStep",
    "step_scope",
    "Status",
    "request_approval",
    "ApprovalRequestError",
]
