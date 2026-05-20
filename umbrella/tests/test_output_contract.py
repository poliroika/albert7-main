import json
import pytest
from umbrella.utils.result_envelope import ResultEnvelope, ErrorCode

def test_success_envelope_structure():
    env = ResultEnvelope.success(data={"key": "value"}, run_id="r1", phase="research")
    d = env.to_dict()
    assert d["ok"] is True
    assert d["data"] == {"key": "value"}
    assert d["errors"] == []
    assert d["meta"]["run_id"] == "r1"

def test_failure_envelope_structure():
    env = ResultEnvelope.failure(ErrorCode.TOOL_DENIED_BY_ENVELOPE, "shell denied")
    d = env.to_dict()
    assert d["ok"] is False
    assert d["errors"][0]["code"] == ErrorCode.TOOL_DENIED_BY_ENVELOPE
    assert d["errors"][0]["message"] == "shell denied"

def test_json_serializable():
    env = ResultEnvelope.success(data={"x": 1})
    raw = env.to_json()
    parsed = json.loads(raw)
    assert parsed["ok"] is True

def test_pretty_json():
    env = ResultEnvelope.success(data={"x": 1})
    raw = env.to_json(pretty=True)
    assert "\n" in raw
    parsed = json.loads(raw)
    assert parsed["ok"] is True

def test_required_fields_present():
    env = ResultEnvelope.success()
    d = env.to_dict()
    for field in ("ok", "data", "errors", "meta"):
        assert field in d
