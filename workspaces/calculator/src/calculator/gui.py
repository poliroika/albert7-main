"""Tkinter-based GUI calculator application.
Integrates with the calculate() model for arithmetic operations."""
import tkinter as tk
from tkinter import messagebox
from calculator.model import calculate, DivisionByZeroError, InvalidOperationError

class CalculatorApp:
    """Main calculator application with Tkinter GUI."""
    
    def __init__(self, headless=False):
        """Initialize the calculator application.
        
        Args:
            headless: If True, creates GUI without Tk window for testing.
        """
        # Expression tracking (initialize before window creation)
        self.current_input = ""
        self.pending_operation = None
        self.first_operand = None
        self.waiting_for_operand = False
        self.window = None
        self.label = None
        self.total_label = None
        
        # Skip window/UI creation in headless mode
        if not headless:
            self.window = tk.Tk()
            self.window.geometry("375x667")
            self.window.resizable(False, False)
            self.window.title("Calculator")
            # Color scheme
            self.OFF_WHITE = "#F8FAFF"
            self.WHITE = "#FFFFFF"
            self.LIGHT_BLUE = "#CCEDFF"
            self.LIGHT_GRAY = "#F5F5F5"
            self.LABEL_COLOR = "#25265E"
            self.window.configure(bg=self.OFF_WHITE)
            # Create UI components
            self.display_frame = self.create_display_frame()
            self.total_label, self.label = self.create_display_labels()
            self.digits = {
                7: (1, 1), 8: (1, 2), 9: (1, 3),
                4: (2, 1), 5: (2, 2), 6: (2, 3),
                1: (3, 1), 2: (3, 2), 3: (3, 3),
                0: (4, 2), '.': (4, 1)
            }
            self.operations = {
                '/': "\u00F7",
                '*': "\u00D7",
                '-': "-",
                '+': "+"
            }
            self.buttons_frame = self.create_buttons_frame()
            self.create_digit_buttons()
            self.create_operator_buttons()
            self.create_special_buttons()
            self.bind_keys()
    
    def create_display_frame(self):
        """Create the frame that holds the display labels."""
        frame = tk.Frame(self.window, bg=self.LIGHT_GRAY)
        frame.pack(expand=True, fill="both")
        return frame
    
    def create_display_labels(self):
        """Create the total and current expression display labels."""
        total_label = tk.Label(
            self.display_frame,
            text=self.current_input,
            anchor=tk.E,
            bg=self.LIGHT_GRAY,
            fg=self.LABEL_COLOR,
            padx=24,
            font=("Arial", 20)
        )
        total_label.pack(expand=True, fill="both")
        label = tk.Label(
            self.display_frame,
            text=self.current_input,
            anchor=tk.E,
            bg=self.LIGHT_GRAY,
            fg=self.LABEL_COLOR,
            padx=24,
            font=("Arial", 40, "bold")
        )
        label.pack(expand=True, fill="both")
        return total_label, label
    
    def create_buttons_frame(self):
        """Create the frame that holds all calculator buttons."""
        frame = tk.Frame(self.window, bg=self.OFF_WHITE)
        frame.pack(expand=True, fill="both")
        return frame
    
    def create_digit_buttons(self):
        """Create digit buttons (0-9 and decimal point)."""
        for digit, grid_info in self.digits.items():
            button = tk.Button(
                self.buttons_frame,
                text=str(digit),
                bg=self.WHITE,
                fg=self.LABEL_COLOR,
                font=("Arial", 24, "bold"),
                borderwidth=0,
                command=lambda x=digit: self.add_to_expression(x)
            )
            button.grid(row=grid_info[0], column=grid_info[1], sticky=tk.NSEW, padx=5, pady=5)
    
    def create_operator_buttons(self):
        """Create operator buttons (+, -, *, /)."""
        i = 0
        for operator, symbol in self.operations.items():
            button = tk.Button(
                self.buttons_frame,
                text=symbol,
                bg=self.LIGHT_BLUE,
                fg=self.LABEL_COLOR,
                font=("Arial", 20),
                borderwidth=0,
                command=lambda x=operator: self.append_operator(x)
            )
            button.grid(row=i, column=4, sticky=tk.NSEW, padx=5, pady=5)
            i += 1
    
    def create_special_buttons(self):
        """Create special buttons (C, ⌫, =)."""
        self.create_clear_button()
        self.create_delete_button()
        self.create_equals_button()
    
    def create_clear_button(self):
        """Create the clear button."""
        button = tk.Button(
            self.buttons_frame,
            text="C",
            bg=self.LIGHT_BLUE,
            fg=self.LABEL_COLOR,
            font=("Arial", 20),
            borderwidth=0,
            command=self.clear
        )
        button.grid(row=0, column=0, sticky=tk.NSEW, padx=5, pady=5)
    
    def create_delete_button(self):
        """Create the delete/backspace button."""
        button = tk.Button(
            self.buttons_frame,
            text="⌫",
            bg=self.LIGHT_BLUE,
            fg=self.LABEL_COLOR,
            font=("Arial", 20),
            borderwidth=0,
            command=self.delete
        )
        button.grid(row=0, column=1, sticky=tk.NSEW, padx=5, pady=5)
    
    def create_equals_button(self):
        """Create the equals button."""
        button = tk.Button(
            self.buttons_frame,
            text="=",
            bg=self.LIGHT_BLUE,
            fg=self.LABEL_COLOR,
            font=("Arial", 20),
            borderwidth=0,
            command=self.evaluate
        )
        button.grid(row=3, column=4, sticky=tk.NSEW, padx=5, pady=5)
    
    def bind_keys(self):
        """Bind keyboard keys to calculator functions."""
        self.window.bind("<Return>", lambda event: self.evaluate())
        self.window.bind("<BackSpace>", lambda event: self.delete())
        self.window.bind("<c>", lambda event: self.clear())
        self.window.bind("<C>", lambda event: self.clear())
        for key in self.digits:
            self.window.bind(str(key), lambda event, digit=key: self.add_to_expression(digit))
        for operator in self.operations:
            self.window.bind(operator, lambda event, op=operator: self.append_operator(op))
    
    def update_label(self):
        """Update the display label with current input."""
        if self.label:
            self.label.config(text=self.current_input[:11])
    
    def update_total_label(self):
        """Update the total expression label."""
        if self.total_label:
            expression = self.current_input
            for operator, symbol in self.operations.items():
                expression = expression.replace(operator, symbol)
            self.total_label.config(text=expression)
    
    def add_to_expression(self, value):
        """Add a digit or decimal point to the current expression."""
        if self.waiting_for_operand:
            self.current_input = ""
            self.waiting_for_operand = False
        if value == '.' and '.' in self.current_input:
            return
        self.current_input += str(value)
        self.update_label()
        self.update_total_label()
    
    def append_operator(self, operator):
        """Append an operator to the expression."""
        if not self.current_input and not self.first_operand:
            return
        if self.first_operand is None:
            try:
                self.first_operand = float(self.current_input)
                self.pending_operation = operator
                self.current_input = ""
                self.waiting_for_operand = True
            except ValueError:
                if self.window:
                    messagebox.showerror("Error", "Invalid input")
        else:
            if self.current_input:
                self.evaluate()
                self.pending_operation = operator
                self.waiting_for_operand = True
    
    def evaluate(self):
        """Evaluate the current expression."""
        if self.current_input == "" or self.pending_operation is None:
            return
        if self.waiting_for_operand:
            self.current_input = str(self.first_operand)
        try:
            second_operand = float(self.current_input)
            operation_map = {
                '+': 'add',
                '-': 'subtract',
                '*': 'multiply',
                '/': 'divide'
            }
            result = calculate(
                operation_map[self.pending_operation],
                self.first_operand,
                second_operand
            )
            # Display result
            if result.is_integer():
                self.current_input = str(int(result))
            else:
                self.current_input = str(result)
            self.first_operand = result
            self.pending_operation = None
            self.waiting_for_operand = True
            self.update_label()
            self.update_total_label()
        except DivisionByZeroError:
            if self.window:
                messagebox.showerror("Error", "Cannot divide by zero")
            self.clear()
        except (InvalidOperationError, ValueError) as e:
            if self.window:
                messagebox.showerror("Error", str(e))
            self.clear()
    
    def delete(self):
        """Delete the last character from the current input."""
        if self.waiting_for_operand:
            return
        if len(self.current_input) > 0:
            self.current_input = self.current_input[:-1]
            self.update_label()
            self.update_total_label()
    
    def clear(self):
        """Clear the calculator state."""
        self.current_input = ""
        self.first_operand = None
        self.pending_operation = None
        self.waiting_for_operand = False
        self.update_label()
        self.update_total_label()
    
    def run(self):
        """Start the main event loop."""
        self.window.mainloop()

def main():
    """Entry point for running the calculator application."""
    app = CalculatorApp()
    app.run()

if __name__ == "__main__":
    main()
