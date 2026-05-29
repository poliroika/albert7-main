"""
Tests for the calculator package entrypoint.
Verifies that the package is executable via 'python -m calculator'.
"""
import sys
from pathlib import Path
def test_entrypoint_imports():
    """Test that the __main__ module can be imported without errors."""
    # This verifies the entrypoint module exists and is importable
    import calculator.__main__
    assert calculator.__main__ is not None
    assert hasattr(calculator.__main__, 'main')
def test_entrypoint_has_main():
    """Test that the entrypoint provides a main() function."""
    from calculator import gui
    assert hasattr(gui, 'main')
    # main is a callable function
    assert callable(gui.main)
