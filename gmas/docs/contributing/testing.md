# Testing

## Running Tests

```bash
# All tests
uv run pytest tests/ -v

# Specific file
uv run pytest tests/test_graph.py -v

# By keyword
uv run pytest tests/ -k "scheduler" -v

# With coverage
uv run pytest tests/ -v --cov=src --cov-report=term --cov-report=html
```

## Test Structure

```python
import pytest
from gmas.core import AgentProfile, RoleGraph

def test_create_agent():
    """Test agent creation."""
    agent = AgentProfile(
        agent_id="test",
        display_name="Test Agent",
    )
    assert agent.agent_id == "test"
    assert agent.display_name == "Test Agent"

def test_graph_creation():
    """Test graph creation with agents."""
    agents = [
        AgentProfile(agent_id="a1", display_name="Agent 1"),
        AgentProfile(agent_id="a2", display_name="Agent 2"),
    ]
    graph = build_property_graph(agents)
    assert graph.num_nodes == 2
```

## Async Tests

```python
import pytest
from gmas.execution import MACPRunner

@pytest.mark.asyncio
async def test_async_execution():
    """Test async execution."""
    runner = MACPRunner(async_llm_caller=async_llm)
    result = await runner.arun_round(graph)
    assert result.final_answer is not None
```

## Fixtures

```python
@pytest.fixture
def sample_agents():
    """Return sample agents for testing."""
    return [
        AgentProfile(agent_id="a1", display_name="Agent 1"),
        AgentProfile(agent_id="a2", display_name="Agent 2"),
    ]

@pytest.fixture
def sample_graph(sample_agents):
    """Return a sample graph."""
    return build_property_graph(sample_agents)
```

## Coverage

Aim for >80% coverage on new code.

```bash
uv run pytest tests/ --cov=src --cov-report=html
open htmlcov/index.html
```
