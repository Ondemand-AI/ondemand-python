"""HTTP webhook streamer for supervisor events.

Subscribes to the EventBus and forwards events as JSON payloads
to a configured callback URL via HTTP POST.
"""

from __future__ import annotations

import json
import logging
from typing import Dict, Optional

import requests

from ondemand.supervisor.event_bus import (
    ArtifactsUploadedEvent,
    Event,
    NewManifestEvent,
    RunStatusChangeEvent,
    StepReportChangeEvent,
)
from ondemand.supervisor.streaming.payloads import (
    ArtifactsUploadedPayload,
    BotManifestPayload,
    Payload,
    RunStatusChangePayload,
    StepReportPayload,
)
from ondemand.utils.json import JSONEncoder

logger = logging.getLogger(__name__)

POST_TIMEOUT_SECONDS = 10


class Streamer:
    """Streams supervisor events to a callback URL via HTTP POST.

    Args:
        run_id: The current run identifier.
        callback_url: URL to POST event payloads to.
        headers: Optional HTTP headers (e.g., auth tokens).
    """

    def __init__(
        self,
        run_id: str,
        callback_url: str,
        headers: Optional[Dict[str, str]] = None,
    ):
        self.run_id = run_id
        self.callback_url = callback_url
        self._session = requests.Session()
        if headers:
            self._session.headers.update(headers)

    def handle_event(self, event: Event) -> None:
        """Convert an event to a payload and POST it."""
        payload: Optional[Payload] = None

        if isinstance(event, StepReportChangeEvent):
            payload = StepReportPayload(
                step_report=event.step_report,
                run_id=self.run_id,
            )
        elif isinstance(event, NewManifestEvent):
            payload = BotManifestPayload(
                manifest=event.manifest,
                run_id=self.run_id,
            )
        elif isinstance(event, ArtifactsUploadedEvent):
            payload = ArtifactsUploadedPayload(
                run_id=self.run_id,
                output_artifacts_uri=event.output_uri,
            )
        elif isinstance(event, RunStatusChangeEvent):
            payload = RunStatusChangePayload(
                run_id=self.run_id,
                status=event.status,
                status_message=event.status_message,
            )
        else:
            logger.warning("Unhandled event type: %s", type(event).__name__)
            return

        if payload:
            self.send(payload)

    def send(self, payload: Payload) -> Optional[requests.Response]:
        """POST a payload to the callback URL."""
        message = json.loads(json.dumps(payload.__json__(), cls=JSONEncoder))

        try:
            response = self._session.post(
                self.callback_url,
                json=message,
                timeout=POST_TIMEOUT_SECONDS,
            )
            logger.debug(
                "Streamer POST %s → %d", self.callback_url, response.status_code
            )
            return response
        except Exception:
            logger.exception("Failed to stream event to %s", self.callback_url)
            return None

    def send_raw(self, payload: dict) -> Optional[requests.Response]:
        """POST a raw dict payload (for custom messages)."""
        try:
            response = self._session.post(
                self.callback_url,
                json=payload,
                timeout=POST_TIMEOUT_SECONDS,
            )
            return response
        except Exception:
            logger.exception("Failed to stream raw payload to %s", self.callback_url)
            return None
