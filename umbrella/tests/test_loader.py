"""
Tests for umbrella.policies.loader and umbrella.policies.defaults.
"""

import pytest
from pathlib import Path
import tempfile
import os

from umbrella.policies.loader import load_policy, load_policy_from_file
from umbrella.policies.models import SystemBoundaryPolicy
from umbrella.policies.defaults import load_default_policy, DEFAULT_POLICY_PATH


class TestLoadDefaultPolicy:
    """Tests for load_default_policy function."""

    def test_load_default_policy_returns_policy(self):
        policy = load_default_policy()
        assert isinstance(policy, SystemBoundaryPolicy)
        assert policy.workspace_first is True
        assert policy.standalone_workspace_required is True
        assert policy.self_improvement.min_repeated_failures == 3
        assert policy.framework_boundary.gmas_readonly is True

    def test_default_policy_path_name(self):
        assert DEFAULT_POLICY_PATH.name == "default_policy.yaml"

    def test_default_policy_yaml_on_disk_parses(self):
        assert DEFAULT_POLICY_PATH.is_file()
        policy = load_policy_from_file(DEFAULT_POLICY_PATH)
        assert isinstance(policy, SystemBoundaryPolicy)
        assert policy.documentation_first_retrieval is True


class TestLoadPolicyFromFile:
    """Tests for load_policy_from_file function."""

    def test_load_from_yaml_file(self):
        yaml_content = """
system_boundary:
  workspace_first: false
  standalone_workspace_required: false
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            f.close()

            policy = load_policy_from_file(Path(f.name))
            assert isinstance(policy, SystemBoundaryPolicy)
            assert policy.workspace_first is False
            assert policy.standalone_workspace_required is False

            os.unlink(f.name)

    def test_load_from_toml_file(self):
        toml_content = """
[system_boundary]
workspace_first = false
standalone_workspace_required = false
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write(toml_content)
            f.flush()
            f.close()

            policy = load_policy_from_file(Path(f.name))
            assert isinstance(policy, SystemBoundaryPolicy)
            assert policy.workspace_first is False
            assert policy.standalone_workspace_required is False

            os.unlink(f.name)

    def test_load_nonexistent_file(self):
        with pytest.raises(FileNotFoundError):
            load_policy_from_file(Path("/nonexistent/policy.yaml"))

    def test_load_invalid_format(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("some content")
            f.flush()
            f.close()

            with pytest.raises(ValueError):
                load_policy_from_file(Path(f.name))

            os.unlink(f.name)


class TestLoadPolicy:
    """Tests for load_policy function."""

    def test_load_policy_from_none(self):
        policy = load_policy(None)
        assert isinstance(policy, SystemBoundaryPolicy)
        assert policy.workspace_first is True

    def test_load_policy_from_path(self):
        yaml_content = """
system_boundary:
  workspace_first: false
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            f.close()

            policy = load_policy(Path(f.name))
            assert policy.workspace_first is False

            os.unlink(f.name)

    def test_load_policy_from_string(self):
        yaml_content = """
system_boundary:
  workspace_first: false
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            f.close()

            policy = load_policy(f.name)
            assert policy.workspace_first is False

            os.unlink(f.name)
