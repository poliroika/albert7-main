"""Test project setup and module imports."""
import sys
def test_module_imports():
    """Test that the calculator module can be imported."""
    import calculator
    assert calculator.__version__ == "0.1.0"
def test_pyproject_exists():
    """Test that pyproject.toml exists and is valid."""
    import os
    from pathlib import Path
    workspace_root = Path(__file__).parent.parent
    pyproject_path = workspace_root / "pyproject.toml"
    assert pyproject_path.exists(), "pyproject.toml must exist"
    assert pyproject_path.is_file(), "pyproject.toml must be a file"
def test_src_package_structure():
    """Test that src package structure is correct."""
    import sys
    from pathlib import Path
    workspace_root = Path(__file__).parent.parent
    src_package = workspace_root / "src" / "calculator"
    assert src_package.exists(), "src/calculator directory must exist"
    assert src_package.is_dir(), "src/calculator must be a directory"
    assert (src_package / "__init__.py").exists(), "__init__.py must exist in package"
