from __future__ import annotations

import json
from pathlib import Path
import pytest

from student_agent.contracts import Contracts
from student_agent.workflow import (
    AgentMessage,
    COORDINATOR_AGENT_SPEC,
    SPECIALIST_AUDITOR_AGENT_SPEC,
    SpecialistAuditorAgent,
)


def test_agent_model_spec_under_10b() -> None:
    # Mandatory requirement: both agents must be under 10 billion parameters
    assert float(COORDINATOR_AGENT_SPEC.parameter_count.replace("B", "")) < 10.0
    assert float(SPECIALIST_AUDITOR_AGENT_SPEC.parameter_count.replace("B", "")) < 10.0
    assert COORDINATOR_AGENT_SPEC.parameter_limit == "< 10B"
    assert SPECIALIST_AUDITOR_AGENT_SPEC.parameter_limit == "< 10B"


def test_agent_message_envelope() -> None:
    msg = AgentMessage(
        case_id="L3B_CASE_001",
        sender="coordinator",
        recipient="specialist_auditor",
        action="delegate_investigation",
        payload={"candidates": ["c1", "c2"]},
    )
    assert msg.case_id == "L3B_CASE_001"
    assert msg.sender == "coordinator"
    assert msg.recipient == "specialist_auditor"
    assert msg.action == "delegate_investigation"


def test_verifier_catches_invalid_case_id(tmp_path: Path) -> None:
    class DummyTrace:
        def emit(self, **kwargs):
            pass

    agent = SpecialistAuditorAgent(None, DummyTrace())
    output = {
        "case_id": "WRONG_ID",
        "schema_version": "day09-l3b-output-v2",
    }
    case = {"case_id": "L3B_CASE_001"}
    with pytest.raises(AssertionError, match="case_id mismatch"):
        agent.verify_invariants(output, case)
