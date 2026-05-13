# Contributing to gMAS

First off, thank you for considering contributing to gMAS! Every contribution helps make this project better for the entire community. Whether you're fixing a bug, adding a feature, improving documentation, or answering questions in issues, your time and effort are valued.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Ways to Contribute](#ways-to-contribute)
- [Getting Started](#getting-started)
  - [Prerequisites](#prerequisites)
  - [Development Setup](#development-setup)
- [Development Workflow](#development-workflow)
  - [Branching Strategy](#branching-strategy)
  - [Commit Messages](#commit-messages)
  - [Code Style](#code-style)
  - [Type Checking](#type-checking)
  - [Running Tests](#running-tests)
- [Submitting Changes](#submitting-changes)
  - [Pull Request Process](#pull-request-process)
  - [Pull Request Checklist](#pull-request-checklist)
  - [Code Review](#code-review)
- [Reporting Bugs](#reporting-bugs)
- [Requesting Features](#requesting-features)
- [Project Structure](#project-structure)
- [AI-Assisted Contributions](#ai-assisted-contributions)

## Code of Conduct

By participating in this project, you agree to maintain a respectful and inclusive environment. Be kind, constructive, and professional in all interactions. Harassment, discrimination, and disruptive behavior will not be tolerated.

## Ways to Contribute

There are many ways to contribute to gMAS, and not all of them involve writing code:

- **Bug Reports** -- found something broken? [Open an issue](#reporting-bugs)
- **Feature Requests** -- have an idea? [Let us know](#requesting-features)
- **Code Contributions** -- fix a bug or implement a new feature
- **Documentation** -- improve existing docs, add examples, fix typos
- **Examples** -- add new usage examples to the `examples/` directory
- **Tests** -- increase test coverage or improve existing tests
- **Answering Questions** -- help other users in issues and discussions

## Getting Started

### Prerequisites

- **Python 3.12+**
- **[uv](https://docs.astral.sh/uv/)** -- fast Python package manager
- **Git** with pre-commit support

### Development Setup

1. **Fork and clone the repository:**

   ```bash
   git clone https://github.com/frontier-ai-next/gmas.git
   cd gmas
   ```

2. **Add the upstream remote:**

   ```bash
   git remote add upstream https://github.com/frontier-ai-next/gmas.git
   ```

3. **Install dependencies:**

   ```bash
   uv sync
   ```

   > Dev dependencies (pytest, ruff, prek, etc.) are installed by default. Optional extras (`embeddings`, `pyg`, `viz`, etc.) should be installed individually as needed -- they may conflict with each other.

4. **Install prek hooks:**

   ```bash
   uv run prek install
   ```

5. **Verify everything works:**

   ```bash
   uv run pytest tests/ -v --co
   ```

## Development Workflow

### Branching Strategy

Always create a new branch from `main` for your work. Never commit directly to `main`.

```bash
git checkout main
git pull upstream main
git checkout -b <type>/<issue>-<short-description>
```

We follow the [Conventional Branch](https://conventional-branch.github.io) naming convention: `type/issue-n/description`, e.g. `feat/issue-42/gnn-attention`, `fix/issue-108/scheduler-deadlock`, `docs/issue-79/prepare-contribute`.

### Commit Messages

This project enforces **[Conventional Commits](https://www.conventionalcommits.org/)**. Each commit message must follow this format:

```text
<type>(<optional scope>): <description>

[optional body]

[optional footer(s)]
```

**Types:**

| Type       | When to use                                |
|------------|--------------------------------------------|
| `feat`     | A new feature                              |
| `fix`      | A bug fix                                  |
| `docs`     | Documentation only changes                 |
| `style`    | Formatting, missing semicolons, etc.       |
| `refactor` | Code change that neither fixes nor adds    |
| `test`     | Adding or correcting tests                 |
| `chore`    | Maintenance tasks, CI, dependencies        |
| `perf`     | Performance improvements                   |

**Examples:**

```text
feat(scheduler): add adaptive scheduling with SCC detection
fix(runner): prevent deadlock when nodes have circular dependencies
docs: update QUICKSTART with streaming examples
test(callbacks): add integration tests for MetricsHandler
```

### Code Style

We use **[Ruff](https://docs.astral.sh/ruff/)** for both linting and formatting.

- **Line length:** 120 characters
- **Target:** Python 3.12+
- **Docstrings:** Google style

Format your code before committing:

```bash
uv run ruff check --fix src/ tests/
uv run ruff format src/ tests/
```

Or let pre-commit hooks handle it automatically.

### Type Checking

We use **[ty](https://github.com/astral-sh/ty)** for type checking:

```bash
uv run ty check src tests --ignore unresolved-import
```

### Running Tests

**Run a specific test file:**

```bash
uv run pytest tests/test_graph.py -v
```

**Run tests matching a keyword:**

```bash
uv run pytest tests/ -k "scheduler" -v
```

**Run with coverage:**

```bash
uv run pytest tests/ -v --cov=src --cov-report=term --cov-report=html
```

**Guidelines:**

- All new features must include tests
- All bug fixes should include a regression test
- Tests use `pytest` with `pytest-asyncio` (asyncio_mode = "auto")
- Source code is on `PYTHONPATH` via `pythonpath = ["src"]` in pytest config
- Aim to maintain or improve the current coverage level

## Submitting Changes

### Pull Request Process

We accept contributions via [pull requests from forks](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/proposing-changes-to-your-work-with-pull-requests/creating-a-pull-request-from-a-fork).

1. **Fork the repository** on GitHub (if you haven't already).

2. **Sync with upstream** before pushing:

   ```bash
   git fetch upstream
   git rebase upstream/main
   ```

3. **Push your branch to your fork:**

   ```bash
   git push origin <your-branch>
   ```

4. **Open a Pull Request** from your fork targeting `main` on the upstream repository.

5. **Fill out the PR description** with:
   - What the change does and why
   - Link to the related issue (e.g., `Closes #42`)
   - Any breaking changes or migration steps

6. **Ensure CI passes** -- the pipeline runs linting, type checks, and the full test suite.

7. **Respond to review feedback** -- push additional commits to your branch as needed.

### Pull Request Checklist

Before submitting, verify:

- [ ] My code follows the project's code style (Ruff passes)
- [ ] I have added tests for my changes
- [ ] All existing tests pass (`uv run pytest tests/ -v`)
- [ ] Type checking passes (`uv run ty check src tests --ignore unresolved-import`)
- [ ] Prek hooks pass (`uv run prek run`)
- [ ] My commit messages follow Conventional Commits
- [ ] I have updated documentation if applicable
- [ ] I have not added any large binary files

### Code Review

- All pull requests require at least one approving review
- Reviewers may request changes -- this is a normal part of the process
- Keep discussions focused and constructive
- If your PR hasn't received a review within a few days, feel free to leave a polite comment requesting one

## Reporting Bugs

When filing a bug report, include:

1. **Summary** -- a clear, concise description of the bug
2. **Environment:**
   - OS and version
   - Python version (`python --version`)
   - gMAS version
   - Relevant dependency versions (rustworkx, torch, etc.)
3. **Steps to reproduce** -- a minimal code example that triggers the bug
4. **Expected behavior** -- what you expected to happen
5. **Actual behavior** -- what actually happened (include the full traceback)
6. **Additional context** -- logs, screenshots, or anything else that helps

A good bug report is one someone can reproduce in under a minute.

## Requesting Features

Feature requests are welcome! When submitting one, please include:

1. **Problem statement** -- what problem does this solve?
2. **Proposed solution** -- how do you envision it working?
3. **Alternatives considered** -- any other approaches you thought about?
4. **Use case** -- a concrete example of how this would be used

## Project Structure

```text
gmas/
├── src/                    # Main source code
│   ├── core/               # Core: agents, graph, schema, GNN, metrics
│   ├── execution/          # Runner, scheduler, streaming, budget
│   ├── builder/            # Graph construction and auto-building
│   ├── callbacks/          # Callback system and handlers
│   ├── tools/              # Agent tools (shell, web search, MCP, etc.)
│   ├── config/             # Settings and logging configuration
│   └── utils/              # Async helpers, memory, state storage
├── tests/                  # Test suite (pytest)
├── examples/               # Usage examples and notebooks
├── benchmarks/             # Performance benchmarks
├── docs/                   # Sphinx documentation source
├── pyproject.toml          # Project metadata and tool configuration
├── .ruff.toml              # Ruff linter configuration
├── .pre-commit-config.yaml # Pre-commit hooks
└── .gitlab-ci.yml          # CI/CD pipeline
```

When adding new functionality, place it in the appropriate module. If you're unsure where something belongs, open an issue to discuss before writing code.

## AI-Assisted Contributions

We welcome contributions that use AI-assisted tools, with the following expectations:

- **You are responsible** for every line of code you submit -- review it thoroughly
- **Run all tests** and verify the changes work end-to-end
- **Disclose AI usage** in your PR description if a significant portion was AI-generated
- **Do not submit** low-effort, auto-generated PRs that create review burden without clear value
- **Understand what you submit** -- be prepared to explain and defend any part of your contribution

---

Thank you for helping make gMAS better! If you have any questions about the contribution process, don't hesitate to open an issue.
