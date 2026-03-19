"""Main execution context manager for supervised runs.

The `supervise` context (a subclass with default instances) wraps
an entire agent run, handling manifest loading, report generation,
status streaming, and optional artifact upload on exit.

Example::

    from ondemand.supervisor import supervise

    with supervise():
        initialize()
        process()
        teardown()
"""

from __future__ import annotations

import datetime
import logging
import os
import pathlib
import warnings
from types import TracebackType
from typing import Callable, Optional, Type, Union
from urllib.parse import urlparse

import boto3

from ondemand.supervisor.event_bus import (
    ArtifactsUploadedEvent,
    EventBus,
    NewManifestEvent,
    RunStatusChangeEvent,
)
from ondemand.supervisor.manifest import Manifest
from ondemand.supervisor.reporting.report_builder import ReportBuilder
from ondemand.supervisor.reporting.status import Status

logger = logging.getLogger(__name__)


class MainContext:
    """Supervises an entire run: manifest, timing, reporting, and artifact upload.

    On enter: loads manifest, emits RUNNING status.
    On exit: builds the final Report, writes it to disk, uploads artifacts.

    Args:
        report_builder: Accumulates step reports for the final run report.
        manifest: Path to manifest.yaml or a Manifest instance.
        output_dir: Directory for the run report and manifest JSON.
        event_bus: Bus for emitting lifecycle events.
        upload_uri: Optional S3/R2 URI for artifact upload.
        callback: Optional function called with (context, report) on exit.
        is_robocorp_multistep_run: If True, skip run-level status streaming.
    """

    def __init__(
        self,
        report_builder: ReportBuilder,
        manifest: Union[Manifest, str, pathlib.Path],
        output_dir: Union[str, pathlib.Path],
        event_bus: EventBus,
        upload_uri: Optional[str] = None,
        callback: Optional[Callable] = None,
        is_robocorp_multistep_run: bool = False,
    ):
        self.report_builder = report_builder
        self.output_path = pathlib.Path(output_dir)
        self.upload_uri = upload_uri
        self.manifest_path = (
            manifest if isinstance(manifest, (str, pathlib.Path)) else None
        )
        self.manifest = self._parse_manifest(manifest)
        self.callback = callback
        self.event_bus = event_bus
        self.is_robocorp_multistep_run = is_robocorp_multistep_run

    @staticmethod
    def _parse_manifest(
        manifest: Union[Manifest, str, pathlib.Path],
    ) -> Optional[Manifest]:
        if isinstance(manifest, Manifest):
            return manifest
        try:
            return Manifest.from_file(pathlib.Path(manifest))
        except Exception:
            logger.exception("Could not read manifest")
            return None

    def __enter__(self) -> MainContext:
        if self.manifest:
            self.event_bus.emit(NewManifestEvent(manifest=self.manifest))

        if not self.is_robocorp_multistep_run:
            self._stream_run_status_change(Status.RUNNING)

        return self

    def set_run_status(
        self, status: Union[Status, str], message: Optional[str] = None
    ) -> None:
        """Set the final run status with an optional message."""
        if message and len(message) > 125:
            warnings.warn("Status messages over 125 characters may be truncated.")

        if not message or not message.strip():
            warnings.warn(
                "set_run_status called without a message. "
                "This will become required in a future release.",
                DeprecationWarning,
                stacklevel=2,
            )

        self.report_builder.set_run_status(status, message)
        self._stream_run_status_change(status, message)

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> bool:
        if exc_type:
            self.report_builder.run_had_exception = True

        work_report = self.report_builder.to_report()

        if not self.is_robocorp_multistep_run:
            self._stream_run_status_change(
                work_report.status, work_report.status_message
            )

        self.output_path.mkdir(parents=True, exist_ok=True)

        report_path = self._safe_report_path(file_prefix="run-report")
        work_report.write(report_path)

        if self.manifest:
            manifest_path = self.output_path / "manifest.json"
            self.manifest.write_to_json_file(manifest_path)

        if self.callback:
            self.callback(self, work_report)

        if self.upload_uri:
            try:
                self._upload_output_files(self.upload_uri)
            except Exception:
                logger.exception("Failed to upload output files")

        return False

    def _stream_run_status_change(
        self, status: Status, status_message: Optional[str] = None
    ) -> None:
        self.event_bus.emit(
            RunStatusChangeEvent(status=status, status_message=status_message)
        )

    def _upload_output_files(self, upload_uri: str) -> None:
        """Upload all files in output_dir to S3/R2."""
        s3_client = boto3.client("s3")
        parsed = urlparse(upload_uri.strip())
        bucket = parsed.hostname
        path = parsed.path.strip("/")

        for file in self.output_path.glob("*"):
            if file.is_file():
                try:
                    key = f"{path}/{file.name}" if path else file.name
                    s3_client.upload_file(str(file), bucket, key)
                except Exception:
                    logger.exception("Failed to upload %s", file)

        self.event_bus.emit(ArtifactsUploadedEvent(output_uri=upload_uri))

    def _safe_report_path(self, file_prefix: str) -> pathlib.Path:
        """Generate a timestamped, filesystem-safe report path."""
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H%M%S"
        )
        filename = f"{file_prefix}-{timestamp}.json"
        for char in [':', '*', '?', '"', '<', '>', '|', "'"]:
            filename = filename.replace(char, "_")
        return self.output_path / filename
