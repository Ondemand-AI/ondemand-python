"""Shared utilities for the agent."""

from ondemand.shared.artifacts import (
    get_output_dir,
    get_base_output_dir,
    save_artifact,
    load_artifact,
    set_workflow_id,
    get_workflow_id,
    get_run_info,
    RunInfo,
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
from ondemand.shared.approval import request_approval, fact, ApprovalRequestError
from ondemand.shared.bitwarden import bw_connect, bw_get_item
# cli.py removed — old RCC CLI parsing, not used in new Temporal architecture
from ondemand.shared.logging import get_logger, configure_logging, OndemandLogger
from ondemand.shared.r2_storage import (
    upload_run_artifacts,
    upload_task_artifacts,
    upload_root_artifacts,
    download_input_files,
    get_r2_client,
    R2StorageClient,
)
from ondemand.shared.runpod import (
    RunPodClient,
    PodHandle,
    RunPodKeyMissingError,
    get_runpod_client,
    get_runpod_api_key,
)
from ondemand.shared.llm_inference import (
    LLMInferenceClient,
    Prediction,
    MarginCalibrator,
    build_alpaca_prompt,
    map_name_to_code,
    margins_from_logprobs,
    normalize_text,
)

__all__ = [
    "get_output_dir",
    "get_base_output_dir",
    "save_artifact",
    "load_artifact",
    "set_workflow_id",
    "get_workflow_id",
    "get_run_info",
    "RunInfo",
    "set_current_task",
    "get_current_task",
    "request_approval",
    "fact",
    "ApprovalRequestError",
    "bw_connect",
    "bw_get_item",
    # Exception tracking
    "record_exception",
    "has_recorded_exceptions",
    "get_recorded_exceptions",
    "get_exception_summary",
    # R2 storage
    "upload_run_artifacts",
    "upload_task_artifacts",
    "upload_root_artifacts",
    "download_input_files",
    "get_r2_client",
    "R2StorageClient",
    # RunPod GPU provisioning
    "RunPodClient",
    "PodHandle",
    "RunPodKeyMissingError",
    "get_runpod_client",
    "get_runpod_api_key",
    # LLM inference + confidence
    "LLMInferenceClient",
    "Prediction",
    "MarginCalibrator",
    "build_alpaca_prompt",
    "map_name_to_code",
    "margins_from_logprobs",
    "normalize_text",
    # Backward compatibility
    "save_state",
    "load_state",
    # Logging
    "get_logger",
    "configure_logging",
    "OndemandLogger",
]
