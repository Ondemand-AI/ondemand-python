"""
Ondemand Logger

Custom logger with extra levels and utilities designed for robot execution.
Log output integrates with the Ondemand console UI color coding:
  - ERROR/FAILED  → red
  - WARNING       → yellow
  - SUCCESS       → green
  - #### / [...]  → cyan (headers/sections)

Usage:
    from ondemand.shared.logging import get_logger

    logger = get_logger(__name__)
    logger.success("Task completed")
    logger.section("Processing Companies")
    logger.step("Extrair Dados", "ABC Corp")

    with logger.timed("Uploading files"):
        upload()

Caller attribution
------------------
Every helper below routes through `_emit`, which passes an explicit
`stacklevel` to `Logger._log`. Without it, Python's `findCaller` stops at the
first frame outside the *stdlib* logging module — which would be this file —
and every record would be attributed to `ondemand/shared/logging.py`. That
matters beyond cosmetics: OpenTelemetry copies `record.pathname/funcName/lineno`
into the `code.*` attributes exported to HyperDX.

`_emit` is called by a public helper, which is called by robot code:
    robot → helper → _emit → _log
`_log` counts stacklevel from its own caller (`_emit` = 1), so the robot frame
is 3. Helpers that delegate to another helper add one level via `depth`.
"""

import logging
import sys
import time
from contextlib import contextmanager
from typing import Optional

# Custom log level between INFO (20) and WARNING (30)
SUCCESS = 25
logging.addLevelName(SUCCESS, "SUCCESS")

# Python 3.11 rewrote logging.findCaller: its stacklevel walk now counts the
# frame of the function that called _log, where <=3.10 started one frame further
# out. Robots run 3.11/3.13, but the package declares >=3.9, so resolve it here.
_OFFSET = 1 if sys.version_info >= (3, 11) else 0
# helper → _log            (e.g. .success())
_DIRECT_FRAME = 1 + _OFFSET
# helper → _emit → _log    (everything routed through _emit)
_ROBOT_FRAME = 2 + _OFFSET


def _success(self, msg, *args, **kwargs):
    """Log a success message. Shows green in the console UI."""
    if self.isEnabledFor(SUCCESS):
        kwargs.setdefault("stacklevel", _DIRECT_FRAME)
        self._log(SUCCESS, msg, args, **kwargs)


# Single definition of .success(), patched onto the base Logger class so it is
# available on *every* logger — including ones created before OndemandLogger was
# registered as the logger class (e.g. `logging.getLogger("demo")` at import
# time in a robot module). OndemandLogger inherits it.
logging.Logger.success = _success  # type: ignore[attr-defined]


class OndemandLogger(logging.Logger):
    """Extended logger with extra levels and utilities for Ondemand robots."""

    def _emit(self, level: int, msg, args=(), depth: int = 0):
        """Log with caller attribution pointing at robot code, not this file."""
        if self.isEnabledFor(level):
            self._log(level, msg, args, stacklevel=_ROBOT_FRAME + depth)

    def section(self, title: str, _depth: int = 0):
        """Log a section header. Shows cyan in the console UI."""
        self._emit(logging.INFO, "#### %s", (title,), _depth)

    def step(self, action: str, target: Optional[str] = None):
        """Log a step action, optionally with a target name."""
        if target:
            self._emit(logging.INFO, "[%s] %s", (action, target))
        else:
            self._emit(logging.INFO, "[%s]", (action,))

    def divider(self, char: str = "=", length: int = 60, _depth: int = 0):
        """Log a visual divider line."""
        self._emit(logging.INFO, char * length, (), _depth)

    def summary(self, title: str, data: dict):
        """Log a summary block with key-value pairs."""
        self.divider(_depth=1)
        self._emit(logging.INFO, "%s", (title,))
        self.divider(_depth=1)
        for key, value in data.items():
            self._emit(logging.INFO, "  %s: %s", (key, value))
        self.divider(_depth=1)

    @contextmanager
    def timed(self, label: str):
        """Context manager that logs start and duration of a block.

        Usage:
            with logger.timed("Uploading files"):
                upload()
            # logs: "#### Uploading files"
            # logs: "SUCCESS - Uploading files completed in 3.2s"
        """
        # +1 for contextlib's __enter__/__exit__ frame, which sits between the
        # generator body and the robot's `with` statement. Emitted directly
        # rather than via self.section() to keep enter and exit symmetric.
        self._emit(logging.INFO, "#### %s", (label,), 1)
        start = time.time()
        try:
            yield
        except Exception:
            elapsed = time.time() - start
            self._emit(logging.ERROR, "%s FAILED after %.1fs", (label, elapsed), 1)
            raise
        else:
            elapsed = time.time() - start
            self._emit(SUCCESS, "%s completed in %.1fs", (label, elapsed), 1)


# Register our custom logger class
logging.setLoggerClass(OndemandLogger)

# Default format matching existing robot convention
DEFAULT_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


def get_logger(name: str, level: int = logging.INFO) -> OndemandLogger:
    """Get an OndemandLogger instance.

    Args:
        name: Logger name (typically __name__)
        level: Logging level (default: INFO)

    Returns:
        Configured OndemandLogger instance
    """
    logger = logging.getLogger(name)

    # Configure root logger with our format if not already configured
    if not logging.root.handlers:
        logging.basicConfig(level=level, format=DEFAULT_FORMAT)

    logger.setLevel(level)
    return logger  # type: ignore[return-value]


def configure_logging(level: int = logging.INFO, fmt: Optional[str] = None):
    """Configure logging globally with Ondemand defaults.

    Call this once at the start of your robot to set up logging.

    Args:
        level: Logging level (default: INFO)
        fmt: Custom format string (default: Ondemand standard format)
    """
    logging.basicConfig(
        level=level,
        format=fmt or DEFAULT_FORMAT,
        force=True,
    )
