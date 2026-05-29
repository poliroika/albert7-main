"""
Tests for the calculator core logic model.
"""
import pytest
from calculator.model import (
    calculate,
    CalculatorError,
    DivisionByZeroError,
    InvalidOperationError
)
class TestValidOperations:
    """Test valid arithmetic operations."""
    def test_add_positive_numbers(self):
        result = calculate('add', 5, 3)
        assert result == 8
    def test_add_negative_numbers(self):
        result = calculate('add', -5, -3)
        assert result == -8
    def test_add_mixed_sign_numbers(self):
        result = calculate('add', -5, 3)
        assert result == -2
    def test_subtract_positive_numbers(self):
        result = calculate('subtract', 10, 4)
        assert result == 6
    def test_subtract_negative_numbers(self):
        result = calculate('subtract', -10, -4)
        assert result == -6
    def test_multiply_positive_numbers(self):
        result = calculate('multiply', 7, 2)
        assert result == 14
    def test_multiply_negative_numbers(self):
        result = calculate('multiply', -7, 2)
        assert result == -14
    def test_multiply_by_zero(self):
        result = calculate('multiply', 5, 0)
        assert result == 0
    def test_multiply_by_one(self):
        result = calculate('multiply', 5, 1)
        assert result == 5
    def test_divide_positive_numbers(self):
        result = calculate('divide', 15, 3)
        assert result == 5
    def test_divide_negative_numbers(self):
        result = calculate('divide', -15, -3)
        assert result == 5
    def test_divide_mixed_sign_numbers(self):
        result = calculate('divide', -15, 3)
        assert result == -5
    def test_divide_result_is_float(self):
        result = calculate('divide', 7, 2)
        assert result == 3.5
    def test_divide_by_one(self):
        result = calculate('divide', 15, 1)
        assert result == 15
class TestDivisionByZero:
    """Test division by zero error handling."""
    def test_divide_by_zero_raises_error(self):
        with pytest.raises(DivisionByZeroError):
            calculate('divide', 5, 0)
    def test_divide_zero_by_zero_raises_error(self):
        with pytest.raises(DivisionByZeroError):
            calculate('divide', 0, 0)
    def test_divide_by_zero_error_message(self):
        with pytest.raises(DivisionByZeroError) as exc_info:
            calculate('divide', 5, 0)
        assert "Cannot divide by zero" in str(exc_info.value)
class TestInvalidOperation:
    """Test invalid operation error handling."""
    def test_invalid_operation_raises_error(self):
        with pytest.raises(InvalidOperationError):
            calculate('invalid_op', 1, 2)
    def test_power_operation_raises_error(self):
        with pytest.raises(InvalidOperationError):
            calculate('power', 2, 3)
    def test_modulo_operation_raises_error(self):
        with pytest.raises(InvalidOperationError):
            calculate('modulo', 5, 2)
    def test_invalid_operation_error_message(self):
        with pytest.raises(InvalidOperationError) as exc_info:
            calculate('invalid_op', 1, 2)
        assert "Invalid operation: invalid_op" in str(exc_info.value)
class TestDistinctInputsDistinctOutputs:
    """Test that distinct inputs produce distinct outputs."""
    def test_different_additions_produce_different_results(self):
        assert calculate('add', 1, 2) != calculate('add', 2, 3)
    def test_different_subtractions_produce_different_results(self):
        assert calculate('subtract', 10, 5) != calculate('subtract', 10, 3)
    def test_different_multiplications_produce_different_results(self):
        assert calculate('multiply', 2, 3) != calculate('multiply', 3, 4)
    def test_different_divisions_produce_different_results(self):
        assert calculate('divide', 10, 2) != calculate('divide', 10, 5)
    def test_same_operation_different_numbers(self):
        result1 = calculate('add', 5, 3)
        result2 = calculate('add', 5, 4)
        result3 = calculate('add', 6, 3)
        assert result1 != result2 != result3
class TestEdgeCases:
    """Test edge cases and boundary conditions."""
    def test_floating_point_numbers(self):
        result = calculate('add', 0.1, 0.2)
        assert abs(result - 0.3) < 1e-9  # Account for floating point precision
    def test_large_numbers(self):
        result = calculate('multiply', 1_000_000, 1_000_000)
        assert result == 1_000_000_000_000
    def test_fractional_division(self):
        result = calculate('divide', 1, 3)
        assert abs(result - 0.3333333333333333) < 1e-9
    def test_zero_components(self):
        assert calculate('add', 0, 5) == 5
        assert calculate('subtract', 0, 5) == -5
        assert calculate('add', 5, 0) == 5
        assert calculate('subtract', 5, 0) == 5
        assert calculate('multiply', 0, 5) == 0
class TestCalculatorErrorHierarchy:
    """Test that custom exceptions inherit properly."""
    def test_division_by_zero_is_calculator_error(self):
        with pytest.raises(CalculatorError):
            calculate('divide', 5, 0)
    def test_invalid_operation_is_calculator_error(self):
        with pytest.raises(CalculatorError):
            calculate('invalid', 1, 2)
    def test_division_by_zero_is_exception(self):
        with pytest.raises(Exception):
            calculate('divide', 5, 0)
    def test_invalid_operation_is_exception(self):
        with pytest.raises(Exception):
            calculate('invalid', 1, 2)
class TestCalculatorRequirements:
    """Test specific requirements from the task specification."""
    def test_add_5_plus_3_equals_8(self):
        """Test from generated test contract: calc_add claim."""
        result = calculate('add', 5, 3)
        assert result == 8
    def test_divide_by_zero_raises_error(self):
        """Test from generated test contract: calc_divide_by_zero claim."""
        with pytest.raises(DivisionByZeroError):
            calculate('divide', 5, 0)
    def test_subtract_10_minus_4_equals_6(self):
        """Test from generated test contract valid values."""
        result = calculate('subtract', 10, 4)
        assert result == 6
    def test_multiply_7_times_2_equals_14(self):
        """Test from generated test contract valid values."""
        result = calculate('multiply', 7, 2)
        assert result == 14
    def test_divide_15_divided_by_3_equals_5(self):
        """Test from generated test contract valid values."""
        result = calculate('divide', 15, 3)
        assert result == 5
