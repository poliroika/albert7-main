# Part 12: Harness vs Meta-Harness Artifacts

This chapter clarifies two different concepts that are easy to confuse, and documents what actually exists in this repository today.

## Runtime harness (`harness_run`)

**What:** A **tool** exposed to the Worker during **plan** or **execute** phases (see `ouroboros/ouroboros/tools/phase_control.py`).

**Purpose:** For a single difficult subtask, spawn **N** short-lived Worker attempts in parallel, score them, and pick a winner (tests pass, metric, vote—per manifest configuration).

**Scope:** One subtask, one synchronous tool call from the parent Worker. It is part of the normal phase machine.

---

## Meta-harness (experiments and candidates)

**Original intent:** An offline platform that runs **many full** harness configurations (models, manifests, skills, envelopes) against a benchmark set, scores them, and decides promotion—without coupling that machinery to ordinary runs.

**Current repository state:**

- There is **no** top-level `umbrella/meta_harness/` Python package in this checkout. Older docs referred to modules such as `store.py`, `evaluator.py`, `cli.py`; those paths may exist only in other branches or were consolidated elsewhere.
- **On-disk layout** under `.umbrella/meta_harness/` **is** still referenced for cleanup and optional web-bridge flows (see `umbrella/web_bridge/cleanup.py` and paths like `.umbrella/meta_harness/experiments/`).
- The bridge synthesizes a compact Markdown digest for the agent at  
  `.umbrella/ouroboros_drive/memory/knowledge/meta_harness_experience.md` via  
  `umbrella/integration/ouroboros_bridge.py::_build_meta_harness_summary()`.

Treat **meta-harness** documentation as describing the **contract** (where artifacts live, how operators clean them up) plus the **distinction** from `harness_run`. Re-introducing a dedicated CLI package belongs to future work and should be tracked in code before this chapter lists concrete module paths again.

---

## Workspace promotion and evals (related, not identical)

Workspace **seed promotion** (instance → seed) is implemented under `umbrella/evals/` — notably `promotion.py` and supporting models. That pipeline answers “should these file changes go back to the seed?” using guardrails, diffs, and eligibility checks. It complements verification but is **not** the same JSON experiment store historically called “meta-harness.”

---

## Operator checklist

| Question | Where to look |
|----------|----------------|
| Parallel attempts for one subtask? | Phase manifest allows `harness_run`; tool in `phase_control.py`. |
| Leftover experiment directories? | `.umbrella/meta_harness/` — use `umbrella/web_bridge/cleanup.py` patterns or manual deletion after confirming contents. |
| Promotion to seed after a good run? | `umbrella/evals/promotion.py` and workspace policy docs ([Part 7](07-workspaces-and-policy.md)). |
| What the agent sees about past experiments? | `meta_harness_experience.md` on the drive after bridge sync. |

---

Next: [Part 13 — Operations](13-operations.md)
