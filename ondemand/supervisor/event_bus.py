"""Event bus for supervisor lifecycle events.

Provides a simple pub/sub mechanism for step reports, manifest updates,
run status changes, and artifact uploads. Subscribers (e.g., webhook
streamers) receive events as they occur during execution.
"""

import logging
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Union

from ondemand.supervisor.reporting.status import Status
from ondemand.supervisor.reporting.step_report import StepReport

logger = logging.getLogger(__name__)


@dataclass
class StepReportChangeEvent:
    """Emitted when a step's report is created or updated."""
    step_report: StepReport


@dataclass
class NewManifestEvent:
    """Emitted when the workflow manifest is updated."""
    manifest: object  # Manifest type (avoid circular import)


@dataclass
class RunStatusChangeEvent:
    """Emitted when the overall run status changes."""
    status: Status
    status_message: Optional[str] = None


@dataclass
class ArtifactsUploadedEvent:
    """Emitted when artifacts have been uploaded to storage."""
    output_uri: str


Event = Union[
    StepReportChangeEvent,
    NewManifestEvent,
    RunStatusChangeEvent,
    ArtifactsUploadedEvent,
]


@dataclass
class EventBus:
    """Simple pub/sub event bus for supervisor lifecycle events.

    Subscribers are called synchronously in registration order.
    """

    subscribers: List[Callable[[Event], None]] = field(default_factory=list)

    def subscribe(self, callback: Callable[[Event], None]) -> None:
        """Register an event handler."""
        self.subscribers.append(callback)

    def emit(self, event: Event) -> None:
        """Broadcast an event to all subscribers."""
        logger.debug("emit event: %s", type(event).__name__)
        for subscriber in self.subscribers:
            try:
                subscriber(event)
            except Exception:
                logger.exception("Error in event subscriber")
