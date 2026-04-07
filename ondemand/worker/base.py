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
import io
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


_active_activities = 0
_activity_lock = asyncio.Lock() if hasattr(asyncio, 'Lock') else None


class _OndemandActivityInterceptor(ActivityInboundInterceptor):
    """Auto-sets ONDEMAND_RUN_ID and ONDEMAND_WEBHOOK_URL before each activity."""

    async def execute_activity(self, input):
        global _active_activities
        _active_activities += 1

        info = activity.info()
        run_id = info.workflow_id
        app_url = os.environ.get("ONDEMAND_APP_URL", "")

        os.environ["ONDEMAND_RUN_ID"] = run_id
        if app_url:
            os.environ["ONDEMAND_WEBHOOK_URL"] = f"{app_url}/api/webhooks/supervisor/{run_id}"

        try:
            return await super().execute_activity(input)
        finally:
            _active_activities -= 1


class _OndemandInterceptor(Interceptor):
    def intercept_activity(self, next):
        return _OndemandActivityInterceptor(next)


class TeeStream:
    """Captures everything written to a stream while still outputting to the original."""

    def __init__(self, original):
        self.original = original
        self.buffer = io.StringIO()

    def write(self, data):
        self.original.write(data)
        self.buffer.write(data)
        return len(data)

    def flush(self):
        self.original.flush()

    def get_output(self) -> str:
        return self.buffer.getvalue()

    def fileno(self):
        return self.original.fileno()


@dataclass
class WorkerConfig:
    """Configuration resolved from environment variables."""
    temporal_address: str = ""
    temporal_namespace: str = ""
    task_queue: str = ""
    app_url: str = ""
    webhook_secret: str = ""
    max_concurrent: int = 1
    idle_timeout: int = 30  # seconds to wait for work before exiting (Cloud Run pays per second)

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

        Captures all stdout/stderr output for console.log upload.
        This is the main entrypoint — call this from your automation's __main__.
        """
        # Capture stdout/stderr — everything the process outputs, including
        # stack traces, print(), logger output, library messages
        self._stdout_capture = TeeStream(sys.stdout)
        self._stderr_capture = TeeStream(sys.stderr)
        sys.stdout = self._stdout_capture
        sys.stderr = self._stderr_capture

        try:
            asyncio.run(self._run())
        finally:
            # Restore original streams
            sys.stdout = self._stdout_capture.original
            sys.stderr = self._stderr_capture.original

    def get_captured_output(self) -> str:
        """Get all captured stdout + stderr output (for console.log upload)."""
        parts = []
        if self._stdout_capture:
            parts.append(self._stdout_capture.get_output())
        if self._stderr_capture:
            stderr = self._stderr_capture.get_output()
            if stderr:
                parts.append(f"\n=== STDERR ===\n{stderr}")
        return "".join(parts)

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
            "interceptors": [_OndemandInterceptor()],
        }

        if self._workflows:
            worker_kwargs["workflows"] = self._workflows

        # Run the worker with idle timeout
        # Cloud Run Jobs pay per second — exit if no work arrives within idle_timeout
        worker = Worker(**worker_kwargs)

        logger.info(f"Worker polling on queue '{config.task_queue}' in namespace '{config.temporal_namespace}'")
        logger.info(f"Idle timeout: {config.idle_timeout}s (exits if no work received)")

        start_time = asyncio.get_event_loop().time()

        async def idle_watchdog():
            """Exit the worker only if idle (no activities running) for too long."""
            while not self._shutdown:
                await asyncio.sleep(config.idle_timeout)
                if self._shutdown:
                    break
                if _active_activities == 0:
                    elapsed = asyncio.get_event_loop().time() - start_time
                    logger.info(f"Idle timeout ({elapsed:.0f}s). No active work. Shutting down.")
                    self._handle_shutdown()
                    break
                else:
                    logger.debug(f"Idle timeout check: {_active_activities} activities still running, skipping.")

        try:
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
            logger.info("Worker stopped.")

    def _handle_shutdown(self):
        logger.info("Shutdown signal received")
        self._shutdown = True
        # Cancel all running tasks
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                task.cancel()
