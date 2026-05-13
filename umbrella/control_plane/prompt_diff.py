"""
Prompt diff rendering utilities.

Prompt rewrites must be reviewable, so we keep a small dedicated wrapper
around unified diffs instead of burying prompt edits inside generic file
patch handling.
"""

from difflib import unified_diff


def render_prompt_diff(
    before_text: str,
    after_text: str,
    surface_label: str = "prompt_surface",
    context_lines: int = 3,
) -> str:
    """Render a unified diff for a prompt rewrite proposal."""
    if before_text == after_text:
        return f"--- {surface_label}\n+++ {surface_label}\n(no changes)"

    diff_lines = list(
        unified_diff(
            before_text.splitlines(),
            after_text.splitlines(),
            fromfile=f"{surface_label}:before",
            tofile=f"{surface_label}:after",
            n=context_lines,
            lineterm="",
        )
    )
    return (
        "\n".join(diff_lines)
        if diff_lines
        else f"--- {surface_label}\n+++ {surface_label}\n(no changes)"
    )
