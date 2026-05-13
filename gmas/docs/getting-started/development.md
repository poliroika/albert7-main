# Development Setup

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Git

## Setup

1. Fork and clone:

```bash
git clone https://github.com/YOUR_USERNAME/gmas.git
cd gmas
git remote add upstream https://github.com/frontier-ai-next/gmas.git
```

2. Install dependencies:

```bash
uv sync
```

3. Install hooks:

```bash
uv run prek install
```

## Development Commands

### Code Quality

```bash
# Lint and format
uv run ruff check --fix src/ tests/
uv run ruff format src/ tests/

# Type check
uv run ty check src tests --ignore unresolved-import
```

### Testing

```bash
# Run all tests
uv run pytest tests/ -v

# Specific test file
uv run pytest tests/test_graph.py -v

# With coverage
uv run pytest tests/ -v --cov=src --cov-report=term
```

### Multi-version Testing

```bash
# Run all environments
tox

# Specific environment
tox -e py312
tox -e lint
```

## Branching

```bash
git checkout main
git pull upstream main
git checkout -b feat/42-feature-name
```

## Commit Messages

Follow Conventional Commits:

```
feat(scope): description
fix(scope): description
docs: description
```

## Project Structure

```
src/gmas/
├── core/       # Graph, agents, schemas
├── execution/  # Runner, scheduler, streaming
├── builder/    # Graph construction
├── tools/      # Agent tools
├── callbacks/  # Event handlers
└── utils/      # Utilities
```
