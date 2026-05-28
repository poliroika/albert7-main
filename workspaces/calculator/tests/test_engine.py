"""Tests for calculator engine."""
import pytest
from calculator.engine import (
    add,
    subtract,
    multiply,
    divide,
    CalculatorError,
    DivisionByZeroError,
    InvalidOperationError,
)
class TestAdd:
    """Test addition operation."""
    def test_add_positive_numbers(self):
        assert add(2, 3) == 5
        assert add(10, 15) == 25
    def test_add_negative_numbers(self):
        assert add(-2, -3) == -5
        assert add(-10, 15) == 5
    def test_add_zero(self):
        assert add(0, 0) == 0
        assert add(5, 0) == 5
        assert add(0, 5) == 5
    def test_add_decimals(self):
        assert add(1.5, 2.5) == 4.0
        assert add(-1.5, 2.5) == 1.0
    def test_add_distinct_inputs_distinct_outputs(self):
        # Different inputs should produce different outputs
        assert add(1, 2) != add(3, 4)
        assert add(1, 2) != add(2, 1)
        assert add(5, 5) != add(10, 0)
class TestSubtract:
    """Test subtraction operation."""
    def test_subtract_positive_numbers(self):
        assert subtract(5, 3) == 2
        assert subtract(10, 15) == -5
    def test_subtract_negative_numbers(self):
        assert subtract(-5, -3) == -2
        assert subtract(-10, 15) == -25
    def test_subtract_zero(self):
        assert subtract(0, 0) == 0
        assert subtract(5, 0) == 5
        assert subtract(0, 5) == -5
    def test_subtract_decimals(self):
        assert subtract(5.5, 2.5) == 3.0
        assert subtract(2.5, 5.5) == -3.0
    def test_subtract_distinct_inputs_distinct_outputs(self):
        assert subtract(5, 3) != subtract(3, 5)
        assert subtract(10, 5) != subtract(5, 10)
        assert subtract(0, 5) != subtract(5, 0)
class TestMultiply:
    """Test multiplication operation."""
    def test_multiply_positive_numbers(self):
        assert multiply(3, 4) == 12
        assert multiply(10, 15) == 150
    def test_multiply_negative_numbers(self):
        assert multiply(-2, 3) == -6
        assert multiply(-2, -3) == 6
    def test_multiply_zero(self):
        assert multiply(0, 5) == 0
        assert multiply(5, 0) == 0
        assert multiply(0, 0) == 0
    def test_multiply_one(self):
        assert multiply(5, 1) == 5
        assert multiply(1, 5) == 5
    def test_multiply_decimals(self):
        assert multiply(2.5, 4) == 10.0
        assert multiply(1.5, 2.0) == 3.0
    def test_multiply_distinct_inputs_distinct_outputs(self):
        assert multiply(2, 3) != multiply(3, 4)
        assert multiply(2, 3) != multiply(3, 2)
        assert multiply(5, 0) != multiply(5, 1)
class TestDivide:
    """Test division operation."""
    def test_divide_positive_numbers(self):
        assert divide(10, 2) == 5
        assert divide(15, 3) == 5
        assert divide(7, 2) == 3.5
    def test_divide_negative_numbers(self):
        assert divide(-10, 2) == -5
        assert divide(10, -2) == -5
        assert divide(-10, -2) == 5
    def test_divide_decimals(self):
        assert divide(2.5, 0.5) == 5.0
        assert divide(1.5, 3) == 0.5
    def test_divide_distinct_inputs_distinct_outputs(self):
        assert divide(10, 2) != divide(20, 2)
        assert divide(10, 2) != divide(10, 5)
        assert divide(0, 5) != divide(5, 1)
    def test_divide_by_zero_raises_error(self):
        """Negative case: division by zero should raise DivisionByZeroError."""
        with pytest.raises(DivisionByZeroError, match="Division by zero"):
            divide(10, 0)
        with pytest.raises(DivisionByZeroError):
            divide(0, 0)
        with pytest.raises(DivisionByZeroError):
            divide(-5, 0)
class TestErrorHandling:
    """Test error handling and invalid inputs."""
    def test_inf_raises_invalid_operation_error(self):
        """Negative case: infinite values should raise InvalidOperationError."""
        import math
        with pytest.raises(InvalidOperationError):
            add(math.inf, 5)
        with pytest.raises(InvalidOperationError):
            subtract(5, math.inf)
        with pytest.raises(InvalidOperationError):
            multiply(math.inf, 0)
        with pytest.raises(InvalidOperationError):
            divide(math.inf, 2)
    def test_neg_inf_raises_invalid_operation_error(self):
        """Negative case: negative infinite values should raise InvalidOperationError."""
        import math
        with pytest.raises(InvalidOperationError):
            add(-math.inf, 5)
        with pytest.raises(InvalidOperationError):
            subtract(5, -math.inf)
        with pytest.raises(InvalidOperationError):
           multiply(-math.inf, 0)
    def test_nan_raises_invalid_operation_error(self):
        """Negative case: NaN values should raise InvalidOperationError."""
        import math
        with pytest.raises(InvalidOperationError):
            add(math.nan, 5)
        with pytest.raises(InvalidOperationError):
            subtract(5, math.nan)
        with pytest.raises(InvalidOperationError):
            multiply(math.nan, 0)
        with pytest.raises(InvalidOperationError):
            divide(math.nan, 2)
    def test_calculator_error_hierarchy(self):
        """Verify error class hierarchy."""
        assert issubclass(DivisionByZeroError, CalculatorError)
        assert issubclass(InvalidOperationError, CalculatorError)
        # Actual division by zero should raise the specific subclass
        with pytest.raises(CalculatorError):
            divide(10, 0)
        # Invalid operations should raise the specific subclass
        import math
        with pytest.raises(CalculatorError):
            add(math.inf, 5)
class TestPrecision:
    """Test precision and edge cases."""
    def test_large_numbers(self):
        assert add(1e10, 1e10) == 2e10
        assert multiply(1e5, 1e5) == 1e10
    def test_small_decimals(self):
        result = add(0.1, 0.2)
        # Due to floating point precision, use approximate comparison
        assert abs(result - 0.3) < 1e-10
    def test_identity_properties(self):
        # Additive identity
        assert abs(add(5.5, 0) - 5.5) < 1e-10
        # Multiplicative identity
        assert abs(multiply(5.5, 1) - 5.5) < 1e-10
        # Multiplication by zero
        assert multiply(5.5, 0) == 0
