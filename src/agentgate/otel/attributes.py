"""gen_ai.agent.* decision-quality attribute constants — Observability Summit semconv.

Constraints C1 (binding): DriftWatch emits ONLY these names. There is no
`gen_ai.agent.drift.*` family. Upstream names are used verbatim; DriftWatch adds
exactly two items, both under the existing tree:
  * gen_ai.agent.gate.action                     (additive — three-way log/drop/block)
  * computed.anomaly.kind == "arg_schema_novel"  (additive enum value)
The score lives on the gen_ai.evaluation.result EVENT; everything else on the SPAN.

AgentGate agent-run telemetry (E13) is additive under the same tree: upstream `gen_ai.request.*`
verbatim plus `gen_ai.agent.tool.*` items. All emitted names live as constants here (no string
literals at the emit site) so the C1 allowlist is auditable in one place.
"""

# --- identity / task / tool ---
GEN_AI_AGENT_ID = "gen_ai.agent.id"
GEN_AI_AGENT_TASK_TYPE = "gen_ai.agent.task_type"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_AGENT_TOOL_CATEGORY = "gen_ai.agent.tool.category"
GEN_AI_AGENT_TOOL_PARAMETERS_HASH = "gen_ai.agent.tool.parameters_hash"
GEN_AI_AGENT_TOOL_RISK_SEVERITY = "gen_ai.agent.tool.risk_severity"

# --- baseline ---
GEN_AI_AGENT_BASELINE_ID = "gen_ai.agent.baseline.id"
GEN_AI_AGENT_BASELINE_EXPECTED_TOOLS = "gen_ai.agent.baseline.expected_tools"
GEN_AI_AGENT_BASELINE_MATCH = "gen_ai.agent.baseline.match"

# --- computed anomaly ---
GEN_AI_AGENT_COMPUTED_ANOMALY = "gen_ai.agent.computed.anomaly"
GEN_AI_AGENT_COMPUTED_ANOMALY_KIND = "gen_ai.agent.computed.anomaly.kind"

# --- gate / enforcement (.action is the DriftWatch additive item) ---
GEN_AI_AGENT_GATE_ACTION = "gen_ai.agent.gate.action"
GEN_AI_AGENT_GATE_BLOCKED = "gen_ai.agent.gate.blocked"
GEN_AI_AGENT_GATE_REASON = "gen_ai.agent.gate.reason"
# E11: True when the gate fired on the DECLARED contract (deterministic, configure-layer) rather
# than the statistical baseline — so the two independent signals are distinguishable in tooling.
GEN_AI_AGENT_GATE_DECLARED = "gen_ai.agent.gate.declared"

# --- scope ---
GEN_AI_AGENT_SCOPE_ESCALATION_ATTEMPTED = "gen_ai.agent.scope.escalation_attempted"

# --- evaluation result event ---
GEN_AI_EVALUATION_RESULT_EVENT = "gen_ai.evaluation.result"
GEN_AI_EVALUATION_NAME = "gen_ai.evaluation.name"
GEN_AI_EVALUATION_SCORE_VALUE = "gen_ai.evaluation.score.value"
GEN_AI_EVALUATION_SCORE_LABEL = "gen_ai.evaluation.score.label"
GEN_AI_EVALUATION_EXPLANATION = "gen_ai.evaluation.explanation"

# evaluation.name vocabulary
EVAL_BASELINE_DEVIATION = "baseline_deviation"
EVAL_DANGER_DETECTED = "danger_detected"
EVAL_INVERSE_SCALING_TREND = "inverse_scaling_trend"

# --- AgentGate agent-run (E13, additive): upstream gen_ai.request.* verbatim + gen_ai.agent.tool.* ---
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"            # upstream GenAI semconv (verbatim)
GEN_AI_AGENT_TOOLS = "gen_ai.agent.tools"                # the tool set offered to the agent
GEN_AI_AGENT_TOOL_CALL_EVENT = "gen_ai.agent.tool.call"  # per-tool-call span event
GEN_AI_AGENT_TOOL_ALLOWED = "gen_ai.agent.tool.allowed"  # was the call bound (creation-driven)
# anomaly.kind value added by the LLM cross-check (§4c)
ANOMALY_LLM_CROSS_CHECK_MISMATCH = "llm_cross_check_mismatch"


def execute_tool_span(tool: str) -> str:
    """Canonical span name template."""
    return f"execute_tool {tool}"
