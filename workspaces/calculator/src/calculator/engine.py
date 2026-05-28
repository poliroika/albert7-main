"""Calculator engine with arithmetic operations and error handling."""
class CalculatorError(Exception):
    """Base exception for calculator errors."""
    pass
class DivisionByZeroError(CalculatorError):
    """Raised when attempting to divide by zero."""
    pass
class InvalidOperationError(CalculatorError):
    """Raised for invalid operations."""
    pass
def add(a: float, b: float) -> float:
    """
    Add two numbers.
    Args:
        a: First operand
        b: Second operand
    Returns:
        Sum of a and b
    Raises:
        InvalidOperationError: If inputs are not finite numbers
    """
    _validate_operands(a, b)
    return a + b
def subtract(a: float, b: float) -> float:
    """
    Subtract b from a.
    Args:
        a: First operand
        b: Second operand
    Returns:
        Difference of a and b
    Raises:
        InvalidOperationError: If inputs are not finite numbers
    """
    _validate_operands(a, b)
    return a - b
def multiply(a: float, b: float) -> float:
    """
    Multiply two numbers.
    Args:
        a: First operand
        b: Second operand
    Returns:
        Product of a and b
    Raises:
        InvalidOperationError: If inputs are not finite numbers
    """
    _validate_operands(a, b)
    return a * b
def divide(a: float, b: float) -> float:
    """
    Divide a by b.
    Args:
        a: Dividend
        b: Divisor
    Returns:
        Quotient of a and b
    Raises:
        DivisionByZeroError: If b is zero
        InvalidOperationError: If inputs are not finite numbers
    """
    _validate_operands(a, b)
    if b == 0:
        raise DivisionByZeroError("Division by zero is not allowed")
    return a / b
def _validate_operands(a: float, b: float) -> None:
    """
    Validate that operands are finite numbers.
    Args:
        a: First operand
        b: Second operand
    Raises:
        InvalidOperationError: If either operand is not finite
    """
    import math
    if not (math.isfinite(a) and math.isfinite(b)):
        raise InvalidOperationError("Operands must be finite numbers")
