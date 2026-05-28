"""
Calculator model core - arithmetic operations with error handling.
"""
from typing import Union, Optional
from enum import Enum

class OperationType(Enum):
    """Supported calculator operations."""
    ADD = "+"
    SUBTRACT = "-"
    MULTIPLY = "×"
    DIVIDE = "÷"

class CalculatorError(Exception):
    """Base exception for calculator errors."""
    pass

class DivisionByZeroError(CalculatorError):
    """Raised when attempting division by zero."""
    pass

class InvalidInputError(CalculatorError):
    """Raised when input is invalid (e.g., non-numeric)."""
    pass

class CalculatorModel:
    """
    Core calculator model handling arithmetic operations.
    This class implements the mathematical operations with proper
    error handling for edge cases like division by zero and invalid inputs.
    
    Chaining behavior:
    - First apply_operation: sets accumulator directly
    - Subsequent apply_operation: executes the PREVIOUS pending operation with NEW operand
    - calculate(): executes the LAST stored operation with given operand
    """
    def __init__(self):
        """Initialize a new calculator model."""
        self.accumulator: float = 0.0
        self.pending_operation: Optional[OperationType] = None
        self.new_operand: Optional[float] = None
        self.history: list[tuple[OperationType, float]] = []

    def add(self, a: float, b: float) -> float:
        """Add two numbers."""
        return a + b

    def subtract(self, a: float, b: float) -> float:
        """Subtract b from a."""
        return a - b

    def multiply(self, a: float, b: float) -> float:
        """Multiply two numbers."""
        return a * b

    def divide(self, a: float, b: float) -> float:
        """
        Divide a by b.
        Args:
            a: Dividend
            b: Divisor
        Returns:
            Result of division
        Raises:
            DivisionByZeroError: If b is zero
        """
        if b == 0:
            raise DivisionByZeroError("Cannot divide by zero")
        return a / b

    def execute_operation(self, a: float, b: float, operation: OperationType) -> float:
        """
        Execute a binary arithmetic operation.
        Args:
            a: First operand
            b: Second operand
            operation: The operation to perform
        Returns:
            Result of the operation
        Raises:
            DivisionByZeroError: If division by zero is attempted
        """
        if operation == OperationType.ADD:
            return self.add(a, b)
        elif operation == OperationType.SUBTRACT:
            return self.subtract(a, b)
        elif operation == OperationType.MULTIPLY:
            return self.multiply(a, b)
        elif operation == OperationType.DIVIDE:
            return self.divide(a, b)
        else:
            raise InvalidInputError(f"Unsupported operation: {operation}")

    def parse_operand(self, value: str) -> float:
        """
        Parse a string to a numeric operand.
        Args:
            value: String representation of a number
        Returns:
            Parsed float value
        Raises:
            InvalidInputError: If value cannot be parsed as a number
        """
        try:
            return float(value)
        except (ValueError, TypeError) as e:
            raise InvalidInputError(f"Invalid numeric value: {value}") from e

    def apply_operation(self, operation: OperationType, operand: float) -> None:
        """
        Store operation and operand. Supports operation chaining.
        Args:
            operation: The operation to perform
            operand: The operand for this operation
        """
        if self.pending_operation is None and self.accumulator == 0.0:
            # First call: set accumulator directly
            self.accumulator = operand
        else:
            # Chaining: execute the PREVIOUS pending operation with the NEW operand
            # This allows operations like: ADD(5), MULTIPLY(2), calculate(3)
            # which should compute: ((5+2)*3) = 21
            self.accumulator = self.execute_operation(
                self.accumulator,
                operand,
                self.pending_operation
            )
        
        # Store the NEW operation for later use by calculate() or next apply_operation()
        self.pending_operation = operation
        self.new_operand = operand
        self.history.append((operation, operand))

    def calculate(self, operand: Optional[float] = None) -> float:
        """
        Execute the pending operation with the given operand.
        If no operand is given, use 0.0.
        Args:
            operand: The operand to combine with accumulator (optional)
        Returns:
            Result of the calculation
        Raises:
            DivisionByZeroError: If division by zero is attempted
        """
        if operand is None:
            operand = 0.0
        
        if self.pending_operation is None:
            self.accumulator = operand
            return self.accumulator
        
        result = self.execute_operation(self.accumulator, operand, self.pending_operation)
        self.accumulator = result
        self.pending_operation = None
        self.new_operand = None
        return result

    def clear(self) -> None:
        """Reset the calculator state."""
        self.accumulator = 0.0
        self.pending_operation = None
        self.new_operand = None
        self.history = []

    def has_pending_operation(self) -> bool:
        """Check if there's a pending operation."""
        return self.pending_operation is not None

# Convenience functions for simple calculations
def calculate(a: float, b: float, operation: OperationType) -> float:
    """
    Perform a simple calculation without state management.
    Args:
        a: First operand
        b: Second operand
        operation: The operation to perform
    Returns:
        Result of the operation
    """
    model = CalculatorModel()
    return model.execute_operation(a, b, operation)
