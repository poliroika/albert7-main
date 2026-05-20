"""Tests for the Reflexion verified-memory promotion gate."""
import pytest
from unittest.mock import MagicMock, patch
from umbrella.memory.palace.graph import EdgeType


def test_promotion_gate_requires_applied_edge():
    """Without applied_reflection edge, promotion is denied."""
    from umbrella.orchestrator.promotion import check_reflexion_promotion_gate
    import os

    palace = MagicMock()
    palace.walk.return_value = []  # no edges

    with patch.dict(os.environ, {"OUROBOROS_REFLEXION_PROMOTE_REQUIRES_VERIFY_PASS": "1"}):
        result = check_reflexion_promotion_gate(palace, reflection_id="refl-1", run_id="run-1")
    assert result is False
    palace.walk.assert_called_once_with(
        "refl-1",
        edge_types=[EdgeType.APPLIED_REFLECTION],
        hops=1,
        direction="in",
    )


def test_promotion_gate_passes_with_applied_edge():
    """With applied_reflection edge present, promotion is allowed."""
    from umbrella.orchestrator.promotion import check_reflexion_promotion_gate
    import os

    palace = MagicMock()
    mock_edge = MagicMock()
    palace.walk.return_value = [mock_edge]

    with patch.dict(os.environ, {"OUROBOROS_REFLEXION_PROMOTE_REQUIRES_VERIFY_PASS": "1"}):
        result = check_reflexion_promotion_gate(palace, reflection_id="refl-2", run_id="run-2")
    assert result is True


def test_promotion_gate_disabled_by_env():
    """When OUROBOROS_REFLEXION_PROMOTE_REQUIRES_VERIFY_PASS=0, gate is bypassed."""
    from umbrella.orchestrator.promotion import check_reflexion_promotion_gate
    import os

    palace = MagicMock()
    palace.walk.return_value = []  # no edges

    with patch.dict(os.environ, {"OUROBOROS_REFLEXION_PROMOTE_REQUIRES_VERIFY_PASS": "0"}):
        result = check_reflexion_promotion_gate(palace, reflection_id="refl-3", run_id="run-3")
    assert result is True


def test_submit_reflection_requires_evidence(tmp_path):
    """submit_reflection must fail when evidence_refs is empty."""
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.parent / "ouroboros"))
    from ouroboros.tools.phase_control import _submit_reflection
    from unittest.mock import MagicMock

    ctx = MagicMock()
    ctx.drive_root = tmp_path
    (tmp_path / "state").mkdir(exist_ok=True)

    result = _submit_reflection(
        ctx,
        text="The migration failed because of a type error.",
        applies_to_phase="execute",
        evidence_refs=[],
    )
    assert "ERROR" in result
    assert "evidence_refs" in result


def test_submit_reflection_with_citations(tmp_path):
    """submit_reflection succeeds with valid evidence_refs."""
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.parent / "ouroboros"))
    from ouroboros.tools.phase_control import _submit_reflection
    from unittest.mock import MagicMock

    ctx = MagicMock()
    ctx.drive_root = tmp_path
    (tmp_path / "state").mkdir(exist_ok=True)

    result = _submit_reflection(
        ctx,
        text="Migration failed [ev:tools_42] due to type mismatch [ev:events_38].",
        applies_to_phase="execute",
        evidence_refs=["tools_42", "events_38"],
    )
    assert "ERROR" not in result
    assert "execute" in result
