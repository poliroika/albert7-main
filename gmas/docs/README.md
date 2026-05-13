# gMAS Documentation

This directory contains the MkDocs documentation for gMAS.

## Building Documentation

### Install Dependencies

```bash
uv sync --group docs
```

### Build

```bash
uv run mkdocs build
```

Output will be in `site/` directory.

### Serve Locally

```bash
uv run mkdocs serve
```

Visit <http://127.0.0.1:8000>

### Deploy

```bash
uv run mkdocs gh-deploy
```

## Structure

```
docs/
├── index.md                     # Homepage
├── getting-started/             # Getting started guides
│   ├── installation.md
│   ├── quickstart.md
│   └── development.md
├── user-guide/                  # User guide
│   ├── key-concepts.md
│   ├── core/                    # Core components
│   ├── execution/               # Execution guides
│   └── advanced/                # Advanced topics
├── api/                         # API reference
│   ├── core.md
│   ├── execution.md
│   ├── builder.md
│   ├── tools.md
│   └── config.md
├── examples/                    # Usage examples
│   ├── basic-usage.md
│   ├── streaming.md
│   └── gnn-routing.md
└── contributing/                # Contributing guides
    ├── index.md
    ├── workflow.md
    ├── code-style.md
    └── testing.md
```

## Writing Documentation

### Adding New Pages

1. Create the markdown file in the appropriate directory
2. Add it to the `nav` section in `mkdocs.yml`
3. Follow existing style and formatting

### Code Examples

Use fenced code blocks with language specified:

```python
def example():
    return "hello"
```

### Admonitions

!!! note
    This is a note

!!! warning
    This is a warning

!!! tip
    This is a tip

## Preview Changes

Run `uv run mkdocs serve` and edit files. Changes will auto-reload.
