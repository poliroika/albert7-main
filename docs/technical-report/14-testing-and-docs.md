# Part 14: Testing and Documentation

[← Table of contents](README.md) · [← Part 13](13-operations.md) · [Next: Task Planner →](15-task-planner.md)

---

## 14.1 Running tests

From the repository root:

```bash
uv sync --extra dev
uv run pytest -q
```

`pyproject.toml` sets `testpaths` to `umbrella/tests` and `ouroboros/tests` and adds `pythonpath` for the `ouroboros` package. Always run from the repo root via `uv run pytest`; ad-hoc `pytest` from another working directory often fails imports.

---

## 14.2 Focused test subsets

When working on a subsystem, narrow the run:

=== "Phase machine and orchestration"

    ```bash
    uv run pytest -q umbrella/tests/test_phase_manifests_valid.py
    uv run pytest -q umbrella/tests/test_phase_runner.py
    uv run pytest -q umbrella/tests/test_phase_plan_mutation.py
    ```

=== "Permissions and watcher"

    ```bash
    uv run pytest -q umbrella/tests/test_permission_envelope.py
    uv run pytest -q umbrella/tests/test_watcher_envelope.py
    uv run pytest -q umbrella/tests/test_watcher_signals.py
    ```

=== "MemPalace"

    ```bash
    uv run pytest -q umbrella/tests/test_palace_facade.py
    uv run pytest -q umbrella/tests/test_palace_backend_filter.py
    ```

=== "Verification"

    ```bash
    uv run pytest -q umbrella/tests/test_verification.py
    uv run pytest -q umbrella/tests/test_app_ouroboros.py
    ```

=== "Web bridge"

    ```bash
    uv run pytest -q umbrella/tests/test_web_bridge_mcp.py
    ```

=== "Ouroboros"

    ```bash
    uv run pytest -q ouroboros/tests/
    uv run pytest -q ouroboros/tests/test_completion_gates.py
    ```

The exact file list evolves; search for `test_*.py` next to the code you change.

---

## 14.3 Notable test modules (post-refactor)

| Module | What it covers |
|--------|------------------|
| `umbrella/tests/test_phase_manifests_valid.py` | Every YAML under `umbrella/phases/manifests/` validates against `manifest.schema.json`. |
| `umbrella/tests/test_phase_runner.py` | PhaseRunner happy-path and integration with fake LLM / fixtures. |
| `umbrella/tests/test_permission_envelope.py` | Allow/deny rules, path globs, command regex, interaction with global rules. |
| `umbrella/tests/test_watcher_signals.py` | Watcher → Runner signal file protocol and handling. |
| `umbrella/tests/test_palace_facade.py` | `MemPalace` add/search/recall/link/walk/promote/expire semantics. |
| `ouroboros/tests/test_completion_gates.py` | Completion gates in `ouroboros/ouroboros/tools/control.py` (planner discovery, delivery contract hints, verify-evidence checks) without a full LLM loop. |

---

## 14.4 Pre-merge smoke matrix

| Area you touch | Minimum tests |
|----------------|---------------|
| `umbrella/phases/` | `test_phase_manifests_valid.py` |
| `umbrella/orchestrator/` | `test_phase_runner.py` + affected orchestrator tests |
| `umbrella/permissions/` | `test_permission_envelope.py` |
| `umbrella/memory/palace/` | `test_palace_facade.py` |
| `umbrella/verification/` | `test_verification.py` + `test_app_ouroboros.py` |
| `umbrella/web_bridge/` | web bridge tests under `umbrella/tests/` |
| `ouroboros/ouroboros/loop.py` or `tools/` | `ouroboros/tests/` |

---

## 14.5 Documentation maintenance

**Rule:** If operator- or developer-visible control-plane behavior changes, update the matching chapter under `docs/technical-report/NN-*.md` and any user-facing page in `docs/*.md`.

**When adding modules:** extend [README.md](README.md) table of contents and `mkdocs.yml` nav.

**Cross-links:**

- Phase manifests → [Part 6](06-umbrella-subsystems.md), [Part 11](11-configuration.md)
- Verification → [Part 9](09-verification.md)
- Ouroboros loop and tools → [Part 8](08-ouroboros-runtime.md), [Part 15](15-task-planner.md)

---

## 14.6 Building the docs site

```bash
pip install mkdocs-material
mkdocs serve
mkdocs build
```

Output goes to `public/`. GitLab CI job `pages` in `.gitlab-ci.yml` runs the same build for GitLab Pages.

---

## 14.7 Versioning

The technical report describes the **current** branch. Use git history for past decisions; avoid duplicating long narrative history in these files.

---

[↑ Back to technical report](README.md)
