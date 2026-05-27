# JSON examples (documentation only)

Static samples for drive-state and contract payloads. These files are **not** loaded by Umbrella at runtime; agents produce the real artifacts under `.umbrella/ouroboros_drive/state/` via `submit_capability_declaration` and related tools.

| File | Runtime path | Purpose |
|------|----------------|---------|
| [capability_declaration.json](capability_declaration.json) | `drive/state/capability_declaration.json` | Research-phase declaration of probed workspace capabilities (gates plan validation) |

See [Runtime artifacts](../technical-report/04-runtime-artifacts.md) for the full on-disk layout.
