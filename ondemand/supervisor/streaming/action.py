"""Webhook action types for supervisor event streaming."""

from enum import Enum


class Action(str, Enum):
    """Defines the type of event being streamed to the callback endpoint."""

    STEP_REPORT = "step_report"
    """Step execution report (new step or status update)."""

    BOT_MANIFEST = "bot_manifest"
    """Workflow manifest defining the step hierarchy."""

    ARTIFACTS_UPLOADED = "artifacts_uploaded"
    """Notification that run artifacts are available in storage."""

    STATUS_CHANGE = "status_change"
    """Run-level status transition (running, completed, failed, etc.)."""
