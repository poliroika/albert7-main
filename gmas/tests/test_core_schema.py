"""Tests for src/core/schema.py"""

from typing import cast

import pytest
import torch
from pydantic import BaseModel

from gmas.core.schema import (
    SCHEMA_VERSION,
    AgentNodeSchema,
    BaseEdgeSchema,
    BaseNodeSchema,
    CostMetrics,
    EdgeType,
    GraphSchema,
    LLMConfig,
    MigrationRegistry,
    MigrationV1ToV2,
    NodeType,
    SchemaValidationResult,
    SchemaValidator,
    SchemaVersion,
    TaskNodeSchema,
    ValidationResult,
    WorkflowEdgeSchema,
    migrate_schema,
)

# ─────────────────────────── SchemaVersion ───────────────────────────────────


class TestSchemaVersion:
    def test_current(self):
        v = SchemaVersion.current()
        assert str(v) == SCHEMA_VERSION

    def test_parse(self):
        v = SchemaVersion.parse("2.0.0")
        assert v.major == 2
        assert v.minor == 0
        assert v.patch == 0

    def test_compatibility_same_major(self):
        v1 = SchemaVersion.parse("2.0.0")
        v2 = SchemaVersion.parse("2.5.1")
        assert v1.is_schema_compatible(v2)

    def test_incompatibility_different_major(self):
        v1 = SchemaVersion.parse("1.0.0")
        v2 = SchemaVersion.parse("2.0.0")
        assert not v1.is_schema_compatible(v2)


# ─────────────────────────── LLMConfig ───────────────────────────────────────


class TestLLMConfig:
    def test_default_init(self):
        cfg = LLMConfig()
        assert cfg.model_name is None
        assert cfg.temperature is None

    def test_configured(self):
        cfg = LLMConfig(model_name="gpt-4", base_url="https://api.openai.com/v1")
        assert cfg.is_configured()

    def test_not_configured(self):
        cfg = LLMConfig()
        assert not cfg.is_configured()

    def test_resolve_api_key_literal(self):
        cfg = LLMConfig(api_key="sk-test-key")
        assert cfg.resolve_api_key() == "sk-test-key"

    def test_resolve_api_key_env_var(self, monkeypatch):
        monkeypatch.setenv("MY_API_KEY", "env-secret")
        cfg = LLMConfig(api_key="$MY_API_KEY")
        assert cfg.resolve_api_key() == "env-secret"

    def test_resolve_api_key_none(self):
        cfg = LLMConfig()
        assert cfg.resolve_api_key() is None

    def test_merge_with_none(self):
        cfg = LLMConfig(model_name="gpt-4", temperature=0.7)
        merged = cfg.merge_with(None)
        assert merged.model_name == "gpt-4"
        assert merged.temperature == 0.7

    def test_merge_with_other_fills_gaps(self):
        cfg = LLMConfig(model_name="gpt-4")
        other = LLMConfig(temperature=0.5, max_tokens=1000)
        merged = cfg.merge_with(other)
        assert merged.model_name == "gpt-4"
        assert merged.temperature == 0.5
        assert merged.max_tokens == 1000

    def test_merge_self_takes_priority(self):
        cfg = LLMConfig(model_name="gpt-4", temperature=0.9)
        other = LLMConfig(model_name="gpt-3.5", temperature=0.1)
        merged = cfg.merge_with(other)
        assert merged.model_name == "gpt-4"
        assert merged.temperature == 0.9

    def test_to_generation_params(self):
        cfg = LLMConfig(temperature=0.7, max_tokens=512, top_p=0.9)
        params = cfg.to_generation_params()
        assert params["temperature"] == 0.7
        assert params["max_tokens"] == 512
        assert params["top_p"] == 0.9

    def test_to_generation_params_empty(self):
        cfg = LLMConfig()
        params = cfg.to_generation_params()
        assert params == {}

    def test_to_generation_params_with_stop(self):
        cfg = LLMConfig(stop_sequences=["END", "STOP"])
        params = cfg.to_generation_params()
        assert params["stop"] == ["END", "STOP"]

    def test_extra_params_merged(self):
        cfg = LLMConfig(extra_params={"stream": True})
        params = cfg.to_generation_params()
        assert params["stream"] is True


# ─────────────────────────── BaseNodeSchema ──────────────────────────────────


class TestBaseNodeSchema:
    def test_basic_creation(self):
        node = BaseNodeSchema(id="node1")
        assert node.id == "node1"
        assert node.type == NodeType.AGENT

    def test_with_embedding(self):
        node = BaseNodeSchema(id="n1", embedding=[0.1, 0.2, 0.3])
        assert node.embedding is not None
        assert len(node.embedding) == 3
        assert node.embedding_dim == 3

    def test_embedding_from_tensor(self):
        t = torch.tensor([1.0, 2.0, 3.0])
        node = BaseNodeSchema(id="n1", embedding=cast("list[float]", t))
        assert node.embedding == [1.0, 2.0, 3.0]

    def test_to_tensor_embedding_none(self):
        node = BaseNodeSchema(id="n1")
        assert node.to_tensor_embedding() is None

    def test_to_tensor_embedding(self):
        node = BaseNodeSchema(id="n1", embedding=[1.0, 2.0])
        t = node.to_tensor_embedding()
        assert isinstance(t, torch.Tensor)
        assert t.shape == (2,)

    def test_get_feature_vector_with_embedding(self):
        node = BaseNodeSchema(id="n1", embedding=[1.0, 2.0, 3.0])
        fv = node.get_feature_vector()
        assert fv.shape == (3,)

    def test_get_feature_vector_with_metadata(self):
        node = BaseNodeSchema(id="n1", embedding=[1.0], metadata={"score": 0.5})
        fv = node.get_feature_vector(feature_names=["score"])
        assert fv.shape == (2,)

    def test_get_feature_vector_empty(self):
        node = BaseNodeSchema(id="n1")
        fv = node.get_feature_vector()
        assert fv.shape == (0,)

    def test_tags(self):
        node = BaseNodeSchema(id="n1", tags={"tag1", "tag2"})
        assert "tag1" in node.tags


# ─────────────────────────── AgentNodeSchema ─────────────────────────────────


class TestAgentNodeSchema:
    def test_basic_creation(self):
        agent = AgentNodeSchema(id="solver", persona="You are a solver")
        assert agent.id == "solver"
        assert agent.type == NodeType.AGENT
        assert agent.trust_score == 1.0

    def test_validate_input_no_schema(self):
        agent = AgentNodeSchema(id="solver")
        result = agent.validate_input({"question": "2+2"})
        assert result.valid is True
        assert "No schema" in result.message

    def test_validate_input_with_pydantic_schema(self):
        class InputSchema(BaseModel):
            question: str

        agent = AgentNodeSchema(id="solver", input_schema=InputSchema)
        result = agent.validate_input({"question": "what is 2+2?"})
        assert result.valid is True
        assert result.validated_data == {"question": "what is 2+2?"}

    def test_validate_input_pydantic_failure(self):
        class InputSchema(BaseModel):
            question: str
            required_field: int

        agent = AgentNodeSchema(id="solver", input_schema=InputSchema)
        result = agent.validate_input({"question": "test"})
        assert result.valid is False
        assert len(result.errors) > 0

    def test_validate_input_json_string(self):
        class InputSchema(BaseModel):
            value: int

        agent = AgentNodeSchema(id="solver", input_schema=InputSchema)
        result = agent.validate_input('{"value": 42}')
        assert result.valid is True

    def test_validate_input_invalid_json(self):
        class InputSchema(BaseModel):
            value: int

        agent = AgentNodeSchema(id="solver", input_schema=InputSchema)
        result = agent.validate_input("not json")
        assert result.valid is False

    def test_validate_input_json_schema_dict(self):
        schema = {
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
        }
        agent = AgentNodeSchema(id="solver", input_schema=schema)
        result = agent.validate_input({"name": "Alice", "age": 30})
        assert result.valid is True

    def test_validate_input_json_schema_missing_required(self):
        schema = {
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string"}},
        }
        agent = AgentNodeSchema(id="solver", input_schema=schema)
        result = agent.validate_input({"age": 30})
        assert result.valid is False
        assert any("name" in e for e in result.errors)

    def test_validate_input_json_schema_wrong_type(self):
        schema = {
            "type": "object",
            "properties": {"age": {"type": "integer"}},
        }
        agent = AgentNodeSchema(id="solver", input_schema=schema)
        result = agent.validate_input({"age": "not_int"})
        assert result.valid is False

    def test_validate_output_no_schema(self):
        agent = AgentNodeSchema(id="solver")
        result = agent.validate_output({"answer": "42"})
        assert result.valid is True

    def test_has_input_schema_true(self):
        class S(BaseModel):
            x: int

        agent = AgentNodeSchema(id="a", input_schema=S)
        assert agent.has_input_schema() is True

    def test_has_input_schema_false(self):
        agent = AgentNodeSchema(id="a")
        assert agent.has_input_schema() is False

    def test_has_output_schema_true(self):
        class S(BaseModel):
            y: str

        agent = AgentNodeSchema(id="a", output_schema=S)
        assert agent.has_output_schema() is True

    def test_get_llm_config(self):
        agent = AgentNodeSchema(
            id="solver",
            llm_backbone="gpt-4",
            base_url="https://api.openai.com/v1",
            temperature=0.7,
        )
        cfg = agent.get_llm_config()
        assert isinstance(cfg, LLMConfig)
        assert cfg.model_name == "gpt-4"
        assert cfg.temperature == 0.7

    def test_has_llm_config_true(self):
        agent = AgentNodeSchema(id="a", llm_backbone="gpt-4")
        assert agent.has_llm_config() is True

    def test_has_llm_config_false(self):
        agent = AgentNodeSchema(id="a")
        assert agent.has_llm_config() is False

    def test_auto_extracts_input_schema_json(self):
        class InputS(BaseModel):
            q: str

        agent = AgentNodeSchema(id="a", input_schema=InputS)
        assert agent.input_schema_json is not None
        assert "q" in agent.input_schema_json.get("properties", {})


# ─────────────────────────── TaskNodeSchema ──────────────────────────────────


class TestTaskNodeSchema:
    def test_creation(self):
        task = TaskNodeSchema(id="task1", query="Solve the problem")
        assert task.id == "task1"
        assert task.type == NodeType.TASK
        assert task.status == "pending"

    def test_with_deadline(self):
        from datetime import UTC, datetime, timedelta

        deadline = datetime.now(UTC) + timedelta(hours=1)
        task = TaskNodeSchema(id="t1", deadline=deadline)
        assert task.deadline is not None


# ─────────────────────────── BaseEdgeSchema ──────────────────────────────────


class TestBaseEdgeSchema:
    def test_basic_creation(self):
        edge = BaseEdgeSchema(source="a", target="b")
        assert edge.source == "a"
        assert edge.target == "b"
        assert edge.weight == 1.0

    def test_to_attr_tensor_with_attr(self):
        edge = BaseEdgeSchema(source="a", target="b", attr=[1.0, 2.0, 3.0])
        t = edge.to_attr_tensor()
        assert isinstance(t, torch.Tensor)
        assert t.shape == (3,)

    def test_to_attr_tensor_default(self):
        edge = BaseEdgeSchema(source="a", target="b")
        t = edge.to_attr_tensor()
        assert isinstance(t, torch.Tensor)
        assert t.shape[0] > 0

    def test_get_feature_vector(self):
        edge = BaseEdgeSchema(source="a", target="b", weight=0.5, probability=0.8)
        fv = edge.get_feature_vector()
        assert isinstance(fv, torch.Tensor)
        # [weight, probability, trust, reliability] = 4 features
        assert fv.shape[0] >= 4

    def test_attr_dim_auto_set(self):
        edge = BaseEdgeSchema(source="a", target="b", attr=[1.0, 2.0])
        assert edge.attr_dim == 2

    def test_embedding_from_tensor(self):
        t = torch.tensor([0.1, 0.2])
        edge = BaseEdgeSchema(source="a", target="b", embedding=cast("list[float]", t))
        assert edge.embedding == [pytest.approx(0.1), pytest.approx(0.2)]


# ─────────────────────────── WorkflowEdgeSchema ──────────────────────────────


class TestWorkflowEdgeSchema:
    def test_basic(self):
        edge = WorkflowEdgeSchema(source="a", target="b")
        assert edge.type == EdgeType.WORKFLOW
        assert edge.is_conditional is False

    def test_with_condition(self):
        edge = WorkflowEdgeSchema(source="a", target="b", condition="source_success")
        assert edge.is_conditional is True
        assert edge.condition == "source_success"

    def test_priority(self):
        edge = WorkflowEdgeSchema(source="a", target="b", priority=5)
        assert edge.priority == 5


# ─────────────────────────── GraphSchema ─────────────────────────────────────


class TestGraphSchema:
    def setup_method(self):
        self.schema = GraphSchema(name="TestGraph")

    def test_empty_schema(self):
        assert self.schema.nodes == {}
        assert self.schema.edges == []

    def test_add_node(self):
        node = AgentNodeSchema(id="solver")
        self.schema.add_node(node)
        assert "solver" in self.schema.nodes

    def test_add_edge(self):
        self.schema.add_node(AgentNodeSchema(id="a"))
        self.schema.add_node(AgentNodeSchema(id="b"))
        edge = BaseEdgeSchema(source="a", target="b")
        self.schema.add_edge(edge)
        assert len(self.schema.edges) == 1

    def test_get_node(self):
        node = AgentNodeSchema(id="solver")
        self.schema.add_node(node)
        retrieved = self.schema.get_node("solver")
        assert retrieved is not None
        assert retrieved.id == "solver"

    def test_get_node_missing(self):
        assert self.schema.get_node("missing") is None

    def test_get_edges_by_source(self):
        for i in range(3):
            self.schema.add_node(AgentNodeSchema(id=f"n{i}"))
        self.schema.add_edge(BaseEdgeSchema(source="n0", target="n1"))
        self.schema.add_edge(BaseEdgeSchema(source="n0", target="n2"))
        self.schema.add_edge(BaseEdgeSchema(source="n1", target="n2"))
        edges = self.schema.get_edges(source="n0")
        assert len(edges) == 2

    def test_get_edges_by_target(self):
        for i in range(3):
            self.schema.add_node(AgentNodeSchema(id=f"n{i}"))
        self.schema.add_edge(BaseEdgeSchema(source="n0", target="n2"))
        self.schema.add_edge(BaseEdgeSchema(source="n1", target="n2"))
        edges = self.schema.get_edges(target="n2")
        assert len(edges) == 2

    def test_compute_feature_dims(self):
        node = AgentNodeSchema(id="n1", embedding=[1.0, 2.0, 3.0])
        self.schema.add_node(node)
        self.schema.add_edge(BaseEdgeSchema(source="n1", target="n1"))
        self.schema.compute_feature_dims()
        assert self.schema.node_feature_dim == 3

    def test_to_dict(self):
        self.schema.add_node(AgentNodeSchema(id="solver"))
        d = self.schema.to_dict()
        assert "nodes" in d
        assert "edges" in d
        assert d["name"] == "TestGraph"


# ─────────────────────────── SchemaValidator ─────────────────────────────────


class TestSchemaValidator:
    def setup_method(self):
        self.schema = GraphSchema()
        self.validator = SchemaValidator(
            check_cycles=True,
            check_duplicates=True,
            check_orphans=True,
        )

    def test_empty_schema_valid(self):
        result = self.validator.validate(self.schema)
        assert result.valid is True

    def test_valid_dag(self):
        for i in range(3):
            self.schema.add_node(AgentNodeSchema(id=f"n{i}"))
        self.schema.add_edge(BaseEdgeSchema(source="n0", target="n1"))
        self.schema.add_edge(BaseEdgeSchema(source="n1", target="n2"))
        result = self.validator.validate(self.schema)
        assert result.valid is True

    def test_invalid_edge_source(self):
        self.schema.add_node(AgentNodeSchema(id="n1"))
        self.schema.add_edge(BaseEdgeSchema(source="missing", target="n1"))
        result = self.validator.validate(self.schema)
        assert result.valid is False
        assert any("missing" in e for e in result.errors)

    def test_invalid_edge_target(self):
        self.schema.add_node(AgentNodeSchema(id="n1"))
        self.schema.add_edge(BaseEdgeSchema(source="n1", target="missing"))
        result = self.validator.validate(self.schema)
        assert result.valid is False

    def test_self_loop_warning(self):
        self.schema.add_node(AgentNodeSchema(id="n1"))
        self.schema.add_edge(BaseEdgeSchema(source="n1", target="n1"))
        result = self.validator.validate(self.schema)
        assert any("self-loop" in w for w in result.warnings)

    def test_cycle_detection(self):
        for i in range(3):
            self.schema.add_node(AgentNodeSchema(id=f"n{i}"))
        self.schema.add_edge(BaseEdgeSchema(source="n0", target="n1"))
        self.schema.add_edge(BaseEdgeSchema(source="n1", target="n2"))
        self.schema.add_edge(BaseEdgeSchema(source="n2", target="n0"))
        result = self.validator.validate(self.schema)
        # Cycle detected → should produce a warning
        assert any("cycle" in w.lower() for w in result.warnings)

    def test_connectivity_check(self):
        validator = SchemaValidator(check_connectivity=True, check_orphans=True)
        for i in range(3):
            self.schema.add_node(AgentNodeSchema(id=f"n{i}"))
        # Only connect n0 → n1, n2 is isolated
        self.schema.add_edge(BaseEdgeSchema(source="n0", target="n1"))
        result = validator.validate(self.schema)
        # n2 should be detected as orphan
        assert any("n2" in w for w in result.warnings)


# ─────────────────────────── ValidationResult ────────────────────────────────


class TestValidationResult:
    def test_initial_valid(self):
        vr = ValidationResult()
        assert vr.valid is True

    def test_add_error(self):
        vr = ValidationResult()
        vr.add_error("Something went wrong")
        assert vr.valid is False
        assert "Something went wrong" in vr.errors

    def test_add_warning(self):
        vr = ValidationResult()
        vr.add_warning("Be careful")
        assert vr.valid is True  # warnings don't affect validity
        assert "Be careful" in vr.warnings


# ─────────────────────────── SchemaValidationResult ──────────────────────────


class TestSchemaValidationResult:
    def test_valid_result(self):
        svr = SchemaValidationResult(valid=True)
        assert svr.valid is True
        svr.raise_if_invalid()  # should not raise

    def test_invalid_result_raises(self):
        svr = SchemaValidationResult(valid=False, errors=["field missing"])
        with pytest.raises(ValueError, match="Schema validation failed"):
            svr.raise_if_invalid()


# ─────────────────────────── Migration ───────────────────────────────────────


class TestMigrationV1ToV2:
    def test_migrate_agents_to_nodes(self):
        migration = MigrationV1ToV2()
        data = {
            "agents": [
                {"agent_id": "solver", "persona": "You are a solver", "description": "desc"},
            ],
            "edges": [
                {"source": "solver", "target": "reviewer", "weight": 1.0},
            ],
        }
        result = migration.migrate(data)
        assert "nodes" in result
        assert "solver" in result["nodes"]
        assert result["edges"][0]["probability"] == 1.0

    def test_migrate_empty_agents(self):
        migration = MigrationV1ToV2()
        data = {"agents": [], "edges": []}
        result = migration.migrate(data)
        assert result["nodes"] == {}

    def test_can_migrate_correct_version(self):
        migration = MigrationV1ToV2()
        assert migration.can_migrate("1.0.0") is True
        assert migration.can_migrate("2.0.0") is False

    def test_migrate_schema_already_current(self):
        data = {"schema_version": SCHEMA_VERSION, "nodes": {}}
        result = migrate_schema(data)
        assert result["schema_version"] == SCHEMA_VERSION


class TestMigrationRegistry:
    def test_migrate_to_latest(self):
        data = {
            "schema_version": "1.0.0",
            "agents": [{"agent_id": "a1", "persona": "p"}],
            "edges": [],
        }
        result = migrate_schema(data)
        assert result["schema_version"] == SCHEMA_VERSION

    def test_get_migration_path(self):
        registry = MigrationRegistry()
        migration = MigrationV1ToV2()
        registry.register(migration)
        path = registry.get_migration_path("1.0.0", "2.0.0")
        assert len(path) == 1

    def test_get_migration_path_unknown(self):
        registry = MigrationRegistry()
        path = registry.get_migration_path("99.0.0", "100.0.0")
        assert path == []


# ─────────────────────────── CostMetrics ─────────────────────────────────────


class TestCostMetrics:
    def test_default(self):
        cm = CostMetrics()
        assert cm.trust == 1.0
        assert cm.reliability == 1.0

    def test_custom_values(self):
        cm = CostMetrics(trust=0.8, reliability=0.9, latency_ms=100.0)
        assert cm.trust == 0.8
        assert cm.latency_ms == 100.0

    def test_extra_fields_allowed(self):
        cm = CostMetrics(custom={"my_metric": 0.5})
        assert cm.custom["my_metric"] == 0.5


# ─────────────────────────── SchemaValidator extra paths ─────────────────────


class TestSchemaValidatorExtraPaths:
    """Test SchemaValidator edge cases for better coverage."""

    def setup_method(self):
        self.validator = SchemaValidator(
            check_cycles=True,
            check_duplicates=True,
            check_orphans=True,
            check_connectivity=True,
        )

    def test_node_id_mismatch(self):
        """Node.id != key in nodes dict."""
        schema = GraphSchema()
        node = AgentNodeSchema(id="agent1")
        # Manually insert with wrong key
        schema.nodes["wrong_key"] = node
        result = self.validator.validate(schema)
        assert any("mismatch" in e for e in result.errors)

    def test_embedding_length_mismatch_warning(self):
        """Embedding length != embedding_dim."""
        schema = GraphSchema()
        node = AgentNodeSchema(id="agent1", embedding=[0.1, 0.2, 0.3], embedding_dim=5)
        # Override embedding_dim to create mismatch
        node = node.model_copy(update={"embedding_dim": 5})
        schema.add_node(node)
        result = self.validator.validate(schema)
        # Should warn about length mismatch
        assert any("embedding" in w for w in result.warnings)

    def test_duplicate_edge_warning(self):
        """Adding duplicate edge should produce warning."""
        schema = GraphSchema()
        schema.add_node(AgentNodeSchema(id="n1"))
        schema.add_node(AgentNodeSchema(id="n2"))
        schema.add_edge(BaseEdgeSchema(source="n1", target="n2"))
        schema.add_edge(BaseEdgeSchema(source="n1", target="n2"))  # duplicate
        result = self.validator.validate(schema)
        assert any("duplicate" in w.lower() for w in result.warnings)

    def test_connectivity_single_node(self):
        """Graph with single node should not error on connectivity check."""
        schema = GraphSchema()
        schema.add_node(AgentNodeSchema(id="n1"))
        result = self.validator.validate(schema)
        assert result.valid is True


# ─────────────────────────── BaseEdgeSchema extra paths ──────────────────────


class TestBaseEdgeSchemaExtraPaths:
    """Test BaseEdgeSchema edge cases."""

    def test_get_feature_vector_with_embedding(self):
        """BaseEdgeSchema.get_feature_vector with embedding."""
        edge = BaseEdgeSchema(source="a", target="b", embedding=[0.1, 0.2, 0.3])
        fv = edge.get_feature_vector()
        # Should include weight, probability, trust, reliability + embedding
        assert fv.shape[0] >= 4 + 3

    def test_get_feature_vector_with_feature_names(self):
        """Test feature_names including cost attribute."""
        edge = BaseEdgeSchema(source="a", target="b")
        fv = edge.get_feature_vector(feature_names=["trust", "reliability"])
        assert fv.shape[0] == 4 + 2  # base + 2 named features

    def test_get_feature_vector_with_metadata_name(self):
        """Test feature_names including metadata field."""
        edge = BaseEdgeSchema(source="a", target="b", metadata={"custom_feat": 0.7})
        fv = edge.get_feature_vector(feature_names=["custom_feat"])
        assert fv.shape[0] == 5  # 4 base + 1 named

    def test_normalize_embedding_list_input(self):
        """Test that embedding normalizer handles list values."""
        edge = BaseEdgeSchema(source="a", target="b", embedding=[1.0, 2.0, 3.0])
        assert edge.embedding == [1.0, 2.0, 3.0]

    def test_output_schema_as_dict(self):
        """Test that output_schema as dict sets output_schema_json."""
        from gmas.core.schema import AgentNodeSchema

        schema_dict = {"type": "object", "properties": {"result": {"type": "string"}}}
        node = AgentNodeSchema(id="n1", output_schema=schema_dict)
        assert node.output_schema_json == schema_dict

    def test_validate_data_unknown_schema_type(self):
        """Test _validate_data when schema is not a pydantic model or dict."""
        from gmas.core.schema import AgentNodeSchema

        node = AgentNodeSchema(id="n1")
        # Call _validate_data with a non-standard schema type (string)
        bad_schema = cast("type[BaseModel] | dict | None", "not_a_dict_or_model")
        result = node._validate_data({"key": "value"}, bad_schema, "input")
        assert result.valid is False
        assert "Unknown schema type" in result.errors

    def test_check_type_unknown_returns_true(self):
        """Test _check_type with unknown type returns True."""
        from gmas.core.schema import AgentNodeSchema

        node = AgentNodeSchema(id="n1")
        result = node._check_type("any_value", "unknown_type_xyz")
        assert result is True
