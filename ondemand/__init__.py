"""
Ondemand — Automation agent toolkit for the Ondemand platform.

Usage:
    from ondemand.worker import OndemandWorker, WorkflowReporter
    from ondemand.shared import get_logger, request_approval
"""

from .shared.approval import request_approval, ApprovalRequestError

__all__ = [
    "request_approval",
    "ApprovalRequestError",
]
