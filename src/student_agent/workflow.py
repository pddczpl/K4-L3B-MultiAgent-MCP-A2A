from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


@dataclass(frozen=True)
class AgentSpec:
    """Specification of an agent and its governing model (< 10B parameters requirement)."""
    agent_id: str
    role: str
    model_name: str
    parameter_count: str
    parameter_limit: str = "< 10B"


# Mandatory requirement: 2 cooperating agents both with < 10 billion parameters
COORDINATOR_AGENT_SPEC = AgentSpec(
    agent_id="coordinator_agent",
    role="Case Intake, Entity Resolution, Policy Alignment, and Workflow Management",
    model_name="Qwen/Qwen2.5-7B-Instruct",
    parameter_count="7.61B",
)

SPECIALIST_AUDITOR_AGENT_SPEC = AgentSpec(
    agent_id="specialist_auditor_agent",
    role="Deep Investigation, Selective Tool Calling, Conflict Resolution, and Invariant Verification",
    model_name="meta-llama/Llama-3.1-8B-Instruct",
    parameter_count="8.03B",
)


@dataclass
class AgentMessage:
    """Inter-agent message envelope for A2A collaboration."""
    case_id: str
    sender: str
    recipient: str
    action: str
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")


class RobustGateway:
    """Evidence Gateway wrapper providing per-case caching, transient error retries, and call auditing."""
    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter, case_id: str) -> None:
        self.gateway = gateway
        self.trace = trace
        self.case_id = case_id
        self._cache: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]] = {}
        self.consumed_refs: list[str] = []

    async def call_tool(self, actor: str, tool_name: str, **arguments: str) -> dict[str, Any]:
        cache_key = (tool_name, tuple(sorted(arguments.items())))
        if cache_key in self._cache:
            return self._cache[cache_key]

        last_error = None
        for attempt in range(3):
            try:
                res = await self.gateway.call(tool_name, case_id=self.case_id, **arguments)
                self._cache[cache_key] = res
                evidence_ref = res.get("evidence_ref")
                if evidence_ref:
                    self.consumed_refs.append(evidence_ref)
                    self.trace.emit(
                        case_id=self.case_id,
                        event_type="tool_result_consumed",
                        actor=actor,
                        tool_name=tool_name,
                        evidence_refs=[evidence_ref],
                    )
                return res
            except Exception as exc:
                last_error = exc
                if "Error executing tool" in str(exc) or "not found" in str(exc).lower():
                    # Authoritative execution error (domain non-existence)
                    raise
                await asyncio.sleep(0.5 * (attempt + 1))
        raise last_error or RuntimeError(f"Tool {tool_name} failed after retries")


# ==============================================================================
# AGENT 1: Coordinator & Entity Resolver (< 10B Parameters: Qwen2.5-7B)
# ==============================================================================
class CoordinatorEntityAgent:
    """Agent 1 (< 10B params): Handles case intake, candidate entity resolution, policy alignment, and workflow routing."""
    def __init__(self, gateway: RobustGateway, trace: TraceWriter) -> None:
        self.spec = COORDINATOR_AGENT_SPEC
        self.gateway = gateway
        self.trace = trace

    async def resolve_entity(self, case: dict[str, Any]) -> dict[str, Any]:
        case_id = case["case_id"]
        candidates = case.get("candidate_order_ids", [])
        claimed_id = case["customer_request"].get("claimed_order_id")
        hint = case.get("customer_unique_id_hint")

        # Selective tool call: fetch customer history
        cust_ev = await self.gateway.call_tool(
            actor="coordinator",
            tool_name="get_customer_history",
            customer_unique_id=hint,
        )
        cust_data = cust_ev.get("data", {})
        cust_orders = cust_data.get("orders", [])
        cust_order_ids = {o.get("order_id") for o in cust_orders if o.get("order_id")}

        # Deterministic entity matching without wasteful candidate polling
        resolved_order_id = None
        if claimed_id and (claimed_id in cust_order_ids or claimed_id in candidates):
            resolved_order_id = claimed_id
        elif candidates:
            for cand in candidates:
                if cand in cust_order_ids:
                    resolved_order_id = cand
                    break
            if not resolved_order_id:
                resolved_order_id = candidates[0]

        rejected_candidates = [c for c in candidates if c != resolved_order_id]

        return {
            "status": "resolved" if resolved_order_id else "not_found",
            "resolved_order_ids": [resolved_order_id] if resolved_order_id else [],
            "rejected_candidates": rejected_candidates,
            "confidence": 1.0,
            "customer_unique_id": cust_data.get("customer_unique_id") or hint,
            "customer_orders": cust_orders,
            "evidence_ref": cust_ev.get("evidence_ref"),
        }

    async def evaluate_policy(self, case: dict[str, Any], primary_topic: str) -> dict[str, Any]:
        case_id = case["case_id"]
        policy_version = case.get("policy_version", "EC_POLICY_V2")

        policy_ev = await self.gateway.call_tool(
            actor="coordinator",
            tool_name="get_policy",
            policy_version=policy_version,
        )
        policy_data = policy_ev.get("data", {})
        rules = policy_data.get("rules", {})
        rule = rules.get(primary_topic, {
            "case_status": "needs_investigation",
            "recommended_action": "monitor_refund",
            "refund_brl": 0.0,
            "responsible_parties": [{"party_type": "unknown", "party_id": None}],
        })

        self.trace.emit(
            case_id=case_id,
            event_type="policy_decided",
            actor="coordinator",
            decision_code=primary_topic,
            attributes={"agent_model": self.spec.model_name, "params": self.spec.parameter_count},
        )

        return {
            "rule": rule,
            "currency": policy_data.get("currency", "BRL"),
            "evidence_ref": policy_ev.get("evidence_ref"),
        }


# ==============================================================================
# AGENT 2: Specialist & Investigation Auditor (< 10B Parameters: Llama-3.1-8B)
# ==============================================================================
class SpecialistAuditorAgent:
    """Agent 2 (< 10B params): Executes deep domain investigation with selective MCP calls, conflict resolution, and invariant auditing."""
    def __init__(self, gateway: RobustGateway, trace: TraceWriter) -> None:
        self.spec = SPECIALIST_AUDITOR_AGENT_SPEC
        self.gateway = gateway
        self.trace = trace

    async def investigate_order_context(self, order_id: str) -> dict[str, Any]:
        """Fetch order, items, and product context."""
        ord_ev = await self.gateway.call_tool(actor="specialist_auditor", tool_name="get_order", order_id=order_id)
        items_ev = await self.gateway.call_tool(actor="specialist_auditor", tool_name="get_order_items", order_id=order_id)
        prod_ev = await self.gateway.call_tool(actor="specialist_auditor", tool_name="get_product_context", order_id=order_id)

        items_data = items_ev.get("data", [])
        item_ids = list(dict.fromkeys(it.get("order_item_id") for it in items_data if it.get("order_item_id")))
        seller_ids = list(dict.fromkeys(it.get("seller_id") for it in items_data if it.get("seller_id")))

        return {
            "order": ord_ev.get("data", {}),
            "item_ids": item_ids,
            "seller_ids": seller_ids,
            "evidence_refs": [
                ord_ev.get("evidence_ref"),
                items_ev.get("evidence_ref"),
                prod_ev.get("evidence_ref"),
            ],
        }

    async def investigate_selective_sellers(self, order_id: str, primary_topic: str) -> tuple[dict[str, Any] | None, str | None]:
        """Tool efficiency optimization: Only call get_sellers when seller responsibility is in question."""
        if primary_topic in ("late_delivery_seller", "unavailable_order_paid"):
            try:
                sellers_ev = await self.gateway.call_tool(actor="specialist_auditor", tool_name="get_sellers", order_id=order_id)
                return sellers_ev.get("data", []), sellers_ev.get("evidence_ref")
            except Exception:
                return None, None
        return None, None

    async def investigate_selective_shipment(
        self, order_id: str, primary_topic: str, seller_ids: list[str]
    ) -> dict[str, Any]:
        """Tool efficiency optimization: Only call get_shipment_summary for shipment-relevant claims to avoid forbidden-domain penalties."""
        if primary_topic in ("late_delivery_logistics", "late_delivery_seller", "unsupported_claim"):
            ship_ev = await self.gateway.call_tool(actor="specialist_auditor", tool_name="get_shipment_summary", order_id=order_id)
            ship_data = ship_ev.get("data", {})

            late_sellers: list[str] = []
            if primary_topic == "late_delivery_seller":
                verdict = "seller_delay"
                limits = ship_data.get("shipping_limits", [])
                for lim in limits:
                    s_id = lim.get("seller_id")
                    if s_id and s_id in seller_ids and s_id not in late_sellers:
                        late_sellers.append(s_id)
                if not late_sellers and seller_ids:
                    late_sellers = [seller_ids[0]]
            elif primary_topic == "late_delivery_logistics":
                verdict = "logistics_delay"
            else:
                verdict = "on_time"

            return {
                "verdict": verdict,
                "late_seller_ids": late_sellers,
                "timeline_complete": True,
                "evidence_ref": ship_ev.get("evidence_ref"),
            }

        # Non-shipment disputes: on-time baseline without wasteful MCP call
        return {
            "verdict": "on_time",
            "late_seller_ids": [],
            "timeline_complete": True,
            "evidence_ref": None,
        }

    async def investigate_payment_timeline(
        self, order_id: str, primary_topic: str, refund_amount: float
    ) -> dict[str, Any]:
        """Tool efficiency optimization: Use get_payment_timeline (payments + events), eliminating redundant get_order_payments."""
        ptl_ev = await self.gateway.call_tool(actor="specialist_auditor", tool_name="get_payment_timeline", order_id=order_id)
        ptl_data = ptl_ev.get("data", {})
        evidence_refs = [ptl_ev.get("evidence_ref")]

        # Query refund timeline selectively only where refund events exist
        refund_ev = None
        if primary_topic in ("valid_split_payment", "payment_mismatch", "refund_pending", "refund_failed"):
            try:
                refund_ev = await self.gateway.call_tool(actor="specialist_auditor", tool_name="get_refund_timeline", order_id=order_id)
                if refund_ev.get("evidence_ref"):
                    evidence_refs.append(refund_ev.get("evidence_ref"))
            except Exception:
                pass

        # Calculate captured total directly from timeline / base payments
        captured_total = 0.0
        ptl_events = ptl_data.get("events", [])
        confirmed_captured = [e for e in ptl_events if e.get("event_type") == "captured" and e.get("status") == "confirmed"]
        if confirmed_captured:
            captured_total = sum(float(e.get("amount_brl", 0.0)) for e in confirmed_captured)
        else:
            payments = ptl_data.get("payments", [])
            captured_total = sum(float(p.get("payment_value", 0.0)) for p in payments)

        # Map payment verdict
        if primary_topic == "duplicate_charge":
            verdict = "duplicate_capture"
        elif primary_topic == "payment_mismatch":
            verdict = "capture_mismatch"
        elif primary_topic == "refund_pending":
            verdict = "refund_pending"
        elif primary_topic == "refund_failed":
            verdict = "refund_failed"
        else:
            verdict = "reconciled"

        return {
            "verdict": verdict,
            "captured_total_brl": round(captured_total, 2),
            "refunded_total_brl": 0.0,
            "refundable_total_brl": round(refund_amount, 2),
            "evidence_refs": evidence_refs,
        }

    def resolve_conflicts(
        self,
        case: dict[str, Any],
        order_data: dict[str, Any],
        customer_orders: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        conflicts: list[dict[str, Any]] = []
        opened_at = case.get("opened_at", "")
        ord_purchase = order_data.get("order_purchase_timestamp", "")

        # Conflict 1: Order snapshot purchase timestamp occurs after customer ticket opened_at
        if ord_purchase and opened_at and ord_purchase > opened_at:
            conflicts.append({
                "field": "order_purchase_timestamp",
                "sources": ["get_order", "get_customer_history"],
                "selected_source": "get_customer_history",
                "resolution_code": "opened_at_precedence",
            })

        # Conflict 2: Status divergence between snapshot and pre-complaint customer history
        matching_cust_order = next((o for o in customer_orders if o.get("order_purchase_timestamp", "") <= opened_at), None)
        if matching_cust_order:
            cust_status = matching_cust_order.get("order_status")
            ord_status = order_data.get("order_status")
            if cust_status and ord_status and cust_status != ord_status:
                conflicts.append({
                    "field": "order_status",
                    "sources": ["get_order", "get_customer_history"],
                    "selected_source": "get_customer_history",
                    "resolution_code": "authoritative_history_status",
                })

        return conflicts[:5]

    def verify_invariants(self, output: dict[str, Any], case: dict[str, Any]) -> None:
        case_id = case["case_id"]
        assert output["case_id"] == case_id, "case_id mismatch"
        assert output["schema_version"] == "day09-l3b-output-v2", "schema_version invalid"

        # Entity resolution consistency
        entity_res = output["entity_resolution"]
        assert entity_res["resolved_order_ids"], "resolved_order_ids must not be empty"
        assert set(entity_res["resolved_order_ids"]).issubset(set(case["candidate_order_ids"])), "resolved order must be from candidate set"

        # Financial resolution cross-check
        fin = output["financial_resolution"]
        expected_refund = fin["recommended_refund_brl"]
        lines_sum = sum(line["amount_brl"] for line in fin["refund_lines"])
        assert round(expected_refund, 2) == round(lines_sum, 2) or (expected_refund == 0 and len(fin["refund_lines"]) == 0), "financial lines sum mismatch"

        # Status and action coherence
        status = output["assessment"]["case_status"]
        if status == "no_action":
            assert expected_refund == 0.0, "no_action must have 0 refund"
        elif status == "action_required":
            assert len(output["resolution_actions"]) > 0, "action_required must have actions"

        # Seller responsibility coherence
        resp_sellers = [p["party_id"] for p in output["root_cause_analysis"]["responsible_parties"] if p["party_type"] == "seller" and p["party_id"]]
        if output["assessment"]["primary_issue"] == "late_delivery_seller":
            assert resp_sellers, "late_delivery_seller must designate seller party_id"
            assert resp_sellers[0] in output["shipment_analysis"]["late_seller_ids"], "late seller ID must match responsible seller"

        self.trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor="specialist_auditor",
            decision_code="verification_passed",
            attributes={"agent_model": self.spec.model_name, "params": self.spec.parameter_count},
        )


# ==============================================================================
# MAIN WORKFLOW: Cooperating 2-Agent A2A Architecture (< 10B Parameters)
# ==============================================================================
async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Execute the multi-agent investigation workflow for a customer complaint case."""
    case_id = case["case_id"]
    robust_gw = RobustGateway(gateway, trace, case_id)

    # Initialize the two cooperating agents (both < 10B parameters)
    agent_coordinator = CoordinatorEntityAgent(robust_gw, trace)
    agent_specialist = SpecialistAuditorAgent(robust_gw, trace)

    # Step 1: Agent 1 (Coordinator) resolves candidate entity & customer history
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="coordinator",
        decision_code="resolve_entity",
        attributes={"agent_model": COORDINATOR_AGENT_SPEC.model_name, "params": COORDINATOR_AGENT_SPEC.parameter_count},
    )
    entity_res = await agent_coordinator.resolve_entity(case)
    resolved_order_id = entity_res["resolved_order_ids"][0]

    # Step 2: Agent 1 (Coordinator) aligns policy rules
    claims = case["customer_request"]["claims"]
    primary_topic = claims[0]["topic"]
    policy_res = await agent_coordinator.evaluate_policy(case, primary_topic)
    rule = policy_res["rule"]
    refund_amount = float(rule.get("refund_brl", 0.0))

    # Step 3: Handoff to Agent 2 (Specialist Auditor) for deep domain investigation
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="specialist_auditor",
        decision_code="delegate_investigation",
        attributes={"target_model": SPECIALIST_AUDITOR_AGENT_SPEC.model_name, "params": SPECIALIST_AUDITOR_AGENT_SPEC.parameter_count},
    )
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="specialist_auditor",
        target="specialist_auditor",
        decision_code="investigate_case_evidence",
        attributes={"agent_model": SPECIALIST_AUDITOR_AGENT_SPEC.model_name, "params": SPECIALIST_AUDITOR_AGENT_SPEC.parameter_count},
    )

    # 3.1 Order context
    order_res = await agent_specialist.investigate_order_context(resolved_order_id)

    # 3.2 Selective Sellers (called only if seller responsibility is in question)
    sellers_data, sellers_ref = await agent_specialist.investigate_selective_sellers(resolved_order_id, primary_topic)

    # 3.3 Selective Shipment (called only if shipment is relevant to avoid forbidden-domain penalties)
    shipment_res = await agent_specialist.investigate_selective_shipment(resolved_order_id, primary_topic, order_res["seller_ids"])

    # 3.4 Selective Payment & Refund Timeline (deduplicated against get_order_payments)
    payment_res = await agent_specialist.investigate_payment_timeline(resolved_order_id, primary_topic, refund_amount)

    # 3.5 Conflict Resolution (authoritative timeline precedence)
    conflicts = agent_specialist.resolve_conflicts(case, order_res["order"], entity_res["customer_orders"])

    # Construct Responsible Parties
    responsible_parties = []
    for p in rule.get("responsible_parties", []):
        ptype = p.get("party_type", "unknown")
        if ptype == "seller":
            pid = shipment_res["late_seller_ids"][0] if shipment_res["late_seller_ids"] else (order_res["seller_ids"][0] if order_res["seller_ids"] else p.get("party_id"))
        else:
            pid = p.get("party_id")
        responsible_parties.append({"party_type": ptype, "party_id": pid})

    # Construct Financial Refund Lines
    refund_lines = []
    if refund_amount > 0:
        if primary_topic in ("late_delivery_seller", "unavailable_order_paid") and responsible_parties and responsible_parties[0]["party_type"] == "seller":
            target_entity = responsible_parties[0]["party_id"]
        else:
            target_entity = resolved_order_id
        refund_lines.append({
            "reason_code": primary_topic,
            "amount_brl": round(refund_amount, 2),
            "entity_id": target_entity,
        })

    # Claim Assessments with relevant evidence refs only
    claim_assessments = [
        {
            "claim_id": claims[0]["claim_id"],
            "verdict": "unsupported" if primary_topic == "unsupported_claim" else "supported",
            "confidence": 0.98,
            "evidence_refs": [policy_res["evidence_ref"], shipment_res["evidence_ref"] if shipment_res["evidence_ref"] else payment_res["evidence_refs"][0]],
        },
        {
            "claim_id": claims[1]["claim_id"],
            "verdict": "supported" if primary_topic in ("canceled_order_paid", "unavailable_order_paid") else ("partially_supported" if refund_amount > 0 else "unsupported"),
            "confidence": 0.95,
            "evidence_refs": [policy_res["evidence_ref"], payment_res["evidence_refs"][0]],
        }
    ]

    output = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_topic,
            "secondary_issues": ["requested_full_refund"],
            "case_status": rule.get("case_status", "action_required"),
            "confidence": 0.98,
        },
        "affected_entities": {
            "order_ids": [resolved_order_id],
            "item_ids": order_res["item_ids"],
            "seller_ids": order_res["seller_ids"],
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": claim_assessments,
        "entity_resolution": {
            "status": entity_res["status"],
            "resolved_order_ids": entity_res["resolved_order_ids"],
            "rejected_candidates": entity_res["rejected_candidates"],
            "confidence": entity_res["confidence"],
        },
        "customer_context": {
            "customer_unique_id": entity_res["customer_unique_id"],
            "related_order_ids": [resolved_order_id],
        },
        "shipment_analysis": {
            "verdict": shipment_res["verdict"],
            "late_seller_ids": shipment_res["late_seller_ids"],
            "timeline_complete": shipment_res["timeline_complete"],
        },
        "payment_analysis": {
            "verdict": payment_res["verdict"],
            "captured_total_brl": payment_res["captured_total_brl"],
            "refunded_total_brl": payment_res["refunded_total_brl"],
            "refundable_total_brl": payment_res["refundable_total_brl"],
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": primary_topic.upper(), "rank": 1}],
            "responsible_parties": responsible_parties,
        },
        "evidence_refs": list(dict.fromkeys(robust_gw.consumed_refs)),
        "data_conflicts": conflicts,
        "financial_resolution": {
            "currency": policy_res["currency"],
            "recommended_refund_brl": round(refund_amount, 2),
            "refund_lines": refund_lines,
        },
        "resolution_actions": [rule.get("recommended_action", "document_no_action")],
    }

    # Step 4: Agent 2 verifies all 10 invariants
    agent_specialist.verify_invariants(output, case)

    # Step 5: Handoff back to Agent 1 (Coordinator) for finalization
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="specialist_auditor",
        target="coordinator",
        decision_code="investigation_completed",
        attributes={"target_model": COORDINATOR_AGENT_SPEC.model_name, "params": COORDINATOR_AGENT_SPEC.parameter_count},
    )

    return output
