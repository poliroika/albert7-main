"""
Calculator package for a simple arithmetic application.
"""
__version__ = "0.1.0"

# Export the calculator functions for easy import
from calculator.model import calculate, CalculatorError, DivisionByZeroError, InvalidOperationError
from calculator.gui import CalculatorApp, main

__all__ = ['calculate', 'CalculatorError', 'DivisionByZeroError', 'InvalidOperationError', 'CalculatorApp', 'main']
