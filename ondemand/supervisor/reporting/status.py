"""Execution status enum for steps, runs, and records."""

from enum import Enum


class Status(str, Enum):
    """Status of a tracked execution unit (step, run, or record)."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    WARNING = "warning"
    RUNNING = "running"
