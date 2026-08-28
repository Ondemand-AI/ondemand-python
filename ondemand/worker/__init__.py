"""
Ondemand Worker SDK — base class for building Cloud Run automation workers.

Usage:
    from ondemand.worker import OndemandWorker

    worker = OndemandWorker()

    @worker.activity
    async def my_automation(inputs: dict) -> dict:
        worker.report("Processing...")
        result = do_work(inputs)
        worker.upload("output.xlsx", result_bytes)
        return {"count": 42}

    if __name__ == "__main__":
        worker.run()

Environment variables:
    TEMPORAL_ADDRESS: Temporal server address (e.g., temporal.ondemand-ai.com.br:7233)
    TEMPORAL_NAMESPACE: Temporal namespace (org code, e.g., "gse")
    TEMPORAL_QUEUE: Task queue name (process code, e.g., "conciliacao")
    ONDEMAND_APP_URL: API base URL for webhook callbacks
    ONDEMAND_WEBHOOK_SECRET: shared secret for webhook auth, sent by every
        sender as `Authorization: Bearer`. Omitted when unset, so local runs work.
"""

from .base import OndemandWorker
from .reporter import WorkflowReporter
from .logging import setup_logging, get_collected_logs, get_and_clear_logs
from .activity_reporter import report, ActivityReporter

from ondemand.worker.input import WorkflowInput

__all__ = ["OndemandWorker", "WorkflowInput", "WorkflowReporter", "setup_logging", "get_collected_logs", "get_and_clear_logs", "report", "ActivityReporter"]
