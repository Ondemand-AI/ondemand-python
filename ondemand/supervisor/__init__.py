"""
Ondemand Supervisor Integration

This module provides the connection between Thoughtful's supervisor library
and the Ondemand platform, enabling:

1. **Automatic Webhook Streaming**: Step status updates sent to Ondemand in real-time
2. **Dynamic Manifests**: Build step hierarchies based on runtime data
3. **Simplified Setup**: The `supervised()` context manager handles all boilerplate

Quick Start:
    from ondemand.supervisor import supervised

    with supervised():
        initialize()
        process()
        teardown()

The supervised() context manager automatically:
- Parses CLI arguments (--run-id, --webhook-url)
- Sets up state isolation per run
- Connects to Ondemand platform for real-time reporting
- Enters Thoughtful's supervise context

Dynamic Manifests:
    from ondemand.supervisor import build_manifest_step, update_manifest

    # Build steps based on discovered data
    dynamic_steps = []
    for company in companies:
        step = build_manifest_step(
            step_id=company["id"],
            title=f"Process {company['name']}",
            children=[
                build_manifest_step(f"{company['id']}_extract", "Extract Data"),
                build_manifest_step(f"{company['id']}_validate", "Validate"),
            ]
        )
        dynamic_steps.append(step)

    # Update manifest and send to Ondemand UI
    update_manifest(dynamic_steps, parent_step_id="Process")

Components:
    - supervised(): Context manager for running Ondemand agents
    - connect_to_ondemand(): Connect supervisor to the platform
    - send_manifest(): Send manifest updates to Ondemand
    - build_manifest_step(): Build a manifest step object
    - update_manifest(): Update manifest with dynamic steps
    - ManifestStep: Dataclass representing a workflow step
    - OndemandStreamer: Low-level webhook event handler
"""

from .connector import (
    supervised,
    supervised_step,
    connect_to_ondemand,
    send_manifest,
    get_streamer,
    OndemandStreamer,
)

from .manifest import (
    build_manifest_step,
    update_manifest,
    load_manifest,
    build_dynamic_manifest,
    ManifestStep,
)

__all__ = [
    # Main entry points
    "supervised",
    "supervised_step",
    # Connection
    "connect_to_ondemand",
    "send_manifest",
    "get_streamer",
    "OndemandStreamer",
    # Manifest helpers
    "build_manifest_step",
    "update_manifest",
    "load_manifest",
    "build_dynamic_manifest",
    "ManifestStep",
]
