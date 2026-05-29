"""Tests for calculator GUI implementation."""
import pytest
from calculator.gui import CalculatorApp
from calculator.model import calculate, DivisionByZeroError, InvalidOperationError

def test_calculatorapp_class_available():
    """Test that the CalculatorApp class is available and importable."""
    assert CalculatorApp is not None
    assert hasattr(CalculatorApp, '__init__')
    assert hasattr(CalculatorApp, 'run')
    assert hasattr(CalculatorApp, 'evaluate')

def test_button_click_sequence_simple():
    """Test GUI launches, processes button events, updates display, handles division by zero."""
    # Create GUI instance (headless - no real display needed for state testing)
    app = CalculatorApp()
    
    # Test 1: GUI launched successfully (runtime_started)
    assert app.window is not None
    assert app.current_input == ""
    assert app.first_operand is None
    assert app.pending_operation is None
    
    # Test 2: Process digit button clicks
    app.add_to_expression(5)
    assert app.current_input == "5"
    assert app.waiting_for_operand is False
    
    app.add_to_expression(3)
    assert app.current_input == "53"
    
    # Test 3: Process operator button
    app.append_operator('+')
    assert app.first_operand == 53.0
    assert app.pending_operation == '+'
    assert app.waiting_for_operand is True
    
    # Test 4: Process more digits and evaluate
    app.add_to_expression(2)
    app.add_to_expression(0)
    assert app.current_input == "20"
    
    # Test 5: Evaluate expression and verify display updates
    app.evaluate()
    assert app.current_input == "73"  # 53 + 20 = 73
    assert app.first_operand == 73.0
    assert app.pending_operation is None
    assert app.waiting_for_operand is True
    
    # Test 6: Test clear functionality
    app.clear()
    assert app.current_input == ""
    assert app.first_operand is None
    assert app.pending_operation is None
    assert app.waiting_for_operand is False
    
    # Test 7: Test another operation sequence (multiplication)
    app.add_to_expression(1)
    app.add_to_expression(0)
    app.append_operator('*')
    assert app.first_operand == 10.0
    assert app.pending_operation == '*'
    
    app.add_to_expression(5)
    app.evaluate()
    assert app.current_input == "50"  # 10 * 5 = 50
    
    # Test 8: Test division by zero error handling (invalid_input_rejected)
    app.clear()
    app.add_to_expression(1)
    app.add_to_expression(0)
    app.append_operator('/')
    app.add_to_expression(0)
    # Evaluate should handle division by zero gracefully
    # (The GUI shows error message and clears state)
    app.evaluate()
    # After error, state should be cleared
    assert app.current_input == ""
    assert app.first_operand is None
    assert app.pending_operation is None
    
    # Test 9: Test delete/backspace functionality
    app.clear()
    app.add_to_expression(1)
    app.add_to_expression(2)
    app.add_to_expression(3)
    assert app.current_input == "123"
    
    app.delete()
    assert app.current_input == "12"
    
    app.delete()
    assert app.current_input == "1"
    
    # Test 10: Test decimal point handling
    app.clear()
    app.add_to_expression(1)
    app.add_to_expression(5)
    app.add_to_expression('.')
    app.add_to_expression(5)
    assert app.current_input == "15.5"
    
    # Test that duplicate decimal points are rejected
    app.add_to_expression('.')
    app.add_to_expression(7)
    assert app.current_input == "15.57"  # Should not add another decimal point
