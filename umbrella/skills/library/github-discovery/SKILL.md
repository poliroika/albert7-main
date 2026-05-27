---
name: github-discovery
status: active
domains: ["research", "github", "prior_art"]
phases: ["research", "plan"]
when_to_use: "When the task may benefit from existing open-source architecture or implementation patterns."
---

## GitHub Discovery

Use GitHub as **deliberate prior art**, not blind paste. After every search, decide what you want from each repo.

### Workflow

1. `github_project_search(query=...)` — pick 1–2 relevant repos from `results`.
2. For each chosen repo, call `github_extract_snippets` using that row's `suggested_extract` (`paths` + `queries`).
3. Read returned `knowledge_md` paths (under `.memory/drive/memory/knowledge/inspiration/`).
4. Choose **reuse intent** (see tool `adoption_playbook.decide_intent`):
   - **idea_only** — non-permissive licence or architecture hint only; summarize in `palace_add`.
   - **pattern_adapt** — permissive snippet; reimplement in workspace types (default).
   - **codeptr** — record pointer + target path for execute (`palace_add` kind=codeptr; plan `codeptr_refs`).
   - **dependency_import** — only when upstream is a real installable library (rare for game repos).
5. `palace_add` with a valid `source_id`: `github:owner/repo` or `github_extract_snippets:owner/repo` after extract.

### Plan / execute handoff

- In **plan**: attach `codeptr_refs` or subtask notes pointing at `knowledge_md` files and intended workspace paths.
- In **execute**: `read_file` each snippet before `apply_workspace_patch`; never paste GPL/non-permissive bodies.

### Quality

- Prefer `license_permissive: true` repos for code adaptation.
- Empty search is valid evidence; record as `kind=observation`, not a fake finding.
- Do not skip extract when a permissive repo clearly matches the task architecture.
