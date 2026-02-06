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
"""

import logging
import time
from contextlib import contextmanager
from typing import Optional

# Custom log level between INFO (20) and WARNING (30)
SUCCESS = 25
logging.addLevelName(SUCCESS, "SUCCESS")


class OndemandLogger(logging.Logger):
    """Extended logger with extra levels and utilities for Ondemand robots."""

    def success(self, msg, *args, **kwargs):
        """Log a success message. Shows green in the console UI."""
        if self.isEnabledFor(SUCCESS):
            self._log(SUCCESS, msg, args, **kwargs)

    def section(self, title: str):
        """Log a section header. Shows cyan in the console UI."""
        self.info("#### %s", title)

    def step(self, action: str, target: Optional[str] = None):
        """Log a step action, optionally with a target name."""
        if target:
            self.info("[%s] %s", action, target)
        else:
            self.info("[%s]", action)

    def divider(self, char: str = "=", length: int = 60):
        """Log a visual divider line."""
        self.info(char * length)

    def summary(self, title: str, data: dict):
        """Log a summary block with key-value pairs."""
        self.divider()
        self.info(title)
        self.divider()
        for key, value in data.items():
            self.info("  %s: %s", key, value)
        self.divider()

    @contextmanager
    def timed(self, label: str):
        """Context manager that logs start and duration of a block.

        Usage:
            with logger.timed("Uploading files"):
                upload()
            # logs: "#### Uploading files"
            # logs: "SUCCESS - Uploading files completed in 3.2s"
        """
        self.section(label)
        start = time.time()
        try:
            yield
        except Exception:
            elapsed = time.time() - start
            self.error("%s FAILED after %.1fs", label, elapsed)
            raise
        else:
            elapsed = time.time() - start
            self.success("%s completed in %.1fs", label, elapsed)


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
