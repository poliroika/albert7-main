# Development Workflow

## Branch Strategy

Always branch from `main`:

```bash
git checkout main
git pull upstream main
git checkout -b feat/issue-42/create-proper-graph-roles
```

Branch naming: `type/issue-n/short-description-kebab-case`
- `feat/issue-n/` - New feature
- `fix/issue-n/` - Bug fix
- `docs/issue-n/` - Documentation
- `refactor/issue-n/` - Refactoring
- `test/issue-n/` - Tests

Examples:
- `feat/issue-42/add-gnn-routing`
- `fix/issue-108/prevent-deadlock`
- `docs/issue-79/update-contributing-guide`

## Commit Messages

Follow Conventional Commits:

```
type(scope): description

# Examples
feat(scheduler): add adaptive execution
fix(runner): prevent deadlock in parallel execution
docs: update installation guide
test(graph): add tests for dynamic topology
```

## Development Cycle

1. Create branch
2. Make changes
3. Run tests: `uv run pytest tests/ -v`
4. Run linting: `uv run ruff check src/`
5. Format code: `uv run ruff format src/`
6. Commit with conventional message
7. Push to fork
8. Create pull request

## Pull Request Process

1. Update your branch with upstream changes
2. Push to your fork
3. Open PR against `main`
4. Wait for CI checks
5. Address review feedback
6. Request review when ready

## CI/CD

The pipeline runs:
- Linting (ruff)
- Type checking (ty)
- Tests (pytest)
- Coverage reporting

Ensure all checks pass before requesting review.
