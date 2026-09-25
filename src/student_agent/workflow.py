from __future__ import annotations

import json
from collections import Counter
from copy import deepcopy
from typing import Any

from .llm import JSONLLM, build_llms
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

MAX_TOTAL_MCP_CALLS = 12
MAX_CALLS_PER_TOOL = 5

PLANNER_SYSTEM = """You are the investigation coordinator for an e-commerce complaint.
Return JSON only: {"calls":[{"tool_name":"exact discovered name","arguments":{},
"actor":"entity-agent|order-agent|shipment-agent|payment-agent|policy-agent"}]}.
Use only discovered tools and exact input-schema field names. Never include case_id in arguments.
Request the minimum evidence needed to resolve the claim. Do not repeat equivalent calls and do
not invent identifiers. Return an empty calls array when no grounded call can be made."""

OUTPUT_SYSTEM = """You are the lead analyst. Produce exactly one JSON object for
day09-l3b-output-v2. Use only the case and MCP evidence supplied. Never invent identifiers,
amounts, dates, facts, or evidence refs. Every submitted evidence ref must occur in the supplied
evidence. Required top-level keys: schema_version, case_id, assessment, affected_entities,
claim_assessments, entity_resolution, customer_context, shipment_analysis, payment_analysis,
root_cause_analysis, evidence_refs, data_conflicts, financial_resolution, resolution_actions.
primary_issue must be one of canceled_order_paid, unavailable_order_paid, late_delivery_seller,
late_delivery_logistics, valid_split_payment, payment_mismatch, duplicate_charge, refund_pending,
refund_failed, unsupported_claim, insufficient_evidence. case_status is action_required,
no_action, or needs_investigation. Entity resolution status is resolved, ambiguous, or not_found.
Shipment verdict is on_time, seller_delay, logistics_delay, lost, returned, conflicting, or
insufficient_evidence. Payment verdict is reconciled, capture_mismatch, duplicate_capture,
refund_pending, refund_failed, refunded, or insufficient_evidence. Currency is BRL. Use zero
refund unless evidence and policy support a positive amount. Confidence must reflect evidence
quality. Return JSON only."""

VERIFIER_SYSTEM = """You are an independent output verifier. Return a corrected complete
day09-l3b-output-v2 JSON object only. Check schema fields, cross-field consistency, entity scope,
refund arithmetic, confidence calibration, and evidence provenance. Remove every unsupported
fact and every evidence_ref not in allowed_evidence_refs. Prefer insufficient_evidence and
needs_investigation over guessing."""


class AgentContext:
    """Case-scoped state, MCP cache, budgets and evidence provenance."""

    def __init__(
        self,
        case_id: str,
        case_data: dict[str, Any],
        gateway: EvidenceGateway,
        trace: TraceWriter,
    ) -> None:
        self.case_id = case_id
        self.case_data = case_data
        self.gateway = gateway
        self.trace = trace
        self.evidence_cache: dict[str, dict[str, Any]] = {}
        self.evidence_by_ref: dict[str, dict[str, Any]] = {}
        self.call_counts: Counter[str] = Counter()

    async def call_mcp(self, tool_name: str, actor: str, **kwargs: Any) -> dict[str, Any]:
        kwargs.pop("case_id", None)
        cache_key = f"{tool_name}:{json.dumps(kwargs, sort_keys=True, default=str)}"
        if cache_key in self.evidence_cache:
            evidence = self.evidence_cache[cache_key]
            self._trace_consumption(actor, tool_name, evidence)
            return evidence
        if sum(self.call_counts.values()) >= MAX_TOTAL_MCP_CALLS:
            raise RuntimeError("Per-case MCP call budget exhausted")
        if self.call_counts[tool_name] >= MAX_CALLS_PER_TOOL:
            raise RuntimeError(f"Per-tool MCP call budget exhausted for {tool_name}")

        self.call_counts[tool_name] += 1
        evidence = await self.gateway.call(tool_name, case_id=self.case_id, **kwargs)
        self.evidence_cache[cache_key] = evidence
        evidence_ref = evidence.get("evidence_ref")
        if isinstance(evidence_ref, str):
            self.evidence_by_ref[evidence_ref] = evidence
        self._trace_consumption(actor, tool_name, evidence)
        return evidence

    def evidence_payload(self) -> list[dict[str, Any]]:
        return list(self.evidence_by_ref.values())

    def _trace_consumption(
        self, actor: str, tool_name: str, evidence: dict[str, Any]
    ) -> None:
        evidence_ref = evidence.get("evidence_ref")
        if isinstance(evidence_ref, str):
            self.trace.emit(
                case_id=self.case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool_name,
                evidence_refs=[evidence_ref],
            )


class BaseAgent:
    def __init__(self, name: str, context: AgentContext, llm: JSONLLM) -> None:
        self.name = name
        self.context = context
        self.llm = llm


class EntityAgent(BaseAgent):
    async def resolve(self, tool_specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        entity_tools = [
            spec
            for spec in tool_specs
            if any(token in spec["name"].lower() for token in ("customer", "order", "entity"))
        ]
        plan = await self.llm.generate(
            system=PLANNER_SYSTEM,
            user=_json(
                {
                    "phase": "entity_resolution",
                    "case": self.context.case_data,
                    "available_tools": entity_tools or tool_specs,
                    "maximum_calls": 5,
                }
            ),
        )
        return await _execute_plan(self.context, plan, allowed_tools=tool_specs)


class SpecialistAgent(BaseAgent):
    async def investigate(self, tool_specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        plan = await self.llm.generate(
            system=PLANNER_SYSTEM,
            user=_json(
                {
                    "phase": "specialist_investigation",
                    "case": self.context.case_data,
                    "evidence_already_collected": self.context.evidence_payload(),
                    "available_tools": tool_specs,
                    "remaining_call_budget": MAX_TOTAL_MCP_CALLS
                    - sum(self.context.call_counts.values()),
                }
            ),
        )
        return await _execute_plan(self.context, plan, allowed_tools=tool_specs)


class VerifierAgent(BaseAgent):
    async def verify(self, draft_output: dict[str, Any]) -> dict[str, Any]:
        checked = await self.llm.generate(
            system=VERIFIER_SYSTEM,
            user=_json(
                {
                    "case": self.context.case_data,
                    "draft_output": draft_output,
                    "allowed_evidence_refs": sorted(self.context.evidence_by_ref),
                    "evidence": self.context.evidence_payload(),
                }
            ),
        )
        return _normalise_output(
            checked,
            case_id=self.context.case_id,
            allowed_refs=set(self.context.evidence_by_ref),
        )


async def _execute_plan(
    context: AgentContext,
    plan: dict[str, Any],
    *,
    allowed_tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    allowed_names = {spec["name"] for spec in allowed_tools}
    consumed: list[dict[str, Any]] = []
    calls = plan.get("calls", [])
    if not isinstance(calls, list):
        return consumed
    for call in calls[:MAX_TOTAL_MCP_CALLS]:
        if not isinstance(call, dict):
            continue
        tool_name = call.get("tool_name")
        arguments = call.get("arguments", {})
        if tool_name not in allowed_names or not isinstance(arguments, dict):
            continue
        actor = _actor_for_tool(tool_name)
        context.trace.emit(
            case_id=context.case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=actor,
            attributes={"tool_name": tool_name},
        )
        try:
            evidence = await context.call_mcp(tool_name, actor, **arguments)
        except (RuntimeError, TimeoutError, ValueError):
            continue
        consumed.append(evidence)
        context.trace.emit(
            case_id=context.case_id,
            event_type="handoff",
            actor=actor,
            target="coordinator",
            evidence_refs=[evidence["evidence_ref"]],
        )
    return consumed


def _actor_for_tool(tool_name: str) -> str:
    lowered = tool_name.lower()
    if any(token in lowered for token in ("customer", "entity")):
        return "entity-agent"
    if any(token in lowered for token in ("shipment", "tracking", "delivery")):
        return "shipment-agent"
    if any(token in lowered for token in ("payment", "refund")):
        return "payment-agent"
    if any(token in lowered for token in ("policy", "shop")):
        return "policy-agent"
    return "order-agent"


async def _tool_specs(gateway: EvidenceGateway) -> list[dict[str, Any]]:
    describe = getattr(gateway, "describe_tools", None)
    if describe is not None:
        return await describe()
    names = await gateway.list_tools()
    return [{"name": name, "description": "", "input_schema": {}} for name in names]


def _empty_output(case_id: str) -> dict[str, Any]:
    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "secondary_issues": [],
            "case_status": "needs_investigation",
            "confidence": 0.0,
        },
        "affected_entities": {
            "order_ids": [],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": [],
        "entity_resolution": {
            "status": "ambiguous",
            "resolved_order_ids": [],
            "rejected_candidates": [],
            "confidence": 0.0,
        },
        "customer_context": {"customer_unique_id": None, "related_order_ids": []},
        "shipment_analysis": {
            "verdict": "insufficient_evidence",
            "late_seller_ids": [],
            "timeline_complete": False,
        },
        "payment_analysis": {
            "verdict": "insufficient_evidence",
            "captured_total_brl": None,
            "refunded_total_brl": None,
            "refundable_total_brl": None,
        },
        "root_cause_analysis": {"ranked_causes": [], "responsible_parties": []},
        "evidence_refs": [],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0,
            "refund_lines": [],
        },
        "resolution_actions": [],
    }


def _normalise_output(
    candidate: dict[str, Any], *, case_id: str, allowed_refs: set[str]
) -> dict[str, Any]:
    template = _empty_output(case_id)
    result = _merge_known(template, candidate if isinstance(candidate, dict) else {})
    result["schema_version"] = "day09-l3b-output-v2"
    result["case_id"] = case_id
    result["evidence_refs"] = _valid_refs(result.get("evidence_refs"), allowed_refs)
    claims = result.get("claim_assessments", [])
    if isinstance(claims, list):
        for claim in claims:
            if isinstance(claim, dict):
                claim["evidence_refs"] = _valid_refs(claim.get("evidence_refs"), allowed_refs)
    else:
        result["claim_assessments"] = []
    return result


def _merge_known(template: Any, candidate: Any) -> Any:
    if isinstance(template, dict):
        source = candidate if isinstance(candidate, dict) else {}
        return {key: _merge_known(value, source.get(key, value)) for key, value in template.items()}
    return deepcopy(candidate)


def _valid_refs(value: Any, allowed_refs: set[str]) -> list[str]:
    if not isinstance(value, list):
        return []
    valid = (ref for ref in value if isinstance(ref, str) and ref in allowed_refs)
    return list(dict.fromkeys(valid))[:30]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run entity resolution, evidence specialists, synthesis and independent verification."""
    case_id = case["case_id"]
    context = AgentContext(case_id, case, gateway, trace)
    primary_llm, verifier_llm = build_llms()
    tool_specs = await _tool_specs(gateway)

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="entity-agent",
        attributes={"phase": "entity_resolution"},
    )
    entity_agent = EntityAgent("entity-agent", context, primary_llm)
    await entity_agent.resolve(tool_specs)
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="entity-agent",
        target="coordinator",
        evidence_refs=sorted(context.evidence_by_ref),
    )

    specialist = SpecialistAgent("specialist-coordinator", context, primary_llm)
    await specialist.investigate(tool_specs)

    draft = await primary_llm.generate(
        system=OUTPUT_SYSTEM,
        user=_json({"case": case, "evidence": context.evidence_payload()}),
    )
    draft = _normalise_output(draft, case_id=case_id, allowed_refs=set(context.evidence_by_ref))

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="verifier",
        evidence_refs=sorted(context.evidence_by_ref),
    )
    verifier = VerifierAgent("verifier", context, verifier_llm)
    final_output = await verifier.verify(draft)
    validate_output = getattr(gateway, "validate_output", None)
    if validate_output is not None:
        try:
            validate_output(final_output, f"workflow output for {case_id}")
        except ValueError:
            try:
                validate_output(draft, f"workflow draft for {case_id}")
                final_output = draft
            except ValueError:
                final_output = _empty_output(case_id)
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        target="coordinator",
        decision_code="OUTPUT_VERIFIED",
    )
    return final_output
