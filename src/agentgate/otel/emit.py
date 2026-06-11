"""Emit an AgentGate agent run as the gen_ai.agent.* schema (a span + tool-call events).

AgentGate-side telemetry only (E13 §4b): the agent identity, model, bound tools, the per-run
tool-call trace, and — for a dynamic delegation gate — a declared violation. Constraints C1: only
the fixed gen_ai.agent.* names (no drift.*); all names live in `attributes`. No endpoint → pure dict
builder (tests/offline). The drift-core emit methods (statistical decision, declared, cross-check)
live in DriftWatch, not here — AgentGate observes agent runs, DriftWatch governs tool calls.
"""
from __future__ import annotations

from . import attributes as A


class Emitter:
    """Pushes the gen_ai.agent.* schema to an OTLP endpoint, or no-ops if OTel isn't available."""

    def __init__(self, service_name: str = "agentgate", endpoint: str | None = None):
        self.service_name = service_name
        self.endpoint = endpoint
        self._tracer = None
        self._m_decisions = None
        self._m_anomaly = None
        self._m_score = None
        # Only wire a live OTLP exporter when an endpoint is explicitly configured. With no endpoint
        # (tests, demos, offline) we stay a pure dict builder — no export thread, no global provider.
        if not endpoint:
            return
        try:  # optional dependency — interceptor/codegen extra
            from opentelemetry import metrics, trace
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
                OTLPMetricExporter,
            )
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            from opentelemetry.sdk.metrics.view import (
                ExplicitBucketHistogramAggregation,
                View,
            )
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            # The collector listens on plaintext gRPC; the OTLP exporter defaults to TLS. Use insecure
            # unless the endpoint explicitly opts into TLS via an https:// scheme.
            insecure = not endpoint.startswith("https://")
            resource = Resource.create({"service.name": service_name})

            provider = TracerProvider(resource=resource)
            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=insecure))
            )
            trace.set_tracer_provider(provider)
            self._tracer = trace.get_tracer(service_name)

            # Metrics surface in Prometheus as <service_name>_decisions_total / _anomaly_total /
            # _score_value_bucket — the names the Grafana dashboard panels query.
            reader = PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=endpoint, insecure=insecure),
                export_interval_millis=5000,
            )
            score_view = View(
                instrument_name=f"{service_name}.score.value",
                aggregation=ExplicitBucketHistogramAggregation(
                    [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
                ),
            )
            metrics.set_meter_provider(
                MeterProvider(resource=resource, metric_readers=[reader], views=[score_view])
            )
            meter = metrics.get_meter(service_name)
            self._m_decisions = meter.create_counter(
                f"{service_name}.decisions", description="agent runs by gate.action")
            self._m_anomaly = meter.create_counter(
                f"{service_name}.anomaly", description="runs by computed.anomaly.kind")
            self._m_score = meter.create_histogram(
                f"{service_name}.score.value", description="normalized score [0,1]")
        except Exception:  # noqa: BLE001 — graceful: pure dict builder if OTel absent
            self._tracer = None

    def emit_agent_run(self, *, agent_id: str, task_type: str, model: str = "",
                       tools: list[str] | tuple[str, ...] = (),
                       tool_calls: list[dict] | tuple[dict, ...] = (),
                       violation: dict | None = None,
                       attributes: tuple[str, ...] | list[str] | None = None) -> dict:
        """Emit one AgentGate agent run as a `gen_ai.agent.*` span (E13 observability, §4b).

        Carries agent identity, model, bound tools, the per-run tool-call trace (span events), and —
        for a dynamic delegation gate — a declared violation (`gate.declared=true`,
        `anomaly.kind=delegation_violation`). No endpoint → pure dict builder (returns attrs for
        tests). `attributes` is the CRD-configurable allow-list: `None`/`()`/`["*"]` → all;
        `["none"]` → emit nothing; else only the listed keys. C1-safe — only ever a subset.
        """
        allow = tuple(attributes or ())
        if "none" in allow:
            return {"span": {}, "emitted": False}        # telemetry off for this org
        allow_all = (not allow) or ("*" in allow)

        span_attrs: dict[str, object] = {
            A.GEN_AI_AGENT_ID: agent_id,
            A.GEN_AI_AGENT_TASK_TYPE: task_type,
        }
        if model:
            span_attrs[A.GEN_AI_REQUEST_MODEL] = model
        if tools:
            span_attrs[A.GEN_AI_AGENT_TOOLS] = list(tools)
        if violation:
            span_attrs[A.GEN_AI_AGENT_GATE_DECLARED] = True
            span_attrs[A.GEN_AI_AGENT_GATE_BLOCKED] = True
            span_attrs[A.GEN_AI_AGENT_GATE_REASON] = violation.get("reason", "")
            span_attrs[A.GEN_AI_AGENT_COMPUTED_ANOMALY] = True
            span_attrs[A.GEN_AI_AGENT_COMPUTED_ANOMALY_KIND] = "delegation_violation"
        if not allow_all:
            span_attrs = {k: v for k, v in span_attrs.items() if k in allow}
        emit_tool_events = allow_all or (A.GEN_AI_AGENT_TOOL_CALL_EVENT in allow)

        if self._tracer is not None:  # pragma: no cover - needs OTel + collector
            with self._tracer.start_as_current_span(f"agent.run {agent_id}") as span:
                for k, v in span_attrs.items():
                    span.set_attribute(k, v)
                if emit_tool_events:
                    for tc in tool_calls:
                        span.add_event(A.GEN_AI_AGENT_TOOL_CALL_EVENT,
                                       attributes={A.GEN_AI_TOOL_NAME: tc.get("tool", ""),
                                                   A.GEN_AI_AGENT_TOOL_ALLOWED: bool(tc.get("ok"))})
        if self._m_decisions is not None:  # pragma: no cover - needs OTel + collector
            self._m_decisions.add(1, {"gate_action": "block" if violation else "log"})
            if violation:
                self._m_anomaly.add(1, {"anomaly_kind": "delegation_violation"})
        return {"span": span_attrs, "emitted": True}
