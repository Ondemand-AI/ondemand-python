"""
OndemandWorker — base class for automation workers.

Handles:
- Temporal connection and activity/workflow registration
- Graceful shutdown on SIGTERM (KEDA scale-down)
- Publishes ONDEMAND_WORKFLOW_ID / ONDEMAND_WEBHOOK_URL via activity interceptor
  (fallback only — in-process code should use ondemand.shared.run_context)
- Auto-sets up log capture via OndemandLogHandler
"""

import asyncio
import logging
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Callable, Any, Dict, List, Optional
from dataclasses import dataclass, field

from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker, ActivityInboundInterceptor, Interceptor

logger = logging.getLogger("ondemand.worker")


def _resolve_service_name() -> str:
    """The single source of truth for this robot's telemetry service name.

    Order matters and both entries are set by the deploy pipeline from the same
    `process_id`, so they agree by construction:

      1. OTEL_SERVICE_NAME — what ondemand-obs and every OTel SDK actually read.
      2. TEMPORAL_QUEUE    — same value; a safety net if the OTel var is missing,
                             so telemetry is still filed under the robot's real
                             name rather than a generic default.
      3. "ondemand-worker" — local runs with neither set.

    Previously each robot passed its own string to OndemandWorker(name=...) while
    ondemand-obs read OTEL_SERVICE_NAME, so the name in the code and the name in
    HyperDX were different values that nothing kept in sync.
    """
    return (
        os.environ.get("OTEL_SERVICE_NAME")
        or os.environ.get("TEMPORAL_QUEUE")
        or "ondemand-worker"
    )


class _WorkTracker:
    """Tracks whether this worker is actually doing anything.

    The idle-exit watchdog needs an answer to "is it safe to die?" that KEDA
    cannot give it. in_flight is incremented for the whole duration of every
    activity, so a worker holding even one activity is never idle regardless of
    how empty the task queue looks.

    Caveat: only activities are counted. Workflow tasks are milliseconds long and
    a workflow whose worker vanishes simply replays on the next one, so the
    exposure is a cold start, never lost work.
    """

    def __init__(self):
        self._in_flight = 0
        self._lock = threading.Lock()
        self._last_change = time.monotonic()

    def start(self) -> None:
        with self._lock:
            self._in_flight += 1
            self._last_change = time.monotonic()

    def end(self) -> None:
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
            self._last_change = time.monotonic()

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    @property
    def idle_seconds(self) -> float:
        """Seconds since the last activity started or finished.

        Counts from worker startup when nothing has run yet, so a Job that KEDA
        created speculatively and that never receives work still exits.
        """
        with self._lock:
            return time.monotonic() - self._last_change


_work = _WorkTracker()


class _OndemandActivityInterceptor(ActivityInboundInterceptor):
    """Publishes the run context into the environment before each activity.

    These variables are a *compatibility fallback* only — for subprocesses that
    inherit the environment and for code paths outside an activity. They are
    process-global, so with concurrent activities on one pod the values belong to
    whichever activity started last. In-process callers must therefore read the
    run context through ondemand.shared.run_context, which prefers temporalio's
    per-task activity contextvar. See that module for the full explanation.
    """

    async def execute_activity(self, input):
        info = activity.info()
        workflow_id = info.workflow_id
        app_url = os.environ.get("ONDEMAND_APP_URL", "")

        os.environ["ONDEMAND_WORKFLOW_ID"] = workflow_id
        if app_url:
            os.environ["ONDEMAND_WEBHOOK_URL"] = f"{app_url}/api/webhooks/supervisor/{workflow_id}"

        interval = float(os.environ.get("ONDEMAND_HEARTBEAT_SECONDS", 10.0))

        _work.start()
        try:
            if interval <= 0:
                return await super().execute_activity(input)

            # Heartbeat on the robot's behalf so no activity has to remember to.
            #
            # Two things depend on it. Temporal only notices a dead worker after
            # heartbeat_timeout — without heartbeats it waits out
            # start_to_close_timeout instead, so a crash mid-activity looks like a
            # 30-minute hang. And while an activity is in flight the task is not in
            # the backlog, so KEDA cannot see the lost work either; the heartbeat
            # timeout is what returns it to the backlog where KEDA can act.
            #
            # Requires the workflow to declare heartbeat_timeout on the activity —
            # the worker cannot set that. Heartbeating without it is recorded and
            # otherwise harmless.
            stop = asyncio.Event()

            async def _beat() -> None:
                while not stop.is_set():
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=interval)
                        return  # stop was set — activity finished
                    except asyncio.TimeoutError:
                        try:
                            activity.heartbeat()
                        except Exception:
                            # Cancellation arrives via heartbeat; the activity task
                            # sees it. Never let the beat itself fail the activity.
                            return

            # create_task copies the current context, so the activity contextvar
            # temporalio sets is visible inside _beat.
            beat = asyncio.create_task(_beat())
            try:
                return await super().execute_activity(input)
            finally:
                stop.set()
                beat.cancel()
        finally:
            _work.end()


class _OndemandInterceptor(Interceptor):
    def intercept_activity(self, next):
        return _OndemandActivityInterceptor(next)


def _build_interceptors() -> list:
    interceptors = [_OndemandInterceptor()]
    try:
        import ondemand_obs
        if ondemand_obs.is_configured():
            interceptors.append(ondemand_obs.get_temporal_tracing_interceptor())
    except ImportError:
        pass
    return interceptors


@dataclass
class WorkerConfig:
    """Configuration resolved from environment variables."""
    temporal_address: str = ""
    temporal_namespace: str = ""
    task_queue: str = ""
    app_url: str = ""
    max_concurrent_activities: int = 0
    graceful_shutdown_seconds: int = 0
    graceful_shutdown_explicit: bool = False
    idle_exit_seconds: int = 0
    heartbeat_seconds: float = 0.0

    # How long a running activity may keep going after SIGTERM before the worker
    # cancels it. KEDA scales robots to zero based on Temporal *queue backlog*,
    # which reads 0 while a worker is busy executing — so a scale-down SIGTERM
    # routinely lands mid-activity. Without this, temporalio's default of 0 means
    # the activity is cancelled instantly and fails with ApplicationError type
    # "WorkerShutdown"; with it, the activity finishes and reports its result.
    #
    # The ceiling is the pod's terminationGracePeriodSeconds, after which kubelet
    # SIGKILLs regardless. GKE Autopilot hard-caps that at 600s via an admission
    # mutator (it silently rewrites larger values), so 540 leaves ~60s for
    # cancellation handling and exit.
    #
    # This is a LAST-RESORT fallback, not a recommendation: the right value
    # depends on how long that robot's activities actually run, so every robot is
    # expected to set ONDEMAND_GRACEFUL_SHUTDOWN_SECONDS in its own deployment
    # manifest. A robot that leaves it unset gets an error logged at startup.
    # See ondemand-auto-demo/k8s/deployment.yml for the reference configuration.
    DEFAULT_GRACEFUL_SHUTDOWN_SECONDS = 540

    # Idle-exit, for robots run as a KEDA ScaledJob rather than a ScaledObject.
    #
    # A ScaledObject is a replica dial KEDA turns down, and Kubernetes deletes
    # whichever pod it likes — it cannot know an activity is running, which is why
    # scale-down kills work. A ScaledJob instead creates a Job per unit of work,
    # and nothing scales a Job down: it ends when the process ends. That only
    # works if the worker eventually exits, so this is that exit condition.
    #
    # The exit is gated on _WorkTracker.in_flight, so a worker can never quit on
    # its own running activity — the decision moves from KEDA (which sees only the
    # queue) to the worker (which knows exactly what it is executing).
    #
    # 0 disables it: the worker polls forever, which is correct under a
    # ScaledObject. Size it above the longest *quiet gap inside a run* (e.g. a
    # workflow.sleep between activities), or the worker exits mid-workflow and the
    # next step waits for a fresh Job. Exiting during a long human-approval wait
    # is the desired behaviour, not a problem.
    DEFAULT_IDLE_EXIT_SECONDS = 0

    # How often to heartbeat from inside an activity. Only meaningful when the
    # workflow declares heartbeat_timeout on the activity; harmless otherwise.
    # Keep it well under that timeout — a third of it is the usual convention.
    DEFAULT_HEARTBEAT_SECONDS = 10.0

    @classmethod
    def from_env(cls) -> "WorkerConfig":
        config = cls(
            temporal_address=os.environ.get("TEMPORAL_ADDRESS", ""),
            temporal_namespace=os.environ.get("TEMPORAL_NAMESPACE", ""),
            task_queue=os.environ.get("TEMPORAL_QUEUE", ""),
            app_url=os.environ.get("ONDEMAND_APP_URL", ""),
            max_concurrent_activities=int(os.environ.get("MAX_CONCURRENT_ACTIVITIES", "0")),
            graceful_shutdown_seconds=int(
                os.environ.get(
                    "ONDEMAND_GRACEFUL_SHUTDOWN_SECONDS",
                    cls.DEFAULT_GRACEFUL_SHUTDOWN_SECONDS,
                )
            ),
            idle_exit_seconds=int(
                os.environ.get(
                    "ONDEMAND_IDLE_EXIT_SECONDS", cls.DEFAULT_IDLE_EXIT_SECONDS
                )
            ),
            heartbeat_seconds=float(
                os.environ.get(
                    "ONDEMAND_HEARTBEAT_SECONDS", cls.DEFAULT_HEARTBEAT_SECONDS
                )
            ),
        )

        # NOTE: no logging here. from_env() runs from __init__, which robots call
        # at module scope (`worker = OndemandWorker(...)`), so this executes at
        # import time — before setup_logging() attaches any handler. A log call
        # here would fall through to logging.lastResort (stderr only) and never
        # reach the portal or HyperDX. The warning is emitted from _run() instead;
        # this only records what needs saying.
        config.graceful_shutdown_explicit = (
            "ONDEMAND_GRACEFUL_SHUTDOWN_SECONDS" in os.environ
        )

        # Validate required fields
        missing = []
        if not config.temporal_address:
            missing.append("TEMPORAL_ADDRESS")
        if not config.temporal_namespace:
            missing.append("TEMPORAL_NAMESPACE")
        if not config.task_queue:
            missing.append("TEMPORAL_QUEUE")
        if not config.max_concurrent_activities:
            missing.append("MAX_CONCURRENT_ACTIVITIES")

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

    def __init__(self, name: Optional[str] = None):
        # The service name is resolved from the environment, not from this
        # argument — see _resolve_service_name. Robots used to pass a name here
        # ("demo-automation", "gse-automation") that never reached HyperDX,
        # because ondemand-obs prefers OTEL_SERVICE_NAME and the deploy pipeline
        # always sets it. The two drifted silently: telemetry said "demo" while
        # the code said "demo-automation", which sends you searching for a
        # service that does not exist.
        self.name = _resolve_service_name()
        if name and name != self.name:
            logger.warning(
                f"OndemandWorker(name={name!r}) is ignored for telemetry — the "
                f"service name comes from OTEL_SERVICE_NAME (resolved: "
                f"{self.name!r}). Drop the argument; it is set by the deploy "
                f"pipeline from process_id."
            )
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
            logger.info(
                "Shutdown signal received (usually KEDA scale-down) — no new tasks; "
                f"running activities have {self.config.graceful_shutdown_seconds}s to finish"
            )
            # Tell the Temporal worker to stop accepting new tasks and drain
            if hasattr(self, '_worker') and self._worker:
                asyncio.create_task(self._graceful_shutdown())

    async def _graceful_shutdown(self):
        """Drain the worker.

        temporalio notifies running activities of the shutdown, waits
        graceful_shutdown_timeout, then cancels whatever is still running — and
        shutdown() itself waits for every activity to actually finish. An
        activity that ignores cancellation can therefore block here until the
        pod's terminationGracePeriodSeconds expires and kubelet SIGKILLs us.
        """
        try:
            await self._worker.shutdown()
            logger.info("Worker drained — all activities finished")
        except Exception as e:
            logger.error(f"Error during graceful shutdown: {e}")

    async def _idle_watchdog(self, idle_exit_seconds: int):
        """Exit once this worker has been idle with nothing in flight.

        This is what lets a robot run as a KEDA ScaledJob: the Job ends when the
        process ends, so the worker has to decide for itself when it is done.
        Gated on _work.in_flight, so it can never cut short its own activity —
        which is exactly the guarantee KEDA's queue-depth trigger cannot make.
        """
        check_interval = min(5.0, max(1.0, idle_exit_seconds / 10))
        while True:
            await asyncio.sleep(check_interval)
            if self._shutdown:
                return
            in_flight = _work.in_flight
            idle_for = _work.idle_seconds
            if in_flight == 0 and idle_for >= idle_exit_seconds:
                logger.info(
                    f"Idle for {idle_for:.0f}s with no activities in flight "
                    f"(ONDEMAND_IDLE_EXIT_SECONDS={idle_exit_seconds}) — exiting so "
                    "the Job can complete"
                )
                self._shutdown = True
                await self._graceful_shutdown()
                return

    def run(self):
        """Start the worker. Blocks until shutdown."""
        try:
            asyncio.run(self._run())
        except KeyboardInterrupt:
            pass

    async def _run(self):
        config = self.config

        # Initialize observability BEFORE logging (so OTLP handler is available)
        try:
            import ondemand_obs
            ondemand_obs.configure_observability(service_name=self.name)
        except ImportError:
            pass

        # Set up Ondemand logging (captures all Python logs for portal + R2)
        from ondemand.worker.logging import setup_logging
        setup_logging()

        from ondemand import __version__ as _lib_version

        logger.info(
            f"OndemandWorker starting: "
            f"ondemand-ai={_lib_version}, "
            f"address={config.temporal_address}, "
            f"namespace={config.temporal_namespace}, "
            f"queue={config.task_queue}, "
            f"graceful_shutdown={config.graceful_shutdown_seconds}s, "
            f"idle_exit={config.idle_exit_seconds or 'off'}, "
            f"heartbeat={config.heartbeat_seconds}s, "
            f"activities={[a.__name__ for a in self._activities]}, "
            f"workflows={[w.__name__ for w in self._workflows]}"
        )

        # Deferred from from_env() so it reaches stdout, the portal and HyperDX
        # rather than only logging.lastResort. An error, not a warning: an unset
        # value means nobody sized the drain window against this robot's activity
        # durations, and the symptom is a mid-run activity dying with
        # "WorkerShutdown", which reads as an application bug rather than a config
        # gap. Non-fatal — the fallback keeps robots running.
        if not config.graceful_shutdown_explicit:
            logger.error(
                "ONDEMAND_GRACEFUL_SHUTDOWN_SECONDS is not set — falling back to "
                f"{config.graceful_shutdown_seconds}s. Set it in this robot's "
                "deployment manifest: it must exceed the robot's longest activity "
                "but stay below the pod's terminationGracePeriodSeconds (GKE "
                "Autopilot caps that at 600s). See ondemand-auto-demo for the "
                "reference configuration."
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
            "max_concurrent_activities": config.max_concurrent_activities,
            "activity_executor": ThreadPoolExecutor(max_workers=config.max_concurrent_activities),
            "interceptors": _build_interceptors(),
            # See WorkerConfig.DEFAULT_GRACEFUL_SHUTDOWN_SECONDS. Without this,
            # temporalio defaults to timedelta() — zero — and every KEDA
            # scale-down kills the in-flight activity.
            "graceful_shutdown_timeout": timedelta(seconds=config.graceful_shutdown_seconds),
        }

        if self._workflows:
            worker_kwargs["workflows"] = self._workflows

        self._worker = Worker(**worker_kwargs)

        logger.info(f"Worker polling on queue '{config.task_queue}' in namespace '{config.temporal_namespace}'")

        watchdog = None
        if config.idle_exit_seconds > 0:
            watchdog = asyncio.create_task(
                self._idle_watchdog(config.idle_exit_seconds)
            )

        try:
            await self._worker.run()
        except asyncio.CancelledError:
            logger.info("Worker cancelled, shutting down gracefully")
        except Exception as e:
            logger.error(f"Worker error: {e}")
            sys.exit(1)
        finally:
            if watchdog:
                watchdog.cancel()
            logger.info("Worker stopped.")
