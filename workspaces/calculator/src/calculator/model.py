"""
Core calculator model implementing arithmetic operations with error handling.
"""
class CalculatorError(Exception):
    """Base exception for calculator errors."""
    pass
class DivisionByZeroError(CalculatorError):
    """Raised when division by zero is attempted."""
    pass
class InvalidOperationError(CalculatorError):
    """Raised when an invalid operation is requested."""
    pass
def calculate(operation: str, a: float, b: float) -> float:
    """
    Perform arithmetic operations on two numbers.
    Args:
        operation: The operation to perform ('add', 'subtract', 'multiply', 'divide')
        a: First operand
        b: Second operand
    Returns:
        The result of the arithmetic operation
    Raises:
        InvalidOperationError: If the operation is not recognized
        DivisionByZeroError: If division by zero is attempted
    """
    if operation == 'add':
        return a + b
    elif operation == 'subtract':
        return a - b
    elif operation == 'multiply':
        return a * b
    elif operation == 'divide':
        if b == 0:
            raise DivisionByZeroError("Cannot divide by zero")
        return a / b
    else:
        raise InvalidOperationError(f"Invalid operation: {operation}")