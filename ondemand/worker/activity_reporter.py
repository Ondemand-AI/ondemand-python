"""
Activity Reporter — sends step updates directly to the portal via webhook.

Unlike WorkflowReporter (which stores state for Temporal queries), this
sends each update immediately to the portal webhook. The portal writes
to DB and broadcasts via SSE — no polling needed.

Usage inside an activity:

    from ondemand.worker.activity_reporter import report

    @activity.defn
    async def my_activity():
        report.step_started("extract", "Extrair Dados", parent="process")
        # ... do work ...
        report.record("extract", "INV-001", "success", "Extracted OK")
        report.step_completed("extract")

All methods are no-ops when ONDEMAND_WEBHOOK_URL is not set (local runs).
"""

import logging
import os
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class StepStatus(str, Enum):
    """Step statuses accepted by the STEP_REPORT webhook."""
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    WARNING = "WARNING"
    SKIPPED = "SKIPPED"

logger = logging.getLogger("ondemand.worker.activity_reporter")

_WEBHOOK_TIMEOUT = 5.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _post(payload: dict) -> bool:
    """POST to the webhook. Returns True on success, False on failure."""
    webhook_url = os.environ.get("ONDEMAND_WEBHOOK_URL")
    if not webhook_url:
        return False

    try:
        import httpx
        from ondemand.shared.webhook_auth import webhook_headers
        with httpx.Client(timeout=_WEBHOOK_TIMEOUT) as client:
            response = client.post(webhook_url, json=payload, headers=webhook_headers())
            return response.status_code == 200
    except Exception as e:
        logger.debug(f"Webhook POST failed: {e}")
        return False


def _step_report(
    step_id: str,
    step_name: str,
    status: str,
    parent_step_id: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    duration_ms: Optional[int] = None,
    record: Optional[dict] = None,
) -> bool:
    """Send a STEP_REPORT to the webhook."""
    step_report: Dict[str, Any] = {
        "step_id": step_id,
        "step_name": step_name,
        "step_status": status,
    }

    if parent_step_id:
        step_report["parent_step_id"] = parent_step_id
    if start_time:
        step_report["start_time"] = start_time
    if end_time:
        step_report["end_time"] = end_time
    if duration_ms is not None:
        step_report["duration_in_ms"] = duration_ms
    if record:
        step_report["record"] = record

    return _post({
        "client": "ondemand-python",
        "version": "2.0.0",
        "action": "STEP_REPORT",
        "payload": {
            "step_report": step_report,
        },
    })


class ActivityReporter:
    """
    Sends step updates directly to the portal via webhook.

    Each method fires immediately — no batching, no accumulation.
    Used inside Temporal activities for real-time step progress.
    """

    def step_started(
        self,
        step_id: str,
        name: str,
        parent: Optional[str] = None,
    ) -> None:
        """Report a step as started (running)."""
        _step_report(
            step_id=step_id,
            step_name=name,
            status=StepStatus.RUNNING,
            parent_step_id=parent,
            start_time=_now(),
        )

    def step_completed(
        self,
        step_id: str,
        name: str = "",
        parent: Optional[str] = None,
    ) -> None:
        """Report a step as completed."""
        _step_report(
            step_id=step_id,
            step_name=name or step_id,
            status=StepStatus.SUCCEEDED,
            parent_step_id=parent,
            end_time=_now(),
        )

    def step_failed(
        self,
        step_id: str,
        name: str = "",
        error: str = "",
        parent: Optional[str] = None,
    ) -> None:
        """Report a step as failed."""
        _step_report(
            step_id=step_id,
            step_name=name or step_id,
            status=StepStatus.FAILED,
            parent_step_id=parent,
            end_time=_now(),
        )

    def step_warning(
        self,
        step_id: str,
        name: str = "",
        parent: Optional[str] = None,
    ) -> None:
        """Report a step as completed with warnings."""
        _step_report(
            step_id=step_id,
            step_name=name or step_id,
            status=StepStatus.WARNING,
            parent_step_id=parent,
            end_time=_now(),
        )

    def step_skipped(
        self,
        step_id: str,
        name: str = "",
        parent: Optional[str] = None,
    ) -> None:
        """Report a step as skipped."""
        _step_report(
            step_id=step_id,
            step_name=name or step_id,
            status=StepStatus.SKIPPED,
            parent_step_id=parent,
        )

    def record(
        self,
        step_id: str,
        record_id: str,
        status: str,
        message: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Add a record to a step (individual item result)."""
        _step_report(
            step_id=step_id,
            step_name=step_id,
            status=StepStatus.RUNNING,  # step stays running while records are added
            record={
                "id": record_id,
                "status": status,
                "message": message,
                "metadata": metadata or {},
            },
        )


# Singleton instance — import and use directly
report = ActivityReporter()
