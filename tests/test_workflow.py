from __future__ import annotations

import asyncio

from student_agent.llm import _parse_json_object
from student_agent.workflow import AgentContext, _normalise_output


class FakeGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict:
        self.calls += 1
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": "ev_12345678901234567890",
            "result_hash": "sha256:" + ("a" * 64),
            "domain": "order",
            "data": {"tool": tool_name, "case_id": case_id, **arguments},
        }


class FakeTrace:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit(self, **event: object) -> None:
        self.events.append(event)


def test_parse_json_removes_qwen_thinking_and_fence() -> None:
    result = _parse_json_object('<think>private</think>\n```json\n{"ok":true}\n```')
    assert result == {"ok": True}


def test_case_cache_avoids_duplicate_mcp_call() -> None:
    async def exercise() -> tuple[dict, dict, FakeGateway, FakeTrace]:
        gateway = FakeGateway()
        trace = FakeTrace()
        context = AgentContext("CASE_001", {}, gateway, trace)  # type: ignore[arg-type]
        first = await context.call_mcp("get_order", "entity-agent", order_id="O1")
        second = await context.call_mcp("get_order", "order-agent", order_id="O1")
        return first, second, gateway, trace

    first, second, gateway, trace = asyncio.run(exercise())
    assert first == second
    assert gateway.calls == 1
    assert len(trace.events) == 2


def test_normalise_removes_foreign_evidence_refs() -> None:
    allowed = "ev_12345678901234567890"
    result = _normalise_output(
        {
            "evidence_refs": [allowed, "ev_99999999999999999999"],
            "claim_assessments": [
                {
                    "claim_id": "claim-1",
                    "verdict": "supported",
                    "confidence": 0.8,
                    "evidence_refs": [allowed, "ev_99999999999999999999"],
                }
            ],
        },
        case_id="CASE_001",
        allowed_refs={allowed},
    )
    assert result["evidence_refs"] == [allowed]
    assert result["claim_assessments"][0]["evidence_refs"] == [allowed]
