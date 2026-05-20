---
name: terminal-debugging
status: active
domains: ["debugging", "terminal"]
phases: ["execute", "subtask_template"]
when_to_use: "When running commands, servers, tests, or reading terminal scrollback."
---

## Terminal Debugging

Use terminal output as evidence.

Practice:
- Run the narrow failing command first.
- Capture stdout, stderr, exit code, and working directory.
- Prefer deterministic commands over interactive sessions.
- For servers, record PID, port, health URL, and log paths.
- Stop or clean up only processes started for the current verification when needed.

Do not treat a background server start as success without a health or behavior check.
