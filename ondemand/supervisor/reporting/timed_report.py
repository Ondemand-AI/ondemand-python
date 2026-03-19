"""Timed report base class for execution reports with duration tracking."""

from __future__ import annotations

import datetime
import math
from dataclasses import dataclass
from typing import Any, Dict, Optional


def _duration_isoformat(td: datetime.timedelta) -> str:
    """Convert timedelta to ISO 8601 duration string (e.g., PT1H30M15S)."""
    total_seconds = int(td.total_seconds())
    hours, remainder = divmod(abs(total_seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = ["PT"]
    if hours:
        parts.append(f"{hours}H")
    if minutes:
        parts.append(f"{minutes}M")
    parts.append(f"{seconds}S")
    return "".join(parts)


@dataclass
class TimedReport:
    """Base class for reports that track start/end time and duration."""

    start_time: datetime.datetime
    end_time: Optional[datetime.datetime] = None

    @property
    def duration(self) -> Optional[datetime.timedelta]:
        if self.end_time is None:
            return None
        return self.end_time - self.start_time

    @property
    def duration_isoformat(self) -> Optional[str]:
        if self.duration is None:
            return None
        return _duration_isoformat(self.duration)

    @property
    def duration_in_milliseconds(self) -> Optional[int]:
        if self.duration is None:
            return None
        return math.ceil(self.duration.total_seconds() * 1000)

    def __json__(self) -> Dict[str, Any]:
        return {
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration": self.duration_isoformat,
            "duration_in_ms": self.duration_in_milliseconds,
        }

    def to_dict(self) -> Dict[str, Any]:
        return self.__json__()
