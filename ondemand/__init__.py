"""
Ondemand — Automation agent toolkit for the Ondemand platform.

Usage:
    from ondemand.worker import OndemandWorker, WorkflowReporter
    from ondemand.shared import get_logger, request_approval
"""

from importlib.metadata import PackageNotFoundError, version as _dist_version

from .shared.approval import request_approval, fact, ApprovalRequestError

try:
    # Read from installed distribution metadata rather than hardcoding, so the
    # value stays correct when a robot upgrades the package at container start.
    __version__ = _dist_version("ondemand-ai")
except PackageNotFoundError:  # running from a source checkout
    __version__ = "0.0.0.dev0"

__all__ = [
    "request_approval",
    "fact",
    "ApprovalRequestError",
    "__version__",
]
