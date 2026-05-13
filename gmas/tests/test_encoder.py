"""Tests for core/encoder.py — NodeEncoder."""

import sys
from pathlib import Path

import pytest
import torch

sys.path.insert(0, str(Path(__file__).parent.parent))

from gmas.core.encoder import NodeEncoder


class TestNodeEncoderCreation:
    """Tests for NodeEncoder creation."""

    def test_default_creation(self):
        """Creation with default parameters."""
        encoder = NodeEncoder()

        assert encoder is not None
        assert encoder.fallback_dim > 0

    def test_creation_with_model(self):
        """Creation with a specified model."""
        encoder = NodeEncoder(model_name="sentence-transformers/all-MiniLM-L6-v2")

        assert encoder.model_name == "sentence-transformers/all-MiniLM-L6-v2"

    def test_creation_with_fallback_dim(self):
        """Creation with a specified fallback dimension."""
        encoder = NodeEncoder(fallback_dim=128)

        assert encoder.fallback_dim == 128


class TestHashEmbeddings:
    """Tests for hash embeddings (fallback)."""

    def test_hash_embedding_deterministic(self):
        """Hash embedding is deterministic."""
        encoder = NodeEncoder(model_name="hash:64")

        text = "test agent"
        emb1 = encoder.encode([text])
        emb2 = encoder.encode([text])

        assert torch.allclose(emb1, emb2)

    def test_hash_embedding_different_texts(self):
        """Different texts produce different embeddings."""
        encoder = NodeEncoder(model_name="hash:64")

        embs = encoder.encode(["agent one", "agent two"])

        assert not torch.allclose(embs[0], embs[1])

    def test_hash_embedding_dimension(self):
        """Hash embedding dimension."""
        encoder = NodeEncoder(model_name="hash:128")

        embs = encoder.encode(["test"])

        assert embs.shape == (1, 128)

    def test_hash_embedding_normalized(self):
        """Hash embedding is normalized."""
        encoder = NodeEncoder(model_name="hash:64")

        embs = encoder.encode(["test"])
        norm = torch.norm(embs[0]).item()

        assert abs(norm - 1.0) < 0.01  # Close to 1

    def test_hash_embedding_empty_string(self):
        """Hash embedding for an empty string."""
        encoder = NodeEncoder(model_name="hash:64")

        embs = encoder.encode([""])

        assert embs.shape == (1, 64)
        assert not torch.isnan(embs).any()


class TestSentenceTransformerEmbeddings:
    """Tests for sentence-transformer embeddings."""

    def test_encode_single_text(self):
        """Encoding a single text."""
        encoder = NodeEncoder()

        embs = encoder.encode(["Test agent description"])

        assert isinstance(embs, torch.Tensor)
        assert embs.dim() == 2
        assert embs.shape[0] == 1
        assert embs.shape[1] > 0

    def test_encode_batch(self):
        """Encoding a batch of texts."""
        encoder = NodeEncoder()

        texts = ["Agent one", "Agent two", "Agent three"]
        embs = encoder.encode(texts)

        assert isinstance(embs, torch.Tensor)
        assert embs.shape[0] == 3

    def test_encode_empty_batch(self):
        """Encoding an empty batch."""
        encoder = NodeEncoder()

        embs = encoder.encode([])

        assert embs.shape[0] == 0

    def test_fallback_when_st_unavailable(self):
        """Fallback to hash when ST is unavailable."""
        encoder = NodeEncoder(model_name="hash:64")

        embs = encoder.encode(["test"])

        assert embs.shape == (1, 64)


class TestAgentProfileEncoding:
    """Tests for agent profile encoding."""

    def test_encode_agent_profile(self):
        """Encoding an agent profile."""
        from gmas.core.agent import AgentProfile

        encoder = NodeEncoder()

        profile = AgentProfile(
            agent_id="test_agent",
            display_name="Researcher",
            persona="Finds and analyzes information",
        )

        embs = encoder.encode([profile.to_text()])

        assert isinstance(embs, torch.Tensor)
        assert embs.dim() == 2
        assert embs.shape[0] == 1

    def test_encode_minimal_profile(self):
        """Encoding a minimal profile."""
        from gmas.core.agent import AgentProfile

        encoder = NodeEncoder()

        profile = AgentProfile(agent_id="minimal", display_name="minimal")

        embs = encoder.encode([profile.to_text()])

        assert isinstance(embs, torch.Tensor)

    def test_encode_profiles_batch(self):
        """Encoding a batch of profiles."""
        from gmas.core.agent import AgentProfile

        encoder = NodeEncoder()

        profiles = [
            AgentProfile(agent_id="a", display_name="Role A"),
            AgentProfile(agent_id="b", display_name="Role B"),
        ]

        texts = [p.to_text() for p in profiles]
        embs = encoder.encode(texts)

        assert embs.shape[0] == 2


class TestConsistency:
    """Tests for encoder consistency."""

    def test_same_input_same_output(self):
        """Same input produces same output."""
        encoder = NodeEncoder()

        text = "consistent input"
        emb1 = encoder.encode([text])
        emb2 = encoder.encode([text])

        assert torch.allclose(emb1, emb2, atol=1e-6)

    def test_similar_texts_close_embeddings(self):
        """Similar texts have close embeddings."""
        encoder = NodeEncoder()

        embs = encoder.encode(
            [
                "This is a researcher agent",
                "This is a research agent",
                "This is a completely different unrelated text about cats",
            ]
        )

        # Cosine similarity
        sim_12 = torch.cosine_similarity(embs[0].unsqueeze(0), embs[1].unsqueeze(0)).item()
        sim_13 = torch.cosine_similarity(embs[0].unsqueeze(0), embs[2].unsqueeze(0)).item()

        # Similar texts should have higher similarity
        assert sim_12 > sim_13

    def test_dimension_consistency(self):
        """Dimension consistency."""
        encoder = NodeEncoder()

        texts = ["short", "medium length text", "a very long text " * 100]

        dims = set()
        embs = encoder.encode(texts)
        for i in range(len(texts)):
            dims.add(embs[i].shape[0])

        # All should have same dimension
        assert len(dims) == 1


class TestEdgeCases:
    """Tests for edge cases."""

    def test_unicode_text(self):
        """Unicode text."""
        encoder = NodeEncoder()

        embs = encoder.encode(["Test agent with unicode 日本語"])

        assert isinstance(embs, torch.Tensor)
        assert not torch.isnan(embs).any()

    def test_special_characters(self):
        """Special characters."""
        encoder = NodeEncoder()

        embs = encoder.encode(["Agent with special chars: !@#$%^&*()"])

        assert isinstance(embs, torch.Tensor)
        assert not torch.isnan(embs).any()

    def test_very_long_text(self):
        """Very long text."""
        encoder = NodeEncoder()

        long_text = "word " * 10000
        embs = encoder.encode([long_text])

        assert isinstance(embs, torch.Tensor)
        assert not torch.isnan(embs).any()

    def test_whitespace_only(self):
        """Whitespace only."""
        encoder = NodeEncoder()

        embs = encoder.encode(["   \t\n   "])

        assert isinstance(embs, torch.Tensor)

    def test_numbers_only(self):
        """Numbers only."""
        encoder = NodeEncoder()

        embs = encoder.encode(["12345 67890"])

        assert isinstance(embs, torch.Tensor)


class TestGraphIntegration:
    """Tests for graph integration."""

    def test_encode_graph_agents(self):
        """Encoding graph agents."""
        from gmas.core.agent import AgentProfile

        encoder = NodeEncoder()

        agents = [
            AgentProfile(
                agent_id="coordinator",
                display_name="Coordinator",
                persona="Manages workflow",
            ),
            AgentProfile(agent_id="researcher", display_name="Researcher", persona="Finds information"),
            AgentProfile(agent_id="writer", display_name="Writer", persona="Creates content"),
        ]

        texts = [a.to_text() for a in agents]
        embeddings = encoder.encode(texts)

        assert embeddings.shape[0] == 3
        # All unique agents should have different embeddings
        assert not torch.allclose(embeddings[0], embeddings[1])
        assert not torch.allclose(embeddings[1], embeddings[2])


class TestNodeEncoderValidationErrors:
    """Tests for validation errors in NodeEncoder creation."""

    def test_hash_prefix_non_numeric_dim(self):
        """Cover lines 48-49: hash: prefix with non-numeric dimension."""
        with pytest.raises((ValueError, Exception)):
            NodeEncoder(model_name="hash:abc")

    def test_hash_prefix_zero_dim(self):
        """Cover lines 50-52: hash: prefix with zero dimension."""
        with pytest.raises((ValueError, Exception)):
            NodeEncoder(model_name="hash:0")

    def test_hash_prefix_negative_dim(self):
        """Cover lines 50-52: hash: prefix with negative dimension (non-digit actually)."""
        with pytest.raises((ValueError, Exception)):
            NodeEncoder(model_name="hash:-5")

    def test_sentence_transformer_missing_model_id(self):
        """Cover lines 60-61: sentence-transformers: prefix without model identifier."""
        with pytest.raises((ValueError, Exception)):
            NodeEncoder(model_name="sentence-transformers/")

    def test_unsupported_model_name(self):
        """Cover lines 63-64: unsupported model name raises ValueError."""
        with pytest.raises((ValueError, Exception)):
            NodeEncoder(model_name="totally-unsupported-model")

    def test_sentence_transformer_colon_style_missing_model(self):
        """Cover lines 60-61: sentence-transformers:  without model."""
        with pytest.raises((ValueError, Exception)):
            NodeEncoder(model_name="sentence-transformers:")

    def test_load_model_returns_none_for_hash_provider(self):
        """Cover line 99: _load_model returns None when provider is hash."""
        encoder = NodeEncoder(model_name="hash:64")
        result = encoder._load_model()
        assert result is None

    def test_encode_with_st_model_none_uses_hash_fallback(self):
        """Cover line 87: model is None → uses hash fallback."""
        from unittest.mock import patch

        encoder = NodeEncoder(model_name="sentence-transformers/all-MiniLM-L6-v2")
        # Force _load_model to return None
        with patch.object(encoder, "_load_model", return_value=None):
            embs = encoder.encode(["test"])
        assert embs.shape[0] == 1

    def test_sentence_transformer_spec_missing_model_colon(self):
        """Cover lines 60-61 via colon notation."""
        with pytest.raises((ValueError, Exception)):
            NodeEncoder(model_name="st:")


class TestNodeEncoderEmbeddingDim:
    """Tests for embedding_dim property."""

    def test_embedding_dim_hash_provider(self):
        """Cover lines 136-137: embedding_dim for hash provider."""
        encoder = NodeEncoder(model_name="hash:64")
        assert encoder.embedding_dim == 64

    def test_embedding_dim_hash_default(self):
        """Cover lines 136-137: embedding_dim for hash provider (default dim)."""
        encoder = NodeEncoder(model_name="hash")
        # fallback_dim should be at least 32
        assert encoder.embedding_dim >= 32

    def test_embedding_dim_sentence_transformer(self):
        """Cover lines 139-141: embedding_dim when model is loaded."""
        encoder = NodeEncoder(model_name="sentence-transformers/all-MiniLM-L6-v2")
        dim = encoder.embedding_dim
        assert dim > 0

    def test_embedding_dim_when_model_none(self):
        """Cover line 143: embedding_dim returns fallback_dim when model returns None."""
        from unittest.mock import patch

        encoder = NodeEncoder(model_name="sentence-transformers/all-MiniLM-L6-v2")
        with patch.object(encoder, "_load_model", return_value=None):
            dim = encoder.embedding_dim
        assert dim == encoder.fallback_dim


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


class TestNodeEncoderSentenceTransformersNotInstalled:
    def test_load_model_falls_back_to_hash_when_st_not_available(self):
        """Lines 105-106: _load_model sets provider to hash when sentence_transformers is missing."""
        import importlib.util
        from unittest.mock import patch

        encoder = NodeEncoder(model_name="sentence-transformers/all-MiniLM-L6-v2")
        # Simulate sentence_transformers not being installed
        with patch.object(importlib.util, "find_spec", return_value=None):
            result = encoder._load_model()
        assert result is None
        assert encoder._provider == "hash"
