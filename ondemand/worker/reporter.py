"""
Workflow Reporter — manages step tree, records, logs, and artifacts for the portal UI.

The workflow maintains mutable state that the portal queries via Temporal's Query API.
This replaces the old manifest + webhook system with a Temporal-native approach.

Usage in a workflow:
    @workflow.defn
    class MyWorkflow:
        def __init__(self):
            self.reporter = WorkflowReporter()

        @workflow.query
        def get_progress(self) -> dict:
            return self.reporter.to_dict()

        @workflow.run
        async def run(self, input):
            # Define step tree
            self.reporter.add_step("extract", "Extrair Dados")
            self.reporter.add_step("validate", "Validar", parent="extract")

            # Update progress
            self.reporter.start_step("extract")
            self.reporter.add_record("extract", "file1.pdf", "success", "Processado OK")
            self.reporter.complete_step("extract")

Usage in an activity (via heartbeat):
    @activity.defn
    async def my_activity(input):
        # Activities can't modify workflow state directly.
        # Return step updates as part of the activity result,
        # and the workflow applies them.
        return {
            "result": "...",
            "step_updates": [
                {"step_id": "extract", "status": "completed"},
                {"record": {"step_id": "extract", "id": "file1.pdf", "status": "success", "message": "OK"}},
            ]
        }
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field

logger = logging.getLogger("ondemand.worker.reporter")


@dataclass
class Record:
    """A single record within a step (e.g., one file processed, one transaction classified)."""
    id: str
    status: str  # "success", "warning", "failed"
    message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "message": self.message,
            "metadata": self.metadata,
        }


@dataclass
class StepNode:
    """A step in the execution tree. Can have children (nested steps) and records."""
    id: str
    title: str
    description: str = ""
    status: str = "pending"  # pending, running, completed, failed, warning, skipped
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error_message: Optional[str] = None
    records: List[Record] = field(default_factory=list)
    children: List["StepNode"] = field(default_factory=list)
    parent_id: Optional[str] = None

    def to_dict(self) -> dict:
        result = {
            "step_id": self.id,
            "name": self.title,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error_message": self.error_message,
            "parent_step_id": self.parent_id,
            "metadata": {
                "records": [r.to_dict() for r in self.records],
            },
        }
        if self.children:
            result["children"] = [c.to_dict() for c in self.children]
        return result


class WorkflowReporter:
    """
    Manages the step tree, logs, and artifacts for a workflow execution.

    The workflow creates a reporter, defines steps, updates progress,
    and exposes it via @workflow.query for the portal to poll.
    """

    def __init__(self):
        self._steps: Dict[str, StepNode] = {}  # Flat lookup by step_id
        self._root_steps: List[str] = []  # Top-level step IDs in order
        self._logs: List[str] = []
        self._artifacts: List[Dict[str, Any]] = []
        self._current_step: Optional[str] = None
        self._status: str = "running"

    # ==================== Step Management ====================

    def add_step(
        self,
        step_id: str,
        title: str,
        description: str = "",
        parent: Optional[str] = None,
    ) -> None:
        """Add a step to the tree. If parent is specified, it becomes a child step."""
        node = StepNode(
            id=step_id,
            title=title,
            description=description,
            parent_id=parent,
        )
        self._steps[step_id] = node

        if parent and parent in self._steps:
            self._steps[parent].children.append(node)
        else:
            self._root_steps.append(step_id)

    def start_step(self, step_id: str) -> None:
        """Mark a step as running."""
        if step_id in self._steps:
            step = self._steps[step_id]
            step.status = "running"
            step.started_at = _now()
            self._current_step = step_id
            self.log(f"▶ {step.title}")

    def complete_step(self, step_id: str) -> None:
        """Mark a step as completed."""
        if step_id in self._steps:
            step = self._steps[step_id]
            step.status = "completed"
            step.completed_at = _now()
            if self._current_step == step_id:
                self._current_step = step.parent_id
            self.log(f"✓ {step.title}", level="SUCCESS")

    def fail_step(self, step_id: str, error: str = "") -> None:
        """Mark a step as failed."""
        if step_id in self._steps:
            step = self._steps[step_id]
            step.status = "failed"
            step.completed_at = _now()
            step.error_message = error
            if self._current_step == step_id:
                self._current_step = step.parent_id
            self.log(f"✗ {step.title}: {error}", level="ERROR")

    def warn_step(self, step_id: str) -> None:
        """Mark a step as completed with warnings."""
        if step_id in self._steps:
            step = self._steps[step_id]
            step.status = "warning"
            step.completed_at = _now()

    def skip_step(self, step_id: str) -> None:
        """Mark a step as skipped."""
        if step_id in self._steps:
            self._steps[step_id].status = "skipped"

    # ==================== Records ====================

    def add_record(
        self,
        step_id: str,
        record_id: str,
        status: str,
        message: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Add a record (individual item result) to a step."""
        if step_id in self._steps:
            self._steps[step_id].records.append(
                Record(
                    id=record_id,
                    status=status,
                    message=message,
                    metadata=metadata or {},
                )
            )

    # ==================== Logs ====================

    def log(self, message: str, level: str = "INFO", module: str | None = None) -> None:
        """Add a log line in standard format. Also prints to stdout for container logs.

        Module defaults to the current step title if one is active.
        """
        if module is None:
            if self._current_step and self._current_step in self._steps:
                module = self._steps[self._current_step].title
            else:
                module = "ondemand.worker"
        timestamp = _log_timestamp()
        line = f"{timestamp} - {module} - {level} - {message}"
        self._logs.append(line)
        logger.info(message)

    # ==================== Artifacts ====================

    def add_artifact(self, name: str, r2_key: str, size: int = 0, mime_type: str = "") -> None:
        """Register an artifact that was uploaded to R2."""
        self._artifacts.append({
            "name": name,
            "r2_key": r2_key,
            "size": size,
            "mime_type": mime_type,
        })

    # ==================== State Export ====================

    def to_dict(self) -> dict:
        """
        Export the full state as a dictionary.
        This is what @workflow.query returns to the portal.
        """
        # Build step tree from root steps
        steps = []
        for step_id in self._root_steps:
            if step_id in self._steps:
                steps.append(self._steps[step_id].to_dict())

        # Also build a flat list (what the portal's step_runs table expects)
        flat_steps = []
        for step in self._steps.values():
            flat_steps.append(step.to_dict())

        return {
            "status": self._status,
            "current_step": self._current_step,
            "steps": flat_steps,  # Flat list with parent_step_id for tree building
            "step_tree": steps,   # Pre-built tree
            "logs": self._logs,
            "artifacts": self._artifacts,
        }

    # ==================== Bulk Updates (from activity results) ====================

    def apply_updates(self, updates: List[dict]) -> None:
        """
        Apply step/record updates returned by an activity.

        Activities can't modify workflow state directly, so they return
        updates that the workflow applies after the activity completes.
        Updates can include 'timestamp' for accurate timing (set during activity execution).
        """
        for update in updates:
            if "step_id" in update and "status" in update:
                step_id = update["step_id"]
                status = update["status"]
                ts = update.get("timestamp")  # Real timestamp from activity execution

                if step_id in self._steps:
                    step = self._steps[step_id]
                    step.status = status

                    if status == "running":
                        step.started_at = ts or _now()
                        self._current_step = step_id
                    elif status in ("completed", "failed", "warning", "skipped"):
                        step.completed_at = ts or _now()
                        if status == "failed":
                            step.error_message = update.get("error", "")
                        if self._current_step == step_id:
                            self._current_step = step.parent_id

            if "record" in update:
                rec = update["record"]
                self.add_record(
                    rec["step_id"],
                    rec["id"],
                    rec["status"],
                    rec.get("message", ""),
                    rec.get("metadata"),
                )

            if "log" in update:
                self.log(update["log"])

            if "artifact" in update:
                art = update["artifact"]
                self.add_artifact(art["name"], art["r2_key"], art.get("size", 0), art.get("mime_type", ""))


def _now() -> str:
    """ISO timestamp for step started_at/completed_at (portal needs this format)."""
    return datetime.now(timezone.utc).isoformat()


def _log_timestamp() -> str:
    """Readable timestamp for log lines: 2026-03-31 17:25:03"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
