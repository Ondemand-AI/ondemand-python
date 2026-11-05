"""Hand the robot's W3C trace context to a GPU workload on RunPod.

The GPU images (``Ondemand-AI/ondemand-gpu``) report as their own service,
``ondemand-gpu``, and start their spans as children of the robot's span, so one
trace follows robot -> GPU. How the context travels depends on the lane:

  - **serverless**: RunPod's queue carries no HTTP headers, so the context rides in
    the job input, next to the obs env: ``_obs.traceparent`` / ``_obs.tracestate``.
    ``serverless_job_span`` opens the robot-side CLIENT span the GPU's job span
    hangs under.
  - **dedicated pod** (training, serving): the pod env gets ``TRACEPARENT`` /
    ``TRACESTATE``, the names vLLM itself reads for its loading spans. Serving
    requests also carry ``traceparent`` per HTTP call, via the httpx
    instrumentation ondemand-obs installs.

Best-effort: without OpenTelemetry, or with no valid span current, every helper
returns empty and the GPU just starts a root span. Old images ignore the keys.
"""

import contextlib
import logging
from typing import Any, Dict, Iterator, Optional

logger = logging.getLogger(__name__)

# The GPU-side service name; also `peer.service` on the robot's CLIENT span, which
# is what draws the robot -> GPU edge on a service map.
GPU_SERVICE_NAME = "ondemand-gpu"

_TRACE_KEYS = ("traceparent", "tracestate")


def trace_carrier() -> Dict[str, str]:
    """The current span as W3C headers ``{"traceparent", "tracestate"?}``, or {}.

    Only the trace-context keys: baggage never leaves the robot.
    """
    try:
        from opentelemetry import trace
        from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

        if not trace.get_current_span().get_span_context().is_valid:
            return {}
        carrier: Dict[str, str] = {}
        TraceContextTextMapPropagator().inject(carrier)
        return {k: v for k, v in carrier.items() if k in _TRACE_KEYS and v}
    except Exception:  # noqa: BLE001 - telemetry must never break a GPU call
        return {}


def trace_env() -> Dict[str, str]:
    """``trace_carrier`` as pod env: ``TRACEPARENT`` / ``TRACESTATE``."""
    return {k.upper(): v for k, v in trace_carrier().items()}


@contextlib.contextmanager
def serverless_job_span(endpoint_id: str, payload: Optional[Dict[str, Any]] = None) -> Iterator[Any]:
    """Robot-side CLIENT span around one serverless job (submit + poll).

    Yields the span (or None without OpenTelemetry). Inject ``trace_carrier()``
    inside the block so the GPU's job span is its child. An exception raised in
    the block is recorded on the span and re-raised unchanged.
    """
    try:
        from opentelemetry import trace
        from opentelemetry.trace import SpanKind

        tracer = trace.get_tracer("ondemand.shared.runpod")
    except Exception:  # noqa: BLE001
        yield None
        return

    attrs: Dict[str, Any] = {
        "peer.service": GPU_SERVICE_NAME,
        "runpod.endpoint.id": endpoint_id or "",
        "ondemand.gpu.kind": "serverless",
    }
    p = payload or {}
    prompts = p.get("prompts")
    if isinstance(prompts, list):
        attrs["ondemand.gpu.prompts"] = len(prompts)
    elif p.get("prompt"):
        attrs["ondemand.gpu.prompts"] = 1
    adapter = p.get("adapter")
    if isinstance(adapter, dict) and adapter.get("name"):
        attrs["ondemand.gpu.adapter"] = str(adapter["name"])

    with tracer.start_as_current_span(
        "runpod.serverless.job", kind=SpanKind.CLIENT, attributes=attrs,
    ) as span:
        yield span


def set_span_attribute(span: Any, key: str, value: Any) -> None:
    """``span.set_attribute`` that tolerates a None span and never raises."""
    if span is None or value is None:
        return
    try:
        span.set_attribute(key, value)
    except Exception:  # noqa: BLE001
        pass
