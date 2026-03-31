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
    SUPERVISOR_WEBHOOK_SECRET: API key for webhook auth
"""

from .base import OndemandWorker
from .reporter import WorkflowReporter

__all__ = ["OndemandWorker", "WorkflowReporter"]
