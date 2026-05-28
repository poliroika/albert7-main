"""Core calculator logic with arithmetic operations and error handling."""
class Calculator:
    """Simple calculator supporting basic arithmetic operations."""
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
        """Divide a by b.
        Args:
            a: Dividend
            b: Divisor
        Returns:
            Result of division
        Raises:
            ValueError: If b is zero
        """
        if b == 0:
            raise ValueError("Division by zero is not allowed")
        return a / b
    def calculate(self, operation: str, a: float, b: float) -> float:
        """Perform a calculation based on the operation string.
        Args:
            operation: One of '+', '-', '*', '/'
            a: First operand
            b: Second operand
        Returns:
            Result of calculation
        Raises:
            ValueError: If operation is invalid or division by zero
        """
        if operation == '+':
            return self.add(a, b)
        elif operation == '-':
            return self.subtract(a, b)
        elif operation == '*':
            return self.multiply(a, b)
        elif operation == '/':
            return self.divide(a, b)
        else:
            raise ValueError(f"Invalid operation: {operation}. Use +, -, *, or /")
