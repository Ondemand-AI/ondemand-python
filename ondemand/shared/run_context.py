"""
Workflow context — the identity of the execution the current code belongs to.

Why this module exists
----------------------
The worker's activity interceptor writes ONDEMAND_WORKFLOW_ID and
ONDEMAND_WEBHOOK_URL into `os.environ` before each activity. `os.environ` is
**process-global**, but a worker runs up to MAX_CONCURRENT_ACTIVITIES activities
at once in a ThreadPoolExecutor. Two activities from different runs on the same
pod therefore overwrite each other's values, and whichever wrote last wins for
*both*. The visible symptoms are artifacts uploaded under the wrong
`artifacts/{workflow_id}/` prefix and webhooks posted against the wrong
workflow.

temporalio keeps its activity context in a `contextvars.ContextVar`, which is
correct per task, so that is the authoritative source whenever we are inside an
activity. The environment variables remain as the fallback for local runs, for
code executing outside an activity (worker startup and shutdown), and for
subprocesses that inherit the environment.

Always prefer these helpers over reading the environment directly.
"""

import os
from typing import Optional


def current_workflow_id() -> Optional[str]:
    """The current Temporal Workflow ID, or None outside an execution.

    This is the business identifier the portal chose when starting the workflow
    (process_runs.id). Distinct from the Temporal Run ID, which identifies one
    execution OF this Workflow ID and changes on retry or continue-as-new — see
    current_temporal_run_id() below.
    """
    try:
        from temporalio import activity

        if activity.in_activity():
            return activity.info().workflow_id
    except Exception:
        pass  # temporalio unavailable, or no activity context

    return os.environ.get("ONDEMAND_WORKFLOW_ID") or None


def current_temporal_run_id() -> Optional[str]:
    """The Temporal Run ID of the current execution, or None outside one.

    A workflow retry, continue-as-new, cron tick or reset opens a new execution
    under the same Workflow ID with a new Run ID. Nothing in the library keys
    off this yet; it exists so telemetry and the portal can tell attempts apart.
    """
    try:
        from temporalio import activity

        if activity.in_activity():
            return activity.info().workflow_run_id
    except Exception:
        pass

    return None


def current_webhook_url() -> Optional[str]:
    """The supervisor webhook URL for the current workflow, or None outside one.

    Derived from ONDEMAND_APP_URL (static per deployment, so not subject to the
    race) plus the run id resolved above, rather than read from the
    per-workflow ONDEMAND_WEBHOOK_URL variable.
    """
    workflow_id = current_workflow_id()
    app_url = os.environ.get("ONDEMAND_APP_URL", "").rstrip("/")

    if workflow_id and app_url:
        return f"{app_url}/api/webhooks/supervisor/{workflow_id}"

    # Local runs and tests may set the full URL directly.
    return os.environ.get("ONDEMAND_WEBHOOK_URL") or None
