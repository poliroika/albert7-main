---
name: patch-discipline
status: active
domains: ["implementation", "patching"]
phases: ["execute", "subtask_template"]
when_to_use: "When editing workspace files."
---

## Patch Discipline

After a patch tool returns `required_mode`, obey it exactly:

- `fresh_read` — call `read_file`, then retry a smaller hunk.
- `replace_workspace_file` — use `replace_workspace_file(path, expected_sha256, content)` with the digest from read.

Do not use delete/recreate sidecars or freeform recovery when Umbrella returns a typed block.
