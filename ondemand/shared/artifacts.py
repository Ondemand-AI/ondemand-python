"""
Artifact management for passing data between tasks.

Each run is isolated by run_id, and each task has its own output folder.
- Output directory: output/{run_id}/{task}/
- Artifacts can be loaded from any task by specifying the task name
"""

import json
from pathlib import Path
from typing import Optional, Union

# Global state
_run_id: Optional[str] = None
_current_task: Optional[str] = None


def set_run_id(run_id: str) -> None:
    """Set the run ID for this execution."""
    global _run_id
    _run_id = run_id


def get_run_id() -> str:
    """Get the current run ID, defaults to 'local' for standalone runs."""
    return _run_id or "local"


def set_current_task(task: Optional[str]) -> None:
    """Set the current task name (called by supervised context manager)."""
    global _current_task
    _current_task = task


def get_current_task() -> Optional[str]:
    """Get the current task name."""
    return _current_task


def get_base_output_dir() -> Path:
    """
    Get the base run output directory (output/{run_id}/).

    Use this for artifacts that need to be shared across all tasks,
    like dynamic_manifest.yaml.
    """
    output_dir = Path("output") / get_run_id()
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def get_output_dir(task: Optional[str] = None) -> Path:
    """
    Get task-specific output directory.

    Args:
        task: Task name. If None, uses current task.
              If no current task is set, returns base run directory.

    Returns:
        Path to output/{run_id}/{task}/ or output/{run_id}/ if no task
    """
    base_dir = Path("output") / get_run_id()

    # Use provided task, or fall back to current task
    effective_task = task or _current_task

    if effective_task:
        output_dir = base_dir / effective_task
    else:
        output_dir = base_dir

    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def save_artifact(data: Union[dict, list], filename: str = "state.json") -> Path:
    """
    Save artifact to the current task's output directory.

    Args:
        data: Dictionary or list to save as JSON
        filename: Name of the artifact file (default: state.json)

    Returns:
        Path to the saved artifact file

    Examples:
        # In Initialize task, saves to output/{run_id}/Initialize/state.json
        save_artifact({"companies": companies})

        # Save with custom filename
        save_artifact(results, "results.json")
    """
    path = get_output_dir() / filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return path


def load_artifact(filename: str = "state.json", task: Optional[str] = None) -> Union[dict, list]:
    """
    Load artifact from a task's output directory.

    Args:
        filename: Name of the artifact file (default: state.json)
        task: Task name to load from. If None, loads from current task.

    Returns:
        Dictionary or list with the saved data

    Examples:
        # Load from current task's folder
        state = load_artifact()

        # Load from Initialize task's folder
        state = load_artifact(task="Initialize Dora")

        # Load results from Process task
        results = load_artifact("results.json", task="Process Dora")
    """
    path = get_output_dir(task) / filename
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# Exception tracking across RCC tasks
EXCEPTIONS_FILE = "exceptions.log"


def record_exception(task: str, exc_type, exc_val, exc_tb) -> Path:
    """
    Record an exception to a shared file for cross-task tracking.

    Since each RCC task runs in a separate Python process, we need
    to persist exceptions to a file so the last_task can check if
    any previous task failed.

    Args:
        task: The task name where the exception occurred
        exc_type: Exception type
        exc_val: Exception value
        exc_tb: Exception traceback

    Returns:
        Path to the exceptions file
    """
    import traceback
    from datetime import datetime

    path = get_base_output_dir() / EXCEPTIONS_FILE

    # Format exception info
    tb_lines = traceback.format_exception(exc_type, exc_val, exc_tb)
    tb_str = "".join(tb_lines)

    entry = f"""
================================================================================
Task: {task}
Time: {datetime.now().isoformat()}
Exception: {exc_type.__name__}: {exc_val}
--------------------------------------------------------------------------------
{tb_str}
"""

    # Append to file (creates if doesn't exist)
    with open(path, "a", encoding="utf-8") as f:
        f.write(entry)

    return path


def has_recorded_exceptions() -> bool:
    """
    Check if any exceptions have been recorded during this run.

    Returns:
        True if exceptions file exists and has content
    """
    path = get_base_output_dir() / EXCEPTIONS_FILE
    return path.exists() and path.stat().st_size > 0


def get_recorded_exceptions() -> Optional[str]:
    """
    Get the content of recorded exceptions.

    Returns:
        Content of exceptions file, or None if no exceptions recorded
    """
    if not has_recorded_exceptions():
        return None

    path = get_base_output_dir() / EXCEPTIONS_FILE
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def get_exception_summary() -> Optional[str]:
    """
    Get a summary of recorded exceptions (first line of each).

    Returns:
        Summary string, or None if no exceptions recorded
    """
    content = get_recorded_exceptions()
    if not content:
        return None

    # Extract "Exception:" lines
    lines = content.split("\n")
    exceptions = [line for line in lines if line.startswith("Exception:")]

    if not exceptions:
        return "Unknown exceptions occurred"

    if len(exceptions) == 1:
        return exceptions[0].replace("Exception: ", "")

    return f"{len(exceptions)} Exceptions occurred: {exceptions[0].replace('Exception: ', '')} (and {len(exceptions) - 1} more)"


# Backward compatibility aliases
save_state = save_artifact
load_state = load_artifact
