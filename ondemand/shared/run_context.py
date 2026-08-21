"""
Run context — the identity of the execution the current code belongs to.

Why this module exists
----------------------
The worker's activity interceptor writes ONDEMAND_RUN_ID and
ONDEMAND_WEBHOOK_URL into `os.environ` before each activity. `os.environ` is
**process-global**, but a worker runs up to MAX_CONCURRENT_ACTIVITIES activities
at once in a ThreadPoolExecutor. Two activities from different runs on the same
pod therefore overwrite each other's values, and whichever wrote last wins for
*both*. The visible symptoms are artifacts uploaded under the wrong
`artifacts/{run_id}/` prefix and webhooks posted against the wrong run.

temporalio keeps its activity context in a `contextvars.ContextVar`, which is
correct per task, so that is the authoritative source whenever we are inside an
activity. The environment variables remain as the fallback for local runs, for
code executing outside an activity (worker startup and shutdown), and for
subprocesses that inherit the environment.

Always prefer these helpers over reading the environment directly.
"""

import os
from typing import Optional


def current_run_id() -> Optional[str]:
    """The current run id, or None when running outside an execution.

    The run id is the Temporal workflow id, which the portal also uses as the
    process_run id.
    """
    try:
        from temporalio import activity

        if activity.in_activity():
            return activity.info().workflow_id
    except Exception:
        pass  # temporalio unavailable, or no activity context

    return os.environ.get("ONDEMAND_RUN_ID") or None


def current_webhook_url() -> Optional[str]:
    """The supervisor webhook URL for the current run, or None outside a run.

    Derived from ONDEMAND_APP_URL (static per deployment, so not subject to the
    race) plus the run id resolved above, rather than read from the
    per-run ONDEMAND_WEBHOOK_URL variable.
    """
    run_id = current_run_id()
    app_url = os.environ.get("ONDEMAND_APP_URL", "").rstrip("/")

    if run_id and app_url:
        return f"{app_url}/api/webhooks/supervisor/{run_id}"

    # Local runs and tests may set the full URL directly.
    return os.environ.get("ONDEMAND_WEBHOOK_URL") or None
