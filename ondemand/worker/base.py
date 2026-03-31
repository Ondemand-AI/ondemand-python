"""
OndemandWorker — base class for automation workers running on Cloud Run.

Handles:
- Temporal connection and activity registration
- Status reporting back to portal via webhooks
- Log streaming
- Artifact upload to R2
- Graceful shutdown after execution

Each automation repo creates a worker, registers activities, and calls worker.run().
The worker connects to Temporal, picks up one job, executes it, and exits.
"""

import asyncio
import logging
import os
import signal
import sys
from typing import Callable, Any, Dict, List, Optional
from dataclasses import dataclass, field

from temporalio.client import Client
from temporalio.worker import Worker

logger = logging.getLogger("ondemand.worker")


@dataclass
class WorkerConfig:
    """Configuration resolved from environment variables."""
    temporal_address: str = ""
    temporal_namespace: str = ""
    task_queue: str = ""
    app_url: str = ""
    webhook_secret: str = ""
    max_concurrent: int = 1
    idle_timeout: int = 300  # seconds to wait for work before exiting

    @classmethod
    def from_env(cls) -> "WorkerConfig":
        config = cls(
            temporal_address=os.environ.get("TEMPORAL_ADDRESS", ""),
            temporal_namespace=os.environ.get("TEMPORAL_NAMESPACE", ""),
            task_queue=os.environ.get("TEMPORAL_QUEUE", ""),
            app_url=os.environ.get("ONDEMAND_APP_URL", ""),
            webhook_secret=os.environ.get("SUPERVISOR_WEBHOOK_SECRET", ""),
            max_concurrent=int(os.environ.get("WORKER_MAX_CONCURRENT", "1")),
            idle_timeout=int(os.environ.get("WORKER_IDLE_TIMEOUT", "300")),
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
                f"These must be set by the Cloud Run Job trigger."
            )

        return config


class OndemandWorker:
    """
    Base class for Ondemand automation workers.

    Usage:
        worker = OndemandWorker()

        @worker.activity
        async def process_data(inputs: dict) -> dict:
            # Your automation code
            return {"result": "done"}

        # In your Dockerfile CMD:
        worker.run()
    """

    def __init__(self, name: str = None):
        self.name = name or os.environ.get("WORKER_NAME", "ondemand-worker")
        self._activities: List[Callable] = []
        self._workflows: List[type] = []
        self._config: Optional[WorkerConfig] = None
        self._shutdown = False

        # Configure logging
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )

    @property
    def config(self) -> WorkerConfig:
        if not self._config:
            self._config = WorkerConfig.from_env()
        return self._config

    def activity(self, fn: Callable) -> Callable:
        """
        Decorator to register a function as a Temporal activity.

        @worker.activity
        async def my_task(inputs: dict) -> dict:
            ...
        """
        self._activities.append(fn)
        return fn

    def workflow(self, cls: type) -> type:
        """
        Decorator to register a class as a Temporal workflow.

        @worker.workflow
        class MyWorkflow:
            @workflow.run
            async def run(self, args):
                ...
        """
        self._workflows.append(cls)
        return cls

    def register_activity(self, fn: Callable):
        """Explicitly register an activity function."""
        self._activities.append(fn)

    def register_workflow(self, cls: type):
        """Explicitly register a workflow class."""
        self._workflows.append(cls)

    def run(self):
        """
        Start the worker. Connects to Temporal, registers activities/workflows,
        polls for tasks, executes, and exits when done.

        This is the main entrypoint — call this from your automation's __main__.
        """
        asyncio.run(self._run())

    async def _run(self):
        config = self.config

        logger.info(
            f"OndemandWorker starting: "
            f"address={config.temporal_address}, "
            f"namespace={config.temporal_namespace}, "
            f"queue={config.task_queue}, "
            f"activities={[a.__name__ for a in self._activities]}, "
            f"workflows={[w.__name__ for w in self._workflows]}"
        )

        # Handle shutdown signals
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
        }

        if self._workflows:
            worker_kwargs["workflows"] = self._workflows

        # Run the worker with idle timeout
        # Cloud Run Jobs pay per second — exit if no work arrives within idle_timeout
        worker = Worker(**worker_kwargs)

        logger.info(f"Worker polling on queue '{config.task_queue}' in namespace '{config.temporal_namespace}'")
        logger.info(f"Idle timeout: {config.idle_timeout}s (exits if no activity runs)")

        # Track activity execution to reset idle timer
        self._last_activity_time = asyncio.get_event_loop().time()
        self._activity_count = 0

        # Wrap activities to track execution
        original_activities = list(self._activities)
        for i, act in enumerate(original_activities):
            original_fn = act

            async def tracked_wrapper(*args, _orig=original_fn, **kwargs):
                self._last_activity_time = asyncio.get_event_loop().time()
                self._activity_count += 1
                logger.info(f"Activity started: {_orig.__name__} (#{self._activity_count})")
                try:
                    return await _orig(*args, **kwargs)
                finally:
                    self._last_activity_time = asyncio.get_event_loop().time()
                    logger.info(f"Activity completed: {_orig.__name__}")

            tracked_wrapper.__name__ = original_fn.__name__
            tracked_wrapper.__qualname__ = original_fn.__qualname__
            # Preserve temporalio activity.defn decorator metadata
            if hasattr(original_fn, '__temporal_activity_definition'):
                tracked_wrapper.__temporal_activity_definition = original_fn.__temporal_activity_definition

        async def idle_watchdog():
            """Exit the worker if idle for too long."""
            while not self._shutdown:
                await asyncio.sleep(10)  # Check every 10 seconds
                elapsed = asyncio.get_event_loop().time() - self._last_activity_time
                if elapsed > config.idle_timeout and self._activity_count > 0:
                    # Had work, now idle — time to exit
                    logger.info(f"Idle for {elapsed:.0f}s after completing {self._activity_count} activities. Shutting down.")
                    self._handle_shutdown()
                    return
                elif elapsed > config.idle_timeout and self._activity_count == 0:
                    # Never got any work — exit
                    logger.info(f"No work received for {elapsed:.0f}s. Shutting down.")
                    self._handle_shutdown()
                    return

        try:
            # Run worker and idle watchdog concurrently
            await asyncio.gather(
                worker.run(),
                idle_watchdog(),
            )
        except asyncio.CancelledError:
            logger.info("Worker cancelled, shutting down gracefully")
        except Exception as e:
            logger.error(f"Worker error: {e}")
            sys.exit(1)
        finally:
            logger.info(f"Worker stopped. Executed {self._activity_count} activities.")

    def _handle_shutdown(self):
        logger.info("Shutdown signal received")
        self._shutdown = True
        # Cancel all running tasks
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                task.cancel()
