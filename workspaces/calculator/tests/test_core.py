"""Tests for core calculator logic."""
import pytest
from calculator.core import Calculator
class TestCalculatorBasicOperations:
    """Test basic arithmetic operations."""
    def test_addition(self, calculator):
        assert calculator.add(2, 3) == 5
        assert calculator.add(-1, 1) == 0
        assert calculator.add(0.5, 0.5) == 1.0
    def test_subtraction(self, calculator):
        assert calculator.subtract(5, 3) == 2
        assert calculator.subtract(3, 5) == -2
        assert calculator.subtract(0, 0) == 0
    def test_multiplication(self, calculator):
        assert calculator.multiply(3, 4) == 12
        assert calculator.multiply(-2, 3) == -6
        assert calculator.multiply(0.5, 2) == 1.0
    def test_division(self, calculator):
        assert calculator.divide(10, 2) == 5.0
        assert calculator.divide(3, 2) == 1.5
        assert calculator.divide(-6, 3) == -2.0
    def test_division_by_zero(self, calculator):
        with pytest.raises(ValueError, match="Division by zero"):
            calculator.divide(5, 0)
        with pytest.raises(ValueError, match="Division by zero"):
            calculator.divide(0, 0)
class TestCalculatorCalculateMethod:
    """Test the generic calculate method."""
    def test_calculate_with_valid_operations(self, calculator):
        assert calculator.calculate('+', 3, 4) == 7
        assert calculator.calculate('-', 10, 4) == 6
        assert calculator.calculate('*', 3, 4) == 12
        assert calculator.calculate('/', 8, 2) == 4.0
    def test_calculate_with_invalid_operation(self, calculator):
        with pytest.raises(ValueError, match="Invalid operation"):
            calculator.calculate('^', 2, 3)
        with pytest.raises(ValueError, match="Invalid operation"):
            calculator.calculate('foo', 1, 2)
    def test_calculate_division_by_zero(self, calculator):
        with pytest.raises(ValueError, match="Division by zero"):
            calculator.calculate('/', 5, 0)
class TestCalculatorEdgeCases:
    """Test edge cases and special values."""
    def test_negative_numbers(self, calculator):
        assert calculator.add(-5, -3) == -8
        assert calculator.multiply(-2, -3) == 6
        assert calculator.multiply(-2, 3) == -6
    def test_float_precision(self, calculator):
        result = calculator.divide(1, 3)
        assert abs(result - 0.3333333333333333) < 1e-10
    def test_large_numbers(self, calculator):
        assert calculator.add(1e10, 1e10) == 2e10
        assert calculator.multiply(1e5, 1e5) == 1e10
    def test_mixed_types(self, calculator):
        assert calculator.add(2, 3.5) == 5.5
        assert calculator.multiply(2, 3.5) == 7.0
