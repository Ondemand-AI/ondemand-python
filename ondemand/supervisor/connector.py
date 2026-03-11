"""
Ondemand Platform Connector

Connects the Thoughtful supervisor library to the Ondemand platform via webhooks.

This module provides:
- OndemandStreamer: Streams supervisor events to Ondemand webhooks
- connect_to_ondemand(): Connect supervisor to the platform
- supervised(): Context manager that handles all boilerplate

Usage:
    from ondemand.supervisor import supervised

    with supervised():
        initialize()
        process()
        teardown()
"""

import functools
import logging
import os
import shutil
import subprocess
import time
import requests
from typing import Optional, Any, Dict, Callable

# Suppress verbose "emit event" logs from thoughtful's event_bus.py
# The thoughtful package incorrectly imports logger from Python's venv module
# and logs every event at INFO level, cluttering console output
logging.getLogger("venv").setLevel(logging.WARNING)

from thoughtful.supervisor import shared_bus, supervise, step
from thoughtful.supervisor.event_bus import (
    Event,
    StepReportChangeEvent,
    RunStatusChangeEvent,
    NewManifestEvent,
)
from thoughtful.supervisor.reporting.status import Status

from ondemand.shared import (
    parse_args,
    set_run_id,
    get_output_dir,
    get_base_output_dir,
    set_current_task,
    record_exception,
    has_recorded_exceptions,
    get_exception_summary,
    upload_run_artifacts,
    upload_task_artifacts,
    upload_root_artifacts,
)

logger = logging.getLogger(__name__)

# Global state
_ondemand_streamer: Optional["OndemandStreamer"] = None
_step_stack: list = []  # Stack to track parent-child relationships
_git_info: Optional[Dict[str, Any]] = None  # Cached git info for current robot


def get_git_info() -> Optional[Dict[str, Any]]:
    """
    Get git information for the current working directory.
    Returns dict with repo_url, branch, commit_hash, commit_message, author.
    Returns None if not in a git repository.
    """
    global _git_info

    # Return cached info if available
    if _git_info is not None:
        return _git_info

    try:
        # Get remote URL
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=5
        )
        repo_url = result.stdout.strip() if result.returncode == 0 else None

        # Clean up repo URL (remove .git suffix, convert SSH to HTTPS for display)
        if repo_url:
            if repo_url.endswith(".git"):
                repo_url = repo_url[:-4]
            # Convert SSH URL to HTTPS for clickable links
            if repo_url.startswith("git@github.com:"):
                repo_url = repo_url.replace("git@github.com:", "https://github.com/")

        # Get current branch
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5
        )
        branch = result.stdout.strip() if result.returncode == 0 else None

        # Get current commit hash (short)
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5
        )
        commit_hash = result.stdout.strip() if result.returncode == 0 else None

        # Get full commit hash for linking
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5
        )
        commit_hash_full = result.stdout.strip() if result.returncode == 0 else None

        # Get commit message (first line)
        result = subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            capture_output=True,
            text=True,
            timeout=5
        )
        commit_message = result.stdout.strip() if result.returncode == 0 else None

        # Get commit author
        result = subprocess.run(
            ["git", "log", "-1", "--format=%an"],
            capture_output=True,
            text=True,
            timeout=5
        )
        author = result.stdout.strip() if result.returncode == 0 else None

        if repo_url or branch or commit_hash:
            _git_info = {
                "repo_url": repo_url,
                "branch": branch,
                "commit_hash": commit_hash,
                "commit_hash_full": commit_hash_full,
                "commit_message": commit_message,
                "author": author,
            }
            logger.info(f"Git info captured: {branch} @ {commit_hash}")
            return _git_info

    except Exception as e:
        logger.debug(f"Could not get git info: {e}")

    return None


class OndemandStreamer:
    """
    Streams supervisor events to the Ondemand platform via webhooks.

    Handles:
    - BOT_MANIFEST: Sends manifest (including dynamic updates)
    - STEP_REPORT: Sends step status updates
    - STATUS_CHANGE: Sends run status changes
    """

    def __init__(self, run_id: str, webhook_url: str, api_key: Optional[str] = None):
        self.run_id = run_id
        self.webhook_url = webhook_url
        self.api_key = api_key
        self._session = requests.Session()

        # Set up headers
        self._session.headers["Content-Type"] = "application/json"
        if api_key:
            self._session.headers["X-Webhook-Secret"] = api_key

    def handle_event(self, event: Event) -> None:
        """Handle supervisor events and forward to Ondemand."""
        try:
            if isinstance(event, NewManifestEvent):
                self._handle_manifest(event)
            elif isinstance(event, StepReportChangeEvent):
                self._handle_step_report(event)
            elif isinstance(event, RunStatusChangeEvent):
                self._handle_status_change(event)
            else:
                logger.debug(f"Unhandled event type: {type(event)}")
        except Exception as e:
            logger.warning(f"Failed to send event to Ondemand: {e}")

    def _handle_manifest(self, event: NewManifestEvent) -> None:
        """Send manifest to Ondemand (supports updates for dynamic manifests)."""
        manifest = event.manifest
        payload = {
            "action": "bot_manifest",
            "payload": {
                "bot_manifest": manifest.__json__()
            }
        }
        self._send(payload)
        logger.info(f"Manifest sent with {len(manifest.workflow)} top-level steps")

    def _handle_step_report(self, event: StepReportChangeEvent) -> None:
        """Send step report to Ondemand."""
        global _step_stack
        step_report = event.step_report
        step_id = step_report.step_id
        status = step_report.status.value.lower()

        # Check if this is a record event (has record attribute)
        record = getattr(step_report, 'record', None)
        is_record_event = record is not None

        # Track parent-child relationships using a stack
        # When a step starts (RUNNING), push it to stack. Its parent is the previous top.
        # When a step ends (SUCCEEDED/FAILED), pop it from stack.
        # Record events don't affect the stack but use the current step as their owner.
        parent_step_id = None

        if is_record_event:
            # For record events, the record belongs to the current step (top of stack)
            # Override step_id to ensure records are stored under the correct step
            if _step_stack:
                step_id = _step_stack[-1]
                parent_step_id = _step_stack[-2] if len(_step_stack) > 1 else None
            logger.debug(f"Record event: using step_id={step_id} from stack (original: {step_report.step_id})")
        elif status == "running":
            # Step is starting - parent is current top of stack (if any)
            if _step_stack:
                parent_step_id = _step_stack[-1]
            _step_stack.append(step_id)
            logger.debug(f"Step stack after push: {_step_stack}")
        else:
            # Step is ending - find and remove it from stack
            if step_id in _step_stack:
                idx = _step_stack.index(step_id)
                # Parent is the step before this one in the stack
                if idx > 0:
                    parent_step_id = _step_stack[idx - 1]
                _step_stack.remove(step_id)
                logger.debug(f"Step stack after pop: {_step_stack}")

        # Build step report payload
        step_data = {
            "step_id": step_id,
            "step_name": step_id,
            "step_status": step_report.status.value,
            "start_time": step_report.start_time.isoformat() if step_report.start_time else None,
            "end_time": step_report.end_time.isoformat() if step_report.end_time else None,
            "parent_step_id": parent_step_id,
        }

        # Include git info when step starts (for version tracking)
        if status == "running" and not is_record_event:
            git_info = get_git_info()
            if git_info:
                step_data["git_info"] = git_info

        # Include record data if present
        if is_record_event:
            step_data["record"] = {
                "id": record.record_id,
                "status": record.status.value if hasattr(record.status, 'value') else str(record.status),
                "message": record.message,
                "metadata": record.metadata,
            }

        payload = {
            "action": "STEP_REPORT",
            "payload": {
                "step_report": step_data
            }
        }
        self._send(payload)

        if is_record_event:
            logger.info(f"Record report sent: {step_id} -> {record.record_id} ({record.status})")
        else:
            logger.info(f"Step report sent: {step_id} -> {step_report.status.value} (parent: {parent_step_id})")

    def _handle_status_change(self, event: RunStatusChangeEvent) -> None:
        """Send run status change to Ondemand."""
        # Map Thoughtful Status enum values (lowercase) to Ondemand webhook status
        status_map = {
            "running": "processing",
            "succeeded": "finished",
            "failed": "failed",
        }

        payload = {
            "action": "STATUS_CHANGE",
            "payload": {
                "status": status_map.get(event.status.value, event.status.value),
                "status_message": event.status_message,
            }
        }
        self._send(payload)
        logger.info(f"Run status sent: {event.status.value}")

    def _send(self, payload: Dict[str, Any], max_retries: int = 3) -> None:
        """Send payload to Ondemand webhook with retry on transient errors."""
        for attempt in range(max_retries + 1):
            try:
                response = self._session.post(
                    self.webhook_url,
                    json=payload,
                    timeout=10,
                )
                response.raise_for_status()
                return
            except requests.exceptions.RequestException as e:
                is_retryable = (
                    isinstance(e, requests.exceptions.ConnectionError)
                    or isinstance(e, requests.exceptions.Timeout)
                    or (hasattr(e, 'response') and e.response is not None
                        and e.response.status_code in (502, 503, 504))
                )
                if is_retryable and attempt < max_retries:
                    wait = 2 ** attempt  # 1s, 2s, 4s
                    logger.warning(f"Webhook failed (attempt {attempt + 1}/{max_retries + 1}), retrying in {wait}s: {e}")
                    time.sleep(wait)
                else:
                    logger.error(f"Webhook request failed after {attempt + 1} attempt(s): {e}")

    def send_raw(self, payload: Dict[str, Any]) -> None:
        """Send a raw payload to Ondemand (for custom actions)."""
        self._send(payload)


def connect_to_ondemand(
    run_id: Optional[str] = None,
    webhook_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Optional[OndemandStreamer]:
    """
    Connect the supervisor to the Ondemand platform.

    Args:
        run_id: The run ID in Ondemand
        webhook_url: The webhook URL for status updates
        api_key: Optional API key for authentication

    Returns:
        OndemandStreamer if connected, None if not configured
    """
    global _ondemand_streamer, _step_stack

    # Clear step stack for new run
    _step_stack = []

    if not run_id or not webhook_url:
        logger.info("Ondemand connection not configured - running in standalone mode")
        return None

    _ondemand_streamer = OndemandStreamer(run_id, webhook_url, api_key)
    shared_bus.subscribe(_ondemand_streamer.handle_event)

    logger.info(f"Connected to Ondemand: run_id={run_id}")
    return _ondemand_streamer


def get_streamer() -> Optional[OndemandStreamer]:
    """Get the current Ondemand streamer instance."""
    return _ondemand_streamer


def send_manifest(manifest_dict: Dict[str, Any]) -> None:
    """
    Send a manifest directly to Ondemand (bypassing supervisor).

    Use this for sending updated/dynamic manifests after the initial
    manifest has been sent by supervise().

    Args:
        manifest_dict: The manifest as a dictionary (must have 'workflow' key)
    """
    streamer = get_streamer()
    if not streamer:
        logger.warning("Cannot send manifest - not connected to Ondemand")
        return

    payload = {
        "action": "bot_manifest",
        "payload": {
            "bot_manifest": manifest_dict
        }
    }
    streamer.send_raw(payload)
    logger.info(f"Dynamic manifest sent with {len(manifest_dict.get('workflow', []))} top-level steps")


class supervised:
    """
    Context manager that handles all Ondemand setup and supervise integration.

    This is the recommended way to run an Ondemand agent. It:
    1. Parses CLI arguments (--run-id, --webhook-url) if not provided
    2. Reads API key from ONDEMAND_API_KEY environment variable
    3. Sets up run_id for state isolation
    4. Connects to Ondemand platform (if configured)
    5. Enters the Thoughtful supervise context
    6. Optionally emits run status on enter/exit based on first_task/last_task flags

    Usage:
        from ondemand import supervised

        # First task - emits RUNNING on enter, uses base manifest
        with supervised(task="initialize", first_task=True):
            build_dynamic_manifest()

        # Middle task - uses dynamic manifest from previous phase
        with supervised(task="process", use_dynamic_manifest=True):
            process()

        # Last task - emits SUCCEEDED/FAILED on exit
        with supervised(task="teardown", use_dynamic_manifest=True, last_task=True):
            teardown()

    Args:
        run_id: Run ID for state isolation (reads from --run-id if not provided)
        webhook_url: Webhook URL for Ondemand reporting (reads from --webhook-url if not provided)
        api_key: API key for webhook authentication (reads from ONDEMAND_API_KEY env var if not provided)
        manifest: Path to manifest.yaml file (default: "manifest.yaml"), used as fallback
        task: Task name for output subfolder (e.g., "initialize", "process", "teardown")
        first_task: If True, emits RUNNING status on enter
        last_task: If True, emits SUCCEEDED/FAILED status on exit
        use_dynamic_manifest: If True, looks for dynamic_manifest.yaml in output/{run_id}/ folder.
                              Falls back to default manifest if not found.
    """

    def __init__(
        self,
        run_id: Optional[str] = None,
        webhook_url: Optional[str] = None,
        api_key: Optional[str] = None,
        manifest: str = "manifest.yaml",
        task: Optional[str] = None,
        first_task: bool = False,
        last_task: bool = False,
        use_dynamic_manifest: bool = False,
    ):
        self.task = task
        self.first_task = first_task
        self.last_task = last_task
        self.use_dynamic_manifest = use_dynamic_manifest
        self._default_manifest = manifest
        self._supervise_context = None

        # Parse CLI args and env vars if not explicitly provided
        if run_id is None or webhook_url is None or api_key is None:
            cli_run_id, cli_webhook_url, cli_api_key = parse_args()
            run_id = run_id or cli_run_id
            webhook_url = webhook_url or cli_webhook_url
            api_key = api_key or cli_api_key

        # Set up state isolation (must be done before get_output_dir)
        if run_id:
            set_run_id(run_id)
            logger.info(f"Run ID: {run_id}")

        self.run_id = run_id
        self.webhook_url = webhook_url
        self.api_key = api_key

    def __enter__(self):
        # Suppress thoughtful's S3 artifact upload warning
        # We handle artifact upload ourselves via R2
        if not os.environ.get("ROBOCORP_HOME"):
            os.environ["ROBOCORP_HOME"] = "/tmp/robocorp"

        # Connect to Ondemand
        connect_to_ondemand(run_id=self.run_id, webhook_url=self.webhook_url, api_key=self.api_key)

        # Set current task for artifact management
        set_current_task(self.task)

        # Get task-specific output directory
        output_dir = get_output_dir()

        # Resolve manifest path (dynamic manifest is in base run directory)
        if self.use_dynamic_manifest:
            dynamic_manifest_path = get_base_output_dir() / "dynamic_manifest.yaml"
            if dynamic_manifest_path.exists():
                manifest = str(dynamic_manifest_path)
                logger.info(f"Using dynamic manifest: {manifest}")
            else:
                manifest = self._default_manifest
                logger.warning(f"Dynamic manifest not found at {dynamic_manifest_path}, falling back to {manifest}")
        else:
            manifest = self._default_manifest

        # Emit RUNNING if this is the first task
        if self.first_task:
            shared_bus.emit(RunStatusChangeEvent(status=Status.RUNNING))

            # Log git info prominently at the start of the run
            git_info = get_git_info()
            if git_info:
                branch = git_info.get("branch", "?")
                commit = git_info.get("commit_hash", "?")
                msg = git_info.get("commit_message", "")
                logger.info(f"Robot version: {branch} @ {commit} — {msg}")
            else:
                logger.warning("Robot version: could not read git info")

        # Enter supervise context (always multistep mode - we handle status ourselves)
        self._supervise_context = supervise(
            manifest=manifest,
            output_dir=output_dir,
            is_robocorp_multistep_run=True
        )
        self._supervise_context.__enter__()

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Record any exception that occurs (for cross-task tracking)
        # This allows the last_task to know if any previous task failed
        if exc_type is not None:
            task_name = self.task or "unknown"
            record_exception(task_name, exc_type, exc_val, exc_tb)
            logger.info(f"Exception recorded for task '{task_name}': {exc_type.__name__}: {exc_val}")

        # Let supervise handle its cleanup first
        try:
            self._supervise_context.__exit__(exc_type, exc_val, exc_tb)
        finally:
            # Upload artifacts for this task (incremental upload after each task)
            self._upload_task_artifacts()

            # Emit final status if this is the last task
            if self.last_task:
                # Check for exceptions: current task OR any previous task
                if exc_type is not None:
                    # Current task failed
                    error_msg = str(exc_val) if exc_val else "Run failed with exception"
                    shared_bus.emit(RunStatusChangeEvent(status=Status.FAILED, status_message=error_msg))
                    logger.info(f"Run status: FAILED (last_task) - {error_msg}")
                elif has_recorded_exceptions():
                    # A previous task failed - report failure with summary
                    error_msg = get_exception_summary() or "A previous task failed"
                    shared_bus.emit(RunStatusChangeEvent(status=Status.FAILED, status_message=''))
                    logger.error(f"Run status: FAILED - {error_msg}")
                else:
                    # No exceptions anywhere - report success
                    shared_bus.emit(RunStatusChangeEvent(status=Status.SUCCEEDED, status_message="Run completed successfully"))
                    logger.info("Run status: SUCCEEDED (last_task)")

                # Clean up local artifacts after final task
                self._cleanup_run_artifacts()

        # Don't suppress exceptions
        return False

    def _upload_task_artifacts(self):
        """Upload artifacts for the current task to R2 and send webhook."""
        if not self.run_id:
            logger.warning("No run_id set, skipping artifact upload")
            return

        if not self.task:
            logger.debug("No task name set, skipping task artifact upload")
            return

        task_output_dir = get_output_dir(self.task)
        if not task_output_dir.exists():
            logger.debug(f"Task output directory not found: {task_output_dir}")
            return

        # Upload only this task's artifacts (excluding console.txt which the
        # worker uploads after the subprocess finishes, so it has full output
        # including tracebacks that are printed after this __exit__ returns)
        uploaded_files = upload_task_artifacts(
            task_output_dir, self.run_id, self.task, exclude=["console.txt"]
        )

        # On the last task, also upload root-level files from output/{run_id}/
        # (e.g., dynamic_manifest.yaml, shared data files between tasks)
        if self.last_task:
            root_files = upload_root_artifacts(
                get_base_output_dir(), self.run_id, exclude=["dynamic_manifest.yaml"]
            )
            uploaded_files.extend(root_files)

        if not uploaded_files:
            logger.debug(f"No artifacts to upload for task {self.task}")
            return

        # Send webhook with artifact info
        streamer = get_streamer()
        if streamer:
            payload = {
                "action": "ARTIFACTS_UPLOADED",
                "payload": {
                    "artifacts": uploaded_files,
                    "total_count": len(uploaded_files),
                    "total_size": sum(f.get("size", 0) for f in uploaded_files),
                    "task": self.task,
                }
            }
            streamer.send_raw(payload)
            logger.info(f"Artifacts webhook sent for task '{self.task}': {len(uploaded_files)} files")
        else:
            logger.warning("No streamer available, artifacts uploaded but webhook not sent")

    def _cleanup_run_artifacts(self):
        """Delete all run artifacts to clean up server."""
        path = get_base_output_dir()
        try:
            shutil.rmtree("/".join(path.parts))
        except:
            logger.error(f"FATAL ERROR WHEN DEALING FILES FROM {'/'.join(path.parts)}")
            raise

def supervised_step(
    step_name: str,
    first_task: bool = False,
    last_task: bool = False,
    use_dynamic_manifest: bool = False,
) -> Callable:
    """
    Decorator that combines supervised() context and @step decorator.

    This is the recommended way to define steps in an Ondemand agent. It:
    1. Creates a supervised() context with the step name as the output folder
    2. Wraps the function with thoughtful's @step decorator
    3. Handles run status emission based on first_task/last_task flags

    Usage:
        from ondemand import supervised_step

        class Dora:
            @supervised_step("Initialize", first_task=True)
            def initialize_dora(self):
                # Output goes to: output/{run_id}/Initialize/
                ...

            @supervised_step("Process", use_dynamic_manifest=True)
            def process_dora(self):
                # Output goes to: output/{run_id}/Process/
                ...

            @supervised_step("Teardown", use_dynamic_manifest=True, last_task=True)
            def teardown_dora(self):
                # Output goes to: output/{run_id}/Teardown/
                # Emits SUCCEEDED/FAILED on exit
                ...

    Args:
        step_name: Name of the step (used for both @step decorator and output folder)
        first_task: If True, emits RUNNING status on enter
        last_task: If True, emits SUCCEEDED/FAILED status on exit
        use_dynamic_manifest: If True, looks for dynamic_manifest.yaml in output/{run_id}/ folder
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            with supervised(
                task=step_name,
                first_task=first_task,
                last_task=last_task,
                use_dynamic_manifest=use_dynamic_manifest,
            ):
                # Wrap with thoughtful's @step decorator
                stepped_func = step(step_name)(func)
                return stepped_func(*args, **kwargs)
        return wrapper
    return decorator
