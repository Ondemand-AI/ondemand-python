"""Shared utilities for the agent."""

from ondemand.shared.artifacts import (
    get_output_dir,
    get_base_output_dir,
    save_artifact,
    load_artifact,
    set_run_id,
    get_run_id,
    set_current_task,
    get_current_task,
    # Exception tracking
    record_exception,
    has_recorded_exceptions,
    get_recorded_exceptions,
    get_exception_summary,
    # Backward compatibility
    save_state,
    load_state,
)
from ondemand.shared.cli import parse_args
from ondemand.shared.r2_storage import (
    upload_run_artifacts,
    get_r2_client,
    R2StorageClient,
)

__all__ = [
    "get_output_dir",
    "get_base_output_dir",
    "save_artifact",
    "load_artifact",
    "set_run_id",
    "get_run_id",
    "set_current_task",
    "get_current_task",
    "parse_args",
    # Exception tracking
    "record_exception",
    "has_recorded_exceptions",
    "get_recorded_exceptions",
    "get_exception_summary",
    # R2 storage
    "upload_run_artifacts",
    "get_r2_client",
    "R2StorageClient",
    # Backward compatibility
    "save_state",
    "load_state",
]
