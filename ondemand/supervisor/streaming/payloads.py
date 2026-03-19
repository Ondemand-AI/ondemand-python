"""Webhook payload builders for supervisor event streaming."""

import datetime
from dataclasses import dataclass

from ondemand.supervisor.reporting.status import Status
from ondemand.supervisor.reporting.step_report import StepReport
from ondemand.supervisor.streaming.action import Action

__version__ = "1.0.0"


@dataclass
class Payload:
    """Base payload for all streaming webhook messages."""

    run_id: str
    action: Action
    client: str = "supervisor"
    version: str = __version__

    def __json__(self) -> dict:
        return {
            "run_id": self.run_id,
            "client": self.client,
            "version": self.version,
            "action": self.action.value,
            "payload": {
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()
            },
        }


class StepReportPayload(Payload):
    """Payload for a step execution report."""

    def __init__(self, step_report: StepReport, run_id: str):
        super().__init__(run_id=run_id, action=Action.STEP_REPORT)
        self.step_report = step_report

    def __json__(self) -> dict:
        data = super().__json__()
        data["payload"]["step_report"] = self.step_report.__json__()
        return data


class BotManifestPayload(Payload):
    """Payload for sending the workflow manifest."""

    def __init__(self, manifest, run_id: str):
        self.manifest = manifest
        super().__init__(run_id=run_id, action=Action.BOT_MANIFEST)

    def __json__(self) -> dict:
        data = super().__json__()
        data["payload"]["bot_manifest"] = self.manifest.__json__()
        return data


class ArtifactsUploadedPayload(Payload):
    """Payload for artifact upload notification."""

    def __init__(self, run_id: str, output_artifacts_uri: str):
        self.output_artifacts_uri = output_artifacts_uri
        super().__init__(run_id, action=Action.ARTIFACTS_UPLOADED)

    def __json__(self) -> dict:
        data = super().__json__()
        data["payload"]["output_artifacts_uri"] = self.output_artifacts_uri
        return data


class RunStatusChangePayload(Payload):
    """Payload for run-level status changes."""

    STATUS_MAP = {
        Status.RUNNING: "processing",
        Status.FAILED: "failed",
        Status.SUCCEEDED: "finished",
        Status.WARNING: "warning",
    }

    def __init__(self, run_id: str, status: Status, status_message: str = None):
        self.status = status
        self.status_message = status_message
        super().__init__(run_id, action=Action.STATUS_CHANGE)

    def __json__(self) -> dict:
        data = super().__json__()
        data["payload"]["status"] = self.STATUS_MAP.get(self.status, self.status.value)
        data["payload"]["status_message"] = self.status_message
        return data
