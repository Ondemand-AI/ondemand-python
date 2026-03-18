"""
Ondemand Platform Integration Module

This module provides helpers for connecting Thoughtful supervisor to the Ondemand platform
and building dynamic manifests at runtime.

Submodules:
    - ondemand.supervisor: Supervisor integration (supervised, manifest helpers)
    - ondemand.shared: State management and CLI parsing

Quick Start:
    from ondemand import supervised, update_manifest, build_manifest_step

    # The supervised() context manager handles everything:
    # - CLI argument parsing (--run-id, --webhook-url)
    # - State isolation per run
    # - Ondemand connection
    # - Thoughtful supervise context

    with supervised():
        initialize()
        process()
        teardown()

Dynamic Manifests:
    # Build dynamic manifest based on runtime data
    dynamic_steps = [
        build_manifest_step("company_abc", "Company ABC", children=[
            build_manifest_step("company_abc_extract", "Extract Data"),
            build_manifest_step("company_abc_validate", "Validate Data"),
        ])
    ]
    update_manifest(dynamic_steps, parent_step_id="Process")

Artifact Management:
    from ondemand.shared import save_artifact, load_artifact, get_output_dir

    # Save artifact to base run directory (shared across tasks)
    save_artifact({"companies": companies})

    # Save artifact to task-specific folder
    save_artifact(results, "results.json", task="Process")

    # Load artifact from base directory
    state = load_artifact()

    # Load artifact from another task's folder
    results = load_artifact("results.json", task="Process")
"""

# Re-export from supervisor module for convenience
from .supervisor import (
    supervised,
    supervised_step,
    connect_to_ondemand,
    send_manifest,
    build_manifest_step,
    update_manifest,
    ManifestStep,
)

# HITL approval
from .shared.approval import request_approval, ApprovalRequestError

__all__ = [
    # Main entry points
    "supervised",
    "supervised_step",
    # Connection
    "connect_to_ondemand",
    "send_manifest",
    # Manifest helpers
    "build_manifest_step",
    "update_manifest",
    "ManifestStep",
    # HITL approval
    "request_approval",
    "ApprovalRequestError",
]
