"""Record tracking for individual work items within a step.

A record represents a single item being processed in a loop — for example,
one invoice out of many in a batch processing step.
"""

import json
import logging
import warnings
from dataclasses import dataclass, field
from typing import Dict, Optional

from ondemand.supervisor.reporting.status import Status
from ondemand.utils.json import JSONEncoder

logger = logging.getLogger(__name__)


@dataclass
class Record:
    """A single work item processed within a step.

    Args:
        record_id: Unique identifier for this record.
        status: Current processing status.
        message: Short description (max 120 chars recommended).
        metadata: Arbitrary key-value data for analysis and grouping.
    """

    record_id: str
    status: Status
    message: Optional[str] = field(default_factory=str)
    metadata: Optional[dict] = field(default_factory=dict)

    def __post_init__(self):
        if self.message and len(self.message) > 120:
            warnings.warn(
                "Record message exceeds 120 characters and may be truncated in the UI.",
                UserWarning,
                stacklevel=2,
            )
        _validate_metadata(self.metadata)

    def to_dict(self) -> dict:
        return {
            "id": self.record_id,
            "status": self.status.value,
            "message": self.message,
            "metadata": self.metadata,
        }

    # Backward compatibility
    __json__ = to_dict


def _validate_metadata(metadata: Optional[Dict]) -> None:
    """Validate that metadata is serializable JSON under 5 KB."""
    if not metadata:
        return
    try:
        encoded = json.dumps(metadata, cls=JSONEncoder)
        if len(encoded) > 5120:
            warnings.warn(
                "Record metadata exceeds 5 KB and may be truncated.",
                UserWarning,
                stacklevel=2,
            )
    except (TypeError, ValueError) as exc:
        logger.exception("Failed to serialize record metadata: %s", exc)
        warnings.warn(
            "Record metadata is not JSON-serializable.",
            UserWarning,
            stacklevel=2,
        )
