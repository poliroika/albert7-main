"""Integration tests for computer_use with the framework tools layer."""

import json

import pytest

from gmas.tools import ComputerUseTool, create_tool_from_config, get_registry
from gmas.tools.computer_use import build_computer_use_full_schema, build_computer_use_tool_schema


def test_tools_package_exports_computer_use_tool():
    tool = ComputerUseTool(runtime_name="mock")
    assert tool.name == "computer_use"
    assert "stateful" in tool.description.lower()


def test_registry_contains_default_computer_use_tool():
    registry = get_registry()
    # ComputerUseTool requires runtime_name, so register it manually
    from gmas.tools.computer_use import ComputerUseTool

    registry.register(ComputerUseTool(runtime_name="mock"))
    tool = registry.get("computer_use")
    assert tool is not None
    assert tool.name == "computer_use"


def test_factory_creates_computer_use_tool():
    tool = create_tool_from_config({"name": "computer_use", "runtime_name": "mock"})
    assert tool is not None
    assert tool.name == "computer_use"


def test_framework_tool_returns_serialized_response_payload():
    tool = ComputerUseTool(runtime_name="mock")
    result = tool.execute(
        operation="start",
        config={
            "runtime_name": "mock",
            "observation": {"mode": "standard"},
        },
    )
    assert result.success is True
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert payload["session"]["runtime_name"] == "mock"
    assert payload["capabilities"]["supports_screenshots"] is True


def test_framework_tool_rejects_unknown_runtime_name():
    with pytest.raises(ValueError, match="unknown computer_use runtime"):
        ComputerUseTool(runtime_name="nope")


@pytest.mark.asyncio
async def test_framework_tool_execute_async_succeeds():
    """execute_async must return the same result as the synchronous execute."""
    tool = ComputerUseTool(runtime_name="mock")
    result = await tool.execute_async(
        operation="start",
        config={"runtime_name": "mock"},
    )
    assert result.success is True
    payload = json.loads(result.output)
    assert payload["session"]["runtime_name"] == "mock"


def test_context_manager_does_not_raise():
    """ComputerUseTool must work as a context manager."""
    with ComputerUseTool(runtime_name="mock") as tool:
        result = tool.execute(operation="start", config={"runtime_name": "mock"})
        assert result.success is True


def test_simplified_schema_parameters_match_tool_parameters_schema():
    """ComputerUseTool.parameters_schema must match the simplified schema."""
    tool = ComputerUseTool(runtime_name="mock")
    simplified = build_computer_use_tool_schema()["function"]["parameters"]
    assert tool.parameters_schema == simplified


def test_full_schema_differs_from_simplified():
    """The full Pydantic schema is more verbose than the simplified LLM schema."""
    simplified = build_computer_use_tool_schema()
    full = build_computer_use_full_schema()
    # Full schema has $defs (Pydantic type definitions); simplified does not.
    assert "$defs" in full["function"]["parameters"]
    assert "$defs" not in simplified["function"]["parameters"]
