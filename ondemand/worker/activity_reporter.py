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
    records: Optional[list] = None,
    summary: Optional[str] = None,
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
    if records:
        # Plural: a whole step's worth of records in one request. The API
        # accepts either; `record` stays for older robots.
        step_report["records"] = records
    if summary:
        # One line the portal shows beside the step, in place of its generic
        # "N registro(s)" fallback.
        step_report["summary"] = summary

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

    Step transitions fire immediately — they are what the portal shows live.
    Records are buffered per step and sent together, because they are not.

    Why the split: a step going running -> completed is progress somebody is
    watching. A record is an audit line for one processed item; nobody reads
    them one at a time while the robot runs, and they were half the traffic.
    Measured 2026-08-29 on one demo run: 55 STEP_REPORT posts, of which 28 were
    step transitions and 27 were individual records.

    Records flush when the step reaches a terminal state, and every
    RECORD_BATCH_SIZE before that so a worker killed mid-step loses at most
    that many rather than all of them.
    """

    #: Records held before forcing a send. Bounds what a killed pod can lose.
    RECORD_BATCH_SIZE = 50

    def __init__(self) -> None:
        # step_id -> records awaiting a send. Process-local, which is correct:
        # one worker owns a step for its duration.
        self._records: Dict[str, list] = {}

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
        summary: Optional[str] = None,
    ) -> None:
        # Before the transition, so the API has every record by the time the
        # step is marked done — the portal reads them together.
        self.flush_records(step_id)
        """
        Report a step as completed.

        Args:
            summary: Optional one-line result shown beside the step in the
                portal, e.g. "3 notas · R$ 4.500,00" or "1 nota com alertas".
                Without it the portal can only show "N registro(s)", since a
                record count is all it actually knows. Keep it short — it sits
                on a single row next to the duration.
        """
        _step_report(
            step_id=step_id,
            step_name=name or step_id,
            status=StepStatus.SUCCEEDED,
            parent_step_id=parent,
            end_time=_now(),
            summary=summary,
        )

    def step_failed(
        self,
        step_id: str,
        name: str = "",
        error: str = "",
        parent: Optional[str] = None,
    ) -> None:
        # Flushed on failure too: the records processed before the error are
        # exactly what someone debugging it needs.
        self.flush_records(step_id)
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
        summary: Optional[str] = None,
    ) -> None:
        """
        Report a step as completed with warnings.

        Args:
            summary: Optional one-line result shown beside the step, e.g.
                "1 nota com alertas". See step_completed.
        """
        _step_report(
            step_id=step_id,
            step_name=name or step_id,
            status=StepStatus.WARNING,
            parent_step_id=parent,
            end_time=_now(),
            summary=summary,
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
        """Buffer a record for a step. Sent when the step ends, or every
        RECORD_BATCH_SIZE, whichever comes first."""
        buffered = self._records.setdefault(step_id, [])
        buffered.append(
            {
                "id": record_id,
                "status": status,
                "message": message,
                "metadata": metadata or {},
            }
        )
        if len(buffered) >= self.RECORD_BATCH_SIZE:
            self.flush_records(step_id)

    def flush_records(self, step_id: str) -> None:
        """Send everything buffered for a step, if anything is."""
        buffered = self._records.pop(step_id, None)
        if not buffered:
            return
        _step_report(
            step_id=step_id,
            step_name=step_id,
            status=StepStatus.RUNNING,  # the step's own transition is a separate report
            records=buffered,
        )


# Singleton instance — import and use directly
report = ActivityReporter()
