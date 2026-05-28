"""Shared pytest fixtures and configuration for calculator tests."""
import pytest
@pytest.fixture
def calculator():
    """Provide a fresh Calculator instance for each test."""
    from calculator import Calculator
    return Calculator()
