# Calculator
A simple calculator application with a graphical user interface built with Tkinter.
## Features
- Basic arithmetic operations: addition, subtraction, multiplication, division
- Clear and delete operations
- Decimal number support
- Error handling for invalid operations (e.g., division by zero)
## Installation
```bash
pip install -e .
```
## Usage
### Command Line
```bash
python -m calculator.main
```
Or using the installed script:
```bash
calculator
```
### Running Tests
```bash
pytest
```
## Project Structure
```
calculator/
├── src/
│   └── calculator/
│       ├── __init__.py
│       ├── core.py          # Core calculator logic
│       ├── gui.py           # Tkinter GUI implementation
│       └── main.py          # Application entry point
├── tests/
│   ├── __init__.py
│   ├── conftest.py          # Pytest fixtures
│   ├── test_core.py         # Core logic tests
│   ├── test_gui.py          # GUI tests (headless)
│   ├── test_launcher.py     # Entry point tests
│   └── test_e2e.py          # End-to-end integration tests
├── pyproject.toml
└── README.md
```
## License
MIT License
