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

All methods are no-ops when there is no run context (local runs).
"""

import logging
import os
from datetime import datetime, timezone
from enum import Enum
import atexit
import threading
from typing import Any, Dict, List, Optional
from ondemand.shared.run_context import current_webhook_url


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


# One client for the process, not one per call.
#
# This used to open `httpx.Client()` inside every _post(), which means a fresh
# TCP connection and a full TLS handshake for every single step transition and
# every record. A demo run posts ~55 of them (9 step calls per company plus one
# record per invoice per substep), so it was paying ~55 handshakes to reach the
# same host. Keep-alive collapses that to one.
#
# httpx.Client is safe to share across threads, which matters because sync
# activities run in the worker's thread pool.
_client_lock = threading.Lock()
_client: "Optional[Any]" = None


def _get_client():
    """Lazily build the shared client. Returns None if httpx is unavailable."""
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            try:
                import httpx
            except Exception as e:  # pragma: no cover
                logger.debug(f"httpx unavailable, step reports disabled: {e}")
                return None
            _client = httpx.Client(
                timeout=_WEBHOOK_TIMEOUT,
                # Small pool: a worker talks to exactly one API host.
                limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
            )
            atexit.register(_close_client)
    return _client


def _close_client() -> None:
    """Release the pool at interpreter exit so sockets are not left dangling."""
    global _client
    with _client_lock:
        if _client is not None:
            try:
                _client.close()
            except Exception:
                pass
            _client = None


def _post(payload: dict) -> bool:
    """POST to the webhook. Returns True on success, False on failure."""
    webhook_url = current_webhook_url()
    if not webhook_url:
        return False

    client = _get_client()
    if client is None:
        return False

    try:
        from ondemand.shared.webhook_auth import webhook_headers
        response = client.post(webhook_url, json=payload, headers=webhook_headers())
        return response.status_code == 200
    except Exception as e:
        # A dropped keep-alive connection surfaces here. httpx re-dials on the
        # next call, so one lost report does not poison the rest of the run.
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
