"""
Ondemand Activity Logging — captures Python logs for portal display and R2 upload.

Attaches a handler to Python's logging system that:
1. Collects all log lines for later R2 upload (console.N.log)
2. Streams batches to the portal webhook (LOG_STREAM) for real-time SSE display
3. Preserves normal stdout output

Usage — automatic when using OndemandWorker (no setup needed):
    import logging
    logger = logging.getLogger(__name__)

    @activity.defn
    async def my_activity():
        logger.info("Processing...")   # → stdout + portal + collected for R2
        logger.warning("Watch out")    # → same
        return {"result": ...}

Usage — manual setup (if not using OndemandWorker):
    from ondemand.worker.logging import setup_logging
    setup_logging()
"""

import atexit
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from typing import List, Optional

# SUCCESS level (25) and Logger.success() are defined once in shared.logging;
# importing it registers the level name and patches the base Logger class.
# Re-exported here so `from ondemand.worker.logging import SUCCESS` keeps working.
from ondemand.shared.logging import SUCCESS  # noqa: F401
from ondemand.shared.run_context import (
    current_workflow_id,
    current_temporal_run_id,
    current_webhook_url,
)

_handler: Optional["OndemandLogHandler"] = None
_lock = threading.Lock()


class OndemandLogHandler(logging.Handler):
    """
    Logging handler that collects lines and streams them to the portal.

    Collected lines are stored in memory for the upload_console_log activity.
    Lines are also batched and POSTed to the webhook for real-time portal display.
    """

    FLUSH_BATCH_SIZE = 3
    FLUSH_INTERVAL_SECONDS = 2.0
    WEBHOOK_TIMEOUT = 5.0

    def __init__(self):
        super().__init__()
        self._lines: List[str] = []
        self._buffer: List[str] = []
        self._task_key = "console"  # default task key for LOG_STREAM
        self._flush_timer: Optional[threading.Timer] = None

    def set_task_key(self, task_key: str) -> None:
        """Set the current task key (used for LOG_STREAM grouping in the portal)."""
        self._task_key = task_key

    # Only exclude loggers that cause feedback loops (webhook HTTP client logs)
    EXCLUDED_LOGGERS = {"httpx", "httpcore"}

    def add_line(self, line: str) -> None:
        """Add a pre-formatted line (e.g. from stderr capture)."""
        self._lines.append(line)
        self._buffer.append(line)
        if len(self._buffer) >= self.FLUSH_BATCH_SIZE:
            self._cancel_timer()
            self.flush()
        elif not self._flush_timer:
            self._flush_timer = threading.Timer(self.FLUSH_INTERVAL_SECONDS, self.flush)
            self._flush_timer.daemon = True
            self._flush_timer.start()

    def emit(self, record: logging.LogRecord) -> None:
        if any(record.name.startswith(prefix) for prefix in self.EXCLUDED_LOGGERS):
            return

        try:
            line = self.format(record)
            self.add_line(line)
        except Exception:
            self.handleError(record)

    def _cancel_timer(self) -> None:
        if self._flush_timer:
            self._flush_timer.cancel()
            self._flush_timer = None

    def flush(self) -> None:
        """Flush buffered lines to the webhook."""
        self._cancel_timer()
        if not self._buffer:
            return

        # Read env vars lazily — they may be set by the first activity at runtime
        webhook_url = current_webhook_url()
        workflow_id = current_workflow_id()

        if not webhook_url or not workflow_id:
            self._buffer = []
            return

        lines = self._buffer[:]
        self._buffer = []

        try:
            import httpx
            payload = {
                "client": "ondemand-python",
                "version": "2.0.0",
                "action": "LOG_STREAM",
                "payload": {
                    "task_key": self._task_key,
                    "lines": lines,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            }
            with httpx.Client(timeout=self.WEBHOOK_TIMEOUT) as client:
                from ondemand.shared.webhook_auth import webhook_headers
                client.post(webhook_url, json=payload, headers=webhook_headers())
        except Exception:
            pass  # don't break the app if webhook fails

    def get_logs(self) -> List[str]:
        """Get all collected log lines."""
        return self._lines

    def clear(self) -> None:
        """Clear collected logs (e.g. after uploading a chunk)."""
        self._lines = []
        self._buffer = []


class _WorkflowContextFilter(logging.Filter):
    """Stamp Temporal's identifier pair onto every record bound for HyperDX.

    OTel's LoggingHandler copies non-reserved LogRecord attributes into log
    attributes, so anything set here becomes filterable in HyperDX —
    e.g. TemporalWorkflowID:"7f3a…" to isolate a single workflow.

    The names are Temporal's own, matching what its TracingInterceptor writes on
    spans, so one filter reaches spans and logs across every service instead of
    each layer inventing its own spelling.

    The Workflow ID is resolved by ondemand.shared.run_context, which prefers
    the per-task activity context over the process-global ONDEMAND_WORKFLOW_ID.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        workflow_id = current_workflow_id()
        if workflow_id:
            record.TemporalWorkflowID = workflow_id

        temporal_run_id = current_temporal_run_id()
        if temporal_run_id:
            record.TemporalRunID = temporal_run_id

        try:
            from temporalio import activity

            if activity.in_activity():
                info = activity.info()
                record.activity_name = info.activity_type
                record.attempt = info.attempt
                record.task_queue = info.task_queue
        except Exception:
            pass  # temporalio unavailable, or no activity context

        return True


class OndemandLogFormatter(logging.Formatter):
    """
    Formats log lines in the Ondemand standard format:
    2026-04-07 11:30:00 - module - LEVEL - message
    """

    LEVEL_MAP = {
        "SUCCESS": "SUCCESS",
        "WARNING": "WARNING",
        "ERROR": "ERROR",
        "CRITICAL": "ERROR",
        "DEBUG": "DEBUG",
    }

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        level = self.LEVEL_MAP.get(record.levelname, "INFO")
        module = record.name if record.name != "root" else "ondemand.worker"
        message = record.getMessage()
        return f"{timestamp} - {module} - {level} - {message}"


class _StderrTee:
    """Tees stderr so non-Python output (Temporal Rust core, subprocesses) is captured."""

    def __init__(self, original, handler: OndemandLogHandler):
        self._original = original
        self._handler = handler
        self._partial = ""

    def write(self, text):
        self._original.write(text)
        self._partial += text
        while "\n" in self._partial:
            line, self._partial = self._partial.split("\n", 1)
            line = line.strip()
            if line:
                self._handler.add_line(line)

    def flush(self):
        self._original.flush()

    def fileno(self):
        return self._original.fileno()

    def isatty(self):
        return self._original.isatty()

    def __getattr__(self, name):
        return getattr(self._original, name)


def setup_logging(level: int = logging.INFO) -> OndemandLogHandler:
    """
    Set up Ondemand logging. Call once at worker startup.

    - Adds OndemandLogHandler to root logger (captures all Python logging)
    - Adds handler to t_vault logger (it sets propagate=False)
    - Tees stderr to capture Temporal Rust core output

    Returns the handler instance.
    """
    global _handler

    with _lock:
        if _handler is not None:
            return _handler

        handler = OndemandLogHandler()
        handler.setFormatter(OndemandLogFormatter())
        handler.setLevel(level)

        root = logging.getLogger()
        root.addHandler(handler)
        if root.level > level:
            root.setLevel(level)

        # Container stdout. This is the only log channel that survives a SIGKILL
        # and the only one that works outside a run:
        #   - the portal/R2 handler discards anything with no run context (see
        #     OndemandLogHandler.flush), so worker startup and shutdown lines
        #     never reach it;
        #   - both the portal handler (threading.Timer) and the OTLP exporter
        #     (BatchLogRecordProcessor) buffer, so an abrupt kill loses whatever
        #     has not flushed. atexit covers graceful exit only.
        # stdout is written line-by-line by the runtime (PYTHONUNBUFFERED=1 in the
        # robot images), so it is what remains after kubelet SIGKILLs a pod at
        # terminationGracePeriodSeconds.
        #
        # Deliberately stdout, not stderr: setup_logging tees stderr into the
        # portal handler below, so a stderr handler here would echo every record
        # back into that buffer and double-log it.
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(OndemandLogFormatter())
        stream_handler.setLevel(level)
        root.addHandler(stream_handler)

        # t_vault sets propagate=False with its own StreamHandler.
        # Add our handler directly so its logs are captured for the portal.
        t_vault_logger = logging.getLogger("t_vault")
        t_vault_logger.addHandler(handler)

        # Capture stderr for non-Python output (Temporal Rust core, etc.)
        sys.stderr = _StderrTee(sys.stderr, handler)

        _handler = handler

        # Dual-emit to OTLP if observability is configured (e.g. HyperDX)
        try:
            import ondemand_obs
            otlp_handler = ondemand_obs.get_otlp_log_handler()
            if otlp_handler is not None:
                # Tag every exported record with the Temporal pair so HyperDX
                # can filter by workflow or by attempt. Scoped to this handler
                # so the portal/R2 path is untouched.
                otlp_handler.addFilter(_WorkflowContextFilter())
                root.addHandler(otlp_handler)
        except ImportError:
            pass

        # Flush remaining buffer on exit
        atexit.register(handler.flush)

        return handler


def get_handler() -> Optional[OndemandLogHandler]:
    """Get the active OndemandLogHandler, or None if not set up."""
    return _handler


def get_collected_logs() -> List[str]:
    """Get all log lines collected since startup (or last clear)."""
    if _handler is None:
        return []
    return _handler.get_logs()


def get_and_clear_logs() -> List[str]:
    """Get all collected log lines and clear the buffer. Used for chunked uploads."""
    if _handler is None:
        return []
    logs = _handler.get_logs()[:]
    _handler.clear()
    return logs
