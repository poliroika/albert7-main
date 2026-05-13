# Code Style

## Formatting

We use [Ruff](https://docs.astral.sh/ruff/) for formatting:

```bash
uv run ruff format src/ tests/
```

- Line length: 120 characters
- Target Python: 3.12+
- Use double quotes for strings

## Linting

```bash
uv run ruff check --fix src/ tests/
```

## Type Hints

Required for public APIs:

```python
from typing import Optional

def process_input(
    prompt: str,
    max_tokens: Optional[int] = None,
) -> str:
    """Process the input prompt."""
    return prompt
```

## Docstrings

Use Google style:

```python
def calculate_metrics(data: dict) -> dict:
    """Calculate metrics from the provided data.

    Args:
        data: Input data dictionary.

    Returns:
        Dictionary containing calculated metrics.

    Raises:
        ValueError: If data is invalid.
    """
    pass
```

## Naming

- Functions: `snake_case`
- Classes: `PascalCase`
- Constants: `UPPER_SNAKE_CASE`
- Private: `_leading_underscore`

## Imports

Group imports:

```python
# Standard library
import os
from pathlib import Path

# Third-party
import torch
from pydantic import BaseModel

# Local
from gmas.core import AgentProfile
```

## Pre-commit

Hooks run automatically:

- Ruff formatting
- Ruff linting
- Type checking

Install hooks:

```bash
uv run prek install
```
