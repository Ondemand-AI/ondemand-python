"""W3C trace context reaches the GPU: serverless job input and dedicated pod env."""

import sys
import types

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from ondemand.shared import gpu_trace
from ondemand.shared import runpod as rp


@pytest.fixture
def spans():
    """An in-memory exporter on the global SDK provider (set once per process)."""
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield exporter
    exporter.clear()


def _traceparent(span) -> str:
    ctx = span.get_span_context()
    return f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-{int(ctx.trace_flags):02x}"


def test_no_current_span_means_no_context():
    assert gpu_trace.trace_carrier() == {}
    assert gpu_trace.trace_env() == {}


def test_carrier_and_env_carry_the_current_span(spans):
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("activity") as span:
        carrier = gpu_trace.trace_carrier()
        env = gpu_trace.trace_env()
    assert carrier["traceparent"] == _traceparent(span)
    assert env["TRACEPARENT"] == _traceparent(span)
    assert set(carrier) <= {"traceparent", "tracestate"}


class _Job:
    job_id = "job-123"

    def __init__(self):
        self._st = iter(["IN_QUEUE", "COMPLETED"])

    def status(self):
        return next(self._st)

    def output(self):
        return {"choices": []}

    def cancel(self):
        pass


def _fake_sdk(captured):
    class Endpoint:
        def __init__(self, endpoint_id):
            captured["endpoint"] = endpoint_id

        def run(self, payload):
            captured["payload"] = payload
            return _Job()

    return types.SimpleNamespace(Endpoint=Endpoint, api_key=None)


def test_serverless_run_puts_its_client_span_in_the_job_input(spans, monkeypatch):
    captured = {}
    monkeypatch.setattr(rp, "RUNPOD_AVAILABLE", True)
    monkeypatch.setattr(rp, "_runpod_sdk", _fake_sdk(captured))
    monkeypatch.setenv("HYPERDX_API_KEY", "k")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "classificacao-demo")
    client = rp.RunPodClient(api_key="x")

    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("activity") as parent:
        out = client.serverless_run(
            "ep-1", {"prompts": ["a", "b"], "adapter": {"name": "2jh", "r2_prefix": "p/"}},
            timeout=30, poll_interval=0,
        )
    assert out == {"choices": []}

    job_span = next(s for s in spans.get_finished_spans() if s.name == "runpod.serverless.job")
    assert job_span.kind == trace.SpanKind.CLIENT
    assert job_span.parent.span_id == parent.get_span_context().span_id
    assert job_span.attributes["peer.service"] == "ondemand-gpu"
    assert job_span.attributes["runpod.endpoint.id"] == "ep-1"
    assert job_span.attributes["runpod.job.id"] == "job-123"
    assert job_span.attributes["runpod.job.status"] == "COMPLETED"
    assert job_span.attributes["ondemand.gpu.prompts"] == 2
    assert job_span.attributes["ondemand.gpu.adapter"] == "2jh"

    obs = captured["payload"]["_obs"]
    assert obs["traceparent"] == _traceparent(job_span)
    assert obs["OTEL_SERVICE_NAME"] == "classificacao-demo"  # -> ondemand.automation on the GPU


def test_serverless_run_failure_is_recorded_and_reraised(spans, monkeypatch):
    class _Failed(_Job):
        def status(self):
            return "FAILED"

    captured = {}
    sdk = _fake_sdk(captured)
    sdk.Endpoint.run = lambda self, payload: _Failed()
    monkeypatch.setattr(rp, "RUNPOD_AVAILABLE", True)
    monkeypatch.setattr(rp, "_runpod_sdk", sdk)
    client = rp.RunPodClient(api_key="x")

    with pytest.raises(RuntimeError, match="FAILED"):
        client.serverless_run("ep-1", {"prompt": "a"}, timeout=30, poll_interval=0)
    job_span = next(s for s in spans.get_finished_spans() if s.name == "runpod.serverless.job")
    assert job_span.status.status_code == trace.StatusCode.ERROR
    assert job_span.attributes["runpod.job.status"] == "FAILED"


def test_serverless_run_without_a_span_still_submits(monkeypatch):
    captured = {}
    monkeypatch.setattr(rp, "RUNPOD_AVAILABLE", True)
    monkeypatch.setattr(rp, "_runpod_sdk", _fake_sdk(captured))
    monkeypatch.delenv("HYPERDX_API_KEY", raising=False)
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("DEPLOYMENT_ENVIRONMENT", raising=False)
    client = rp.RunPodClient(api_key="x")
    # No SDK provider span is current here; with the no-op API the job still runs.
    assert client.serverless_run("ep-1", {"prompt": "a"}, timeout=30, poll_interval=0) == {"choices": []}
    assert captured["payload"]["prompt"] == "a"


def test_pod_env_carries_traceparent(spans, monkeypatch):
    captured = {}

    def generate(**kwargs):
        captured.update(kwargs)
        return 'mutation { podFindAndDeployOnDemand(input: { imageName: "img" }) { id } }'

    pods = types.ModuleType("runpod.api.mutations.pods")
    pods.generate_pod_deployment_mutation = generate
    graphql = types.ModuleType("runpod.api.graphql")
    graphql.run_graphql_query = lambda q: {"errors": ["stop here"]}
    mutations = types.ModuleType("runpod.api.mutations")
    mutations.pods = pods
    api = types.ModuleType("runpod.api")
    api.mutations = mutations
    api.graphql = graphql
    root = types.ModuleType("runpod")
    root.api = api
    for name, mod in {
        "runpod": root, "runpod.api": api, "runpod.api.mutations": mutations,
        "runpod.api.mutations.pods": pods, "runpod.api.graphql": graphql,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setattr(rp, "RUNPOD_AVAILABLE", True)
    monkeypatch.setattr(rp, "_runpod_sdk", types.SimpleNamespace(api_key=None))
    client = rp.RunPodClient(api_key="x")

    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("RunActivity:train") as span:
        with pytest.raises(RuntimeError, match="stop here"):
            client.provision_pod(gpu_type="NVIDIA A40", image="img", name="train-wf", env={"DATASET_KEY": "d"})
    env = captured["env"]
    assert env["TRACEPARENT"] == _traceparent(span)
    assert env["DATASET_KEY"] == "d"
