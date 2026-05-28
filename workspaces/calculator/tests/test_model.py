"""
Tests for calculator model core.
"""
import pytest
from calculator.model import (
    CalculatorModel,
    OperationType,
    CalculatorError,
    DivisionByZeroError,
    InvalidInputError,
    calculate
)
class TestBasicOperations:
    """Test basic arithmetic operations."""
    def test_add_positive_numbers(self):
        """Test adding two positive numbers."""
        model = CalculatorModel()
        result = model.add(5.0, 3.0)
        assert result == 8.0
    def test_add_negative_numbers(self):
        """Test adding two negative numbers."""
        model = CalculatorModel()
        result = model.add(-5.0, -3.0)
        assert result == -8.0
    def test_subtract_positive_numbers(self):
        """Test subtracting two positive numbers."""
        model = CalculatorModel()
        result = model.subtract(10.0, 3.0)
        assert result == 7.0
    def test_subtract_negative_numbers(self):
        """Test subtracting negative numbers."""
        model = CalculatorModel()
        result = model.subtract(10.0, -3.0)
        assert result == 13.0
    def test_multiply_positive_numbers(self):
        """Test multiplying two positive numbers."""
        model = CalculatorModel()
        result = model.multiply(4.0, 3.0)
        assert result == 12.0
    def test_multiply_negative_numbers(self):
        """Test multiplying negative numbers."""
        model = CalculatorModel()
        result = model.multiply(-4.0, -3.0)
        assert result == 12.0
    def test_multiply_by_zero(self):
        """Test multiplying by zero."""
        model = CalculatorModel()
        result = model.multiply(4.0, 0.0)
        assert result == 0.0
    def test_divide_positive_numbers(self):
        """Test dividing two positive numbers."""
        model = CalculatorModel()
        result = model.divide(12.0, 3.0)
        assert result == 4.0
    def test_divide_negative_numbers(self):
        """Test dividing negative numbers."""
        model = CalculatorModel()
        result = model.divide(-12.0, -3.0)
        assert result == 4.0
    def test_divide_negative_by_positive(self):
        """Test dividing negative by positive."""
        model = CalculatorModel()
        result = model.divide(-12.0, 3.0)
        assert result == -4.0
class TestDivisionByZero:
    """Test division by zero error handling."""
    def test_divide_by_zero_raises_error(self):
        """Test that dividing by zero raises DivisionByZeroError."""
        model = CalculatorModel()
        with pytest.raises(DivisionByZeroError):
            model.divide(5.0, 0.0)
    def test_divide_negative_by_zero_raises_error(self):
        """Test that dividing negative number by zero raises DivisionByZeroError."""
        model = CalculatorModel()
        with pytest.raises(DivisionByZeroError):
            model.divide(-5.0, 0.0)
    def test_divide_zero_by_zero_raises_error(self):
        """Test that dividing zero by zero raises DivisionByZeroError."""
        model = CalculatorModel()
        with pytest.raises(DivisionByZeroError):
            model.divide(0.0, 0.0)
    def test_execute_operation_divide_by_zero(self):
        """Test execute_operation with division by zero."""
        model = CalculatorModel()
        with pytest.raises(DivisionByZeroError):
            model.execute_operation(5.0, 0.0, OperationType.DIVIDE)
class TestInvalidInput:
    """Test invalid input error handling."""
    def test_parse_operand_valid_integer(self):
        """Test parsing a valid integer string."""
        model = CalculatorModel()
        result = model.parse_operand("42")
        assert result == 42.0
    def test_parse_operand_valid_float(self):
        """Test parsing a valid float string."""
        model = CalculatorModel()
        result = model.parse_operand("3.14")
        assert result == 3.14
    def test_parse_operand_negative_number(self):
        """Test parsing a negative number string."""
        model = CalculatorModel()
        result = model.parse_operand("-5.5")
        assert result == -5.5
    def test_parse_operand_invalid_string(self):
        """Test that parsing an invalid string raises InvalidInputError."""
        model = CalculatorModel()
        with pytest.raises(InvalidInputError):
            model.parse_operand("abc")
    def test_parse_operand_empty_string(self):
        """Test that parsing an empty string raises InvalidInputError."""
        model = CalculatorModel()
        with pytest.raises(InvalidInputError):
            model.parse_operand("")
    def test_parse_operand_none(self):
        """Test that parsing None raises InvalidInputError."""
        model = CalculatorModel()
        with pytest.raises(InvalidInputError):
            model.parse_operand(None)
class TestExecuteOperation:
    """Test execute_operation method."""
    def test_execute_add_operation(self):
        """Test executing add operation."""
        model = CalculatorModel()
        result = model.execute_operation(5.0, 3.0, OperationType.ADD)
        assert result == 8.0
    def test_execute_subtract_operation(self):
        """Test executing subtract operation."""
        model = CalculatorModel()
        result = model.execute_operation(10.0, 3.0, OperationType.SUBTRACT)
        assert result == 7.0
    def test_execute_multiply_operation(self):
        """Test executing multiply operation."""
        model = CalculatorModel()
        result = model.execute_operation(4.0, 3.0, OperationType.MULTIPLY)
        assert result == 12.0
    def test_execute_divide_operation(self):
        """Test executing divide operation."""
        model = CalculatorModel()
        result = model.execute_operation(12.0, 3.0, OperationType.DIVIDE)
        assert result == 4.0
class TestStatefulOperations:
    """Test stateful calculator operations for chained calculations."""
    def test_apply_operation_initial(self):
        """Test applying the first operation."""
        model = CalculatorModel()
        model.apply_operation(OperationType.ADD, 5.0)
        assert model.accumulator == 5.0
        assert model.pending_operation == OperationType.ADD
    def test_apply_operation_chain(self):
        """Test chaining operations."""
        model = CalculatorModel()
        model.apply_operation(OperationType.ADD, 5.0)
        model.apply_operation(OperationType.SUBTRACT, 3.0)
        # First op should have been executed
        assert model.accumulator == 2.0  # 5 + (previous) - 3
        assert model.pending_operation == OperationType.SUBTRACT
    def test_calculate_simple(self):
        """Test a simple calculation."""
        model = CalculatorModel()
        model.apply_operation(OperationType.ADD, 5.0)
        result = model.calculate(3.0)
        assert result == 8.0
        assert model.pending_operation is None
    def test_calculate_chained(self):
        """Test chained calculations."""
        model = CalculatorModel()
        model.apply_operation(OperationType.ADD, 5.0)
        model.apply_operation(OperationType.MULTIPLY, 2.0)
        result = model.calculate(3.0)
        # (5 + 2) * 3 = 21? Wait, let's recalculate
        # apply_operation(ADD, 5): accumulator=5
        # apply_operation(MULTIPLY, 2): accumulator=5+2=7
        # calculate(3): result=7*3=21
        assert result == 21.0
    def test_clear_state(self):
        """Test clearing calculator state."""
        model = CalculatorModel()
        model.apply_operation(OperationType.ADD, 5.0)
        model.clear()
        assert model.accumulator == 0.0
        assert model.pending_operation is None
        assert model.new_operand is None
    def test_has_pending_operation(self):
        """Test checking for pending operations."""
        model = CalculatorModel()
        assert not model.has_pending_operation()
        model.apply_operation(OperationType.ADD, 5.0)
        assert model.has_pending_operation()
class TestDistinctInputsDistinctOutputs:
    """Test that different inputs produce different outputs."""
    def test_different_inputs_different_outputs_add(self):
        """Different operands should give different results for addition."""
        model = CalculatorModel()
        result1 = model.add(1.0, 2.0)
        result2 = model.add(2.0, 3.0)
        assert result1 != result2
    def test_different_inputs_different_outputs_multiply(self):
        """Different operands should give different results for multiplication."""
        model = CalculatorModel()
        result1 = model.multiply(2.0, 3.0)
        result2 = model.multiply(3.0, 4.0)
        assert result1 != result2
    def test_different_operations_different_results(self):
        """Different operations on same inputs should give different results."""
        model = CalculatorModel()
        result_add = model.add(5.0, 3.0)
        result_subtract = model.subtract(5.0, 3.0)
        result_multiply = model.multiply(5.0, 3.0)
        result_divide = model.divide(6.0, 3.0)
        # All should be different
        assert len({result_add, result_subtract, result_multiply, result_divide}) == 4
class TestConvenienceFunction:
    """Test the convenience calculate function."""
    def test_calculate_convenience_add(self):
        """Test convenience function for addition."""
        result = calculate(5.0, 3.0, OperationType.ADD)
        assert result == 8.0
    def test_calculate_convenience_divide(self):
        """Test convenience function for division."""
        result = calculate(12.0, 3.0, OperationType.DIVIDE)
        assert result == 4.0
    def test_calculate_convenience_divide_by_zero(self):
        """Test convenience function raises error on division by zero."""
        with pytest.raises(DivisionByZeroError):
            calculate(5.0, 0.0, OperationType.DIVIDE)
class TestEdgeCases:
    """Test edge cases and boundary conditions."""
    def test_very_large_numbers(self):
        """Test operations with very large numbers."""
        model = CalculatorModel()
        result = model.add(1e100, 1e100)
        assert result == 2e100
    def test_very_small_numbers(self):
        """Test operations with very small numbers."""
        model = CalculatorModel()
        result = model.divide(1e-10, 1e-10)
        assert result == 1.0
    def test_decimal_precision(self):
        """Test that decimal operations maintain reasonable precision."""
        model = CalculatorModel()
        result = model.add(0.1, 0.2)
        # Account for floating-point precision
        assert abs(result - 0.3) < 1e-10
    def test_zero_operands_with_operations(self):
        """Test operations with zero as operand."""
        model = CalculatorModel()
        assert model.add(0, 5) == 5
        assert model.subtract(5, 0) == 5
        assert model.multiply(5, 0) == 0
        assert model.add(0, 0) == 0
