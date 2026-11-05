"""Activity failures reach Better Stack Errors (via ondemand-obs) and are
re-raised unchanged. No network, no Temporal server."""

import asyncio
import types

import pytest
from temporalio.exceptions import ApplicationError, CancelledError

from ondemand.worker import base


class _Next:
    def __init__(self, result=None, exc=None):
        self.result, self.exc = result, exc

    def init(self, outbound):
        pass

    async def execute_activity(self, input):
        if self.exc is not None:
            raise self.exc
        return self.result


@pytest.fixture
def captured(monkeypatch):
    calls = []
    monkeypatch.setattr(base, "_capture_exception", lambda exc, tags=None: calls.append((exc, tags)))
    info = types.SimpleNamespace(
        workflow_id="wf-1", workflow_run_id="run-1", workflow_type="DemoWorkflow",
        activity_type="process_company", attempt=2,
    )
    monkeypatch.setattr(base.activity, "info", lambda: info)
    return calls


def _run(interceptor):
    return asyncio.run(interceptor.execute_activity(object()))


def test_failure_is_captured_with_attempt_and_reraised_unchanged(captured):
    exc = ApplicationError("bank portal refused login", type="LoginError")
    with pytest.raises(ApplicationError) as raised:
        _run(base._ErrorCaptureActivityInterceptor(_Next(exc=exc)))

    assert raised.value is exc
    ((got, tags),) = captured
    assert got is exc
    assert tags["TemporalWorkflowID"] == "wf-1"
    assert tags["TemporalRunID"] == "run-1"
    assert tags["temporal.workflow_type"] == "DemoWorkflow"
    assert tags["temporal.activity_type"] == "process_company"
    assert tags["temporal.attempt"] == "2"
    assert tags["ondemand.failure"] == "activity"


def test_success_is_not_captured(captured):
    assert _run(base._ErrorCaptureActivityInterceptor(_Next(result=42))) == 42
    assert captured == []


@pytest.mark.parametrize("exc", [CancelledError("worker shutdown"), asyncio.CancelledError()])
def test_cancellation_is_not_captured(captured, exc):
    with pytest.raises(type(exc)):
        _run(base._ErrorCaptureActivityInterceptor(_Next(exc=exc)))
    assert captured == []


def test_error_interceptor_is_innermost(monkeypatch):
    import ondemand_obs

    monkeypatch.setattr(ondemand_obs, "is_configured", lambda: True)
    monkeypatch.setattr(ondemand_obs, "get_temporal_tracing_interceptor", lambda: "tracing")

    chain = base._build_interceptors()

    assert isinstance(chain[0], base._OndemandInterceptor)
    assert chain[1] == "tracing"
    # temporalio wraps in reverse, so the last one runs closest to the activity,
    # inside the tracing span.
    assert isinstance(chain[-1], base._ErrorCaptureInterceptor)


def test_capture_goes_through_ondemand_obs_once(monkeypatch):
    """The real path: ondemand_obs.capture_exception dedupes by object."""
    import ondemand_obs
    from ondemand_obs import errors

    sent = []
    monkeypatch.setattr(errors, "_enabled", True)
    monkeypatch.setattr(
        "sentry_sdk.capture_exception", lambda exc, **kw: sent.append((exc, kw)) or "evt-1"
    )
    exc = ValueError("x")
    base._capture_exception(exc, {"ondemand.failure": "activity"})
    base._capture_exception(exc, {"ondemand.failure": "worker"})

    assert len(sent) == 1
    assert sent[0][1]["tags"] == {"ondemand.failure": "activity"}


def test_helpers_are_noops_without_dsn(monkeypatch):
    from ondemand_obs import errors

    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.setattr(errors, "_enabled", False)
    base._configure_error_tracking("svc")
    base._capture_exception(ValueError("x"))
    base._flush_errors()
    assert not errors.error_tracking_enabled()
