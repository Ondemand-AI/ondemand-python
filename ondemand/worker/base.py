"""
OndemandWorker — base class for automation workers.

Handles:
- Temporal connection and activity/workflow registration
- Graceful shutdown on SIGTERM (KEDA scale-down)
- Auto-sets ONDEMAND_RUN_ID and ONDEMAND_WEBHOOK_URL via activity interceptor
- Auto-sets up log capture via OndemandLogHandler
"""

import asyncio
import logging
import os
import signal
import sys
from typing import Callable, Any, Dict, List, Optional
from dataclasses import dataclass, field

from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker, ActivityInboundInterceptor, Interceptor

logger = logging.getLogger("ondemand.worker")


class _OndemandActivityInterceptor(ActivityInboundInterceptor):
    """Auto-sets ONDEMAND_RUN_ID and ONDEMAND_WEBHOOK_URL before each activity."""

    async def execute_activity(self, input):
        info = activity.info()
        run_id = info.workflow_id
        app_url = os.environ.get("ONDEMAND_APP_URL", "")

        os.environ["ONDEMAND_RUN_ID"] = run_id
        if app_url:
            os.environ["ONDEMAND_WEBHOOK_URL"] = f"{app_url}/api/webhooks/supervisor/{run_id}"

        return await super().execute_activity(input)


class _OndemandInterceptor(Interceptor):
    def intercept_activity(self, next):
        return _OndemandActivityInterceptor(next)


@dataclass
class WorkerConfig:
    """Configuration resolved from environment variables."""
    temporal_address: str = ""
    temporal_namespace: str = ""
    task_queue: str = ""
    app_url: str = ""
    webhook_secret: str = ""
    max_concurrent: int = 1

    @classmethod
    def from_env(cls) -> "WorkerConfig":
        config = cls(
            temporal_address=os.environ.get("TEMPORAL_ADDRESS", ""),
            temporal_namespace=os.environ.get("TEMPORAL_NAMESPACE", ""),
            task_queue=os.environ.get("TEMPORAL_QUEUE", ""),
            app_url=os.environ.get("ONDEMAND_APP_URL", ""),
            webhook_secret=os.environ.get("SUPERVISOR_WEBHOOK_SECRET", ""),
            max_concurrent=int(os.environ.get("WORKER_MAX_CONCURRENT", "1")),
        )

        # Validate required fields
        missing = []
        if not config.temporal_address:
            missing.append("TEMPORAL_ADDRESS")
        if not config.temporal_namespace:
            missing.append("TEMPORAL_NAMESPACE")
        if not config.task_queue:
            missing.append("TEMPORAL_QUEUE")

        if missing:
            raise RuntimeError(
                f"Missing required environment variables: {', '.join(missing)}. "
                f"Set them in the deployment manifest or .env file."
            )

        return config


class OndemandWorker:
    """
    Base class for Ondemand automation workers.

    Usage:
        worker = OndemandWorker(name="my-automation")
        worker.register_workflow(MyWorkflow)
        worker.register_activity(my_activity)
        worker.run()
    """

    def __init__(self, name: str = "ondemand-worker"):
        self.name = name
        self.config = WorkerConfig.from_env()
        self._workflows: List[Any] = []
        self._activities: List[Callable] = []
        self._shutdown = False

    def register_workflow(self, workflow_class):
        self._workflows.append(workflow_class)

    def register_activity(self, activity_fn):
        self._activities.append(activity_fn)

    def _handle_shutdown(self):
        if not self._shutdown:
            self._shutdown = True
            logger.info("Shutdown signal received")
            for task in asyncio.all_tasks():
                task.cancel()

    def run(self):
        """Start the worker. Blocks until shutdown."""
        try:
            asyncio.run(self._run())
        except KeyboardInterrupt:
            pass

    async def _run(self):
        config = self.config

        # Set up Ondemand logging (captures all Python logs for portal + R2)
        from ondemand.worker.logging import setup_logging
        setup_logging()

        logger.info(
            f"OndemandWorker starting: "
            f"address={config.temporal_address}, "
            f"namespace={config.temporal_namespace}, "
            f"queue={config.task_queue}, "
            f"activities={[a.__name__ for a in self._activities]}, "
            f"workflows={[w.__name__ for w in self._workflows]}"
        )

        # Handle shutdown signals (SIGTERM from KEDA scale-down)
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._handle_shutdown)

        # Connect to Temporal
        client = await Client.connect(
            config.temporal_address,
            namespace=config.temporal_namespace,
        )
        logger.info(f"Connected to Temporal at {config.temporal_address}")

        # Build worker kwargs
        worker_kwargs = {
            "client": client,
            "task_queue": config.task_queue,
            "activities": self._activities,
            "max_concurrent_activities": config.max_concurrent,
            "interceptors": [_OndemandInterceptor()],
        }

        if self._workflows:
            worker_kwargs["workflows"] = self._workflows

        worker = Worker(**worker_kwargs)

        logger.info(f"Worker polling on queue '{config.task_queue}' in namespace '{config.temporal_namespace}'")

        try:
            await worker.run()
        except asyncio.CancelledError:
            logger.info("Worker cancelled, shutting down gracefully")
        except Exception as e:
            logger.error(f"Worker error: {e}")
            sys.exit(1)
        finally:
            logger.info("Worker stopped.")
