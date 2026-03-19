"""Timer utilities for tracking execution duration."""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Optional


@dataclass
class TimerResult:
    """Result of a completed timer with start, end, and duration."""

    start: datetime.datetime
    end: datetime.datetime
    duration: datetime.timedelta


@dataclass
class Timer:
    """Tracks execution timing with start/end and duration computation."""

    start_time: Optional[datetime.datetime] = None
    end_time: Optional[datetime.datetime] = None
    duration: Optional[datetime.timedelta] = None

    def start(self) -> datetime.datetime:
        """Record the start time. Returns the start timestamp."""
        self.start_time = datetime.datetime.now(datetime.timezone.utc)
        return self.start_time

    def end(self) -> TimerResult:
        """Record the end time and compute duration.

        Returns:
            TimerResult with start, end, and duration.
        """
        self.end_time = datetime.datetime.now(datetime.timezone.utc)
        self.duration = self.end_time - self.start_time
        return TimerResult(
            start=self.start_time,
            end=self.end_time,
            duration=self.duration,
        )
