"""
Code analysis service for workspace improvement.

This module provides LLM-based code analysis that can:
- Inspect workspace code for issues
- Suggest specific improvements
- Generate patches for common problems
"""

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _anthropic_messages_url(base_url: str) -> str:
    """Avoid ``/v1/v1/messages`` when ``get_anthropic_base_url`` already ends with ``/v1``."""
    b = str(base_url).strip().rstrip("/")
    if b.endswith("/v1"):
        b = b[:-3].rstrip("/")
    return f"{b}/v1/messages"


def _use_native_anthropic_api(base_url: str) -> bool:
    """True only for real Anthropic API; internal OpenAI-compatible proxies use chat/completions."""
    import os

    if (os.environ.get("UMBRELLA_LLM_PROVIDER") or "").strip().lower() == "anthropic":
        return True
    b = str(base_url or "").lower()
    return "anthropic.com" in b


def get_llm_client():
    """Get configured LLM client for code analysis.

    Defaults to **OpenAI-compatible** ``POST .../chat/completions`` with
    ``Authorization: Bearer`` so internal gateways (e.g. ``/v1`` proxies)
    work. Native Anthropic ``/v1/messages`` is used only when the base URL
    points at ``anthropic.com`` or ``UMBRELLA_LLM_PROVIDER=anthropic``.
    """
    try:
        from umbrella.env import (
            get_anthropic_base_url,
            get_code_analyzer_model,
            get_llm_env_config,
            load_env,
        )

        load_env()
        _, api_key, base_url = get_llm_env_config()

        if not api_key:
            return None

        import httpx

        from ouroboros.llm import chat_completions_url

        class SimpleLLMClient:
            def __init__(self, api_key, base_url):
                self.api_key = api_key
                self.base_url = get_anthropic_base_url(base_url)
                self._anthropic_native = _use_native_anthropic_api(self.base_url)
                if self._anthropic_native:
                    self.headers = {
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                    }
                else:
                    self.headers = {
                        "Authorization": f"Bearer {api_key}",
                    }
                    if "openrouter" in self.base_url.lower():
                        self.headers["HTTP-Referer"] = "https://umbrella.ai"

            def chat(self, messages, model=None):
                model = model or get_code_analyzer_model()
                if self._anthropic_native:
                    response = httpx.post(
                        _anthropic_messages_url(self.base_url),
                        headers=self.headers,
                        json={
                            "model": model,
                            "max_tokens": 4096,
                            "messages": messages,
                        },
                        timeout=60.0,
                    )
                    response.raise_for_status()
                    data = response.json()
                    parts = data.get("content") or []
                    text = ""
                    for block in parts:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text += str(block.get("text", ""))
                    return {"role": "assistant", "content": text}, {}

                url = chat_completions_url(self.base_url)
                response = httpx.post(
                    url,
                    headers=self.headers,
                    json={
                        "model": model,
                        "messages": messages,
                        "max_tokens": 4096,
                    },
                    timeout=60.0,
                )
                response.raise_for_status()
                data = response.json()
                msg = data.get("choices", [{}])[0].get("message", {}) or {}
                return msg, {}

        return SimpleLLMClient(api_key, base_url)

    except Exception as e:
        log.warning(f"Failed to create LLM client: {e}")
        return None


def analyze_workspace_code(
    workspace_path: Path,
    task_description: str,
    focus_areas: list[str] | None = None,
) -> dict[str, Any]:
    """Analyze workspace code and suggest improvements.

    Args:
        workspace_path: Path to the workspace
        task_description: What task the workspace should solve
        focus_areas: Specific areas to focus on (e.g., ["agents", "graph"])

    Returns:
        Dict with analysis results
    """
    from umbrella.env import get_code_analyzer_model

    client = get_llm_client()
    if not client:
        return {
            "status": "no_llm",
            "message": "LLM not configured for code analysis",
        }

    # Collect workspace structure
    structure = _collect_workspace_structure(workspace_path)

    # Read key files
    code_samples = _read_workspace_code_samples(workspace_path, focus_areas or [])

    # Build analysis prompt
    prompt = f"""You are a code reviewer analyzing a multi-agent workspace.

**Task the workspace should solve:**
{task_description}

**Workspace structure:**
{structure}

**Code samples to review:**
{code_samples}

Your task:
1. Identify the top 3-5 issues in this workspace code
2. For each issue provide:
   - File path
   - Line numbers (if applicable)
   - What the problem is
   - How to fix it (specific code change)

Format your response as JSON:
```json
{{
  "issues": [
    {{
      "file": "path/to/file.py",
      "line": 42,
      "problem": "description of problem",
      "fix": "specific code or description of fix",
      "severity": "high|medium|low"
    }}
  ]
}}
```

Focus on:
- Logic bugs
- Missing error handling
- Inefficient patterns
- Missing features for the stated task
- Configuration issues

Be specific and actionable.
"""

    try:
        response, usage = client.chat(
            messages=[{"role": "user", "content": prompt}],
            model=get_code_analyzer_model(),
        )

        content = response.get("content", "")

        # Try to parse JSON from response
        import json

        try:
            # Extract JSON from markdown code blocks if present
            if "```json" in content:
                start = content.find("```json") + 7
                end = content.find("```", start)
                if end > start:
                    content = content[start:end]
            elif "```" in content:
                start = content.find("```") + 3
                end = content.find("```", start)
                if end > start:
                    content = content[start:end]

            result = json.loads(content)
            return {
                "status": "success",
                "issues": result.get("issues", []),
                "raw_response": content,
            }
        except json.JSONDecodeError:
            return {
                "status": "parse_error",
                "raw_response": content,
                "message": "Could not parse LLM response as JSON",
            }

    except Exception as e:
        log.error(f"Code analysis failed: {e}")
        return {
            "status": "error",
            "error": str(e),
        }


def _collect_workspace_structure(workspace_path: Path) -> str:
    """Collect workspace structure information."""
    parts = []
    parts.append(f"Workspace: {workspace_path.name}")

    # List directories
    for item in workspace_path.iterdir():
        if item.is_dir():
            # Count files
            try:
                file_count = sum(1 for _ in item.rglob("*") if _.is_file())
                parts.append(f"  {item.name}/ ({file_count} files)")
            except:
                parts.append(f"  {item.name}/")
        elif item.is_file() and not item.name.startswith("."):
            parts.append(f"  {item.name}")

    return "\n".join(parts)


def _read_workspace_code_samples(workspace_path: Path, focus_areas: list[str]) -> str:
    """Read representative code samples from the workspace."""
    samples = []

    # Priority files to read
    priority_files = [
        "agents/tester.py",
        "agents/drafter.py",
        "agents/researcher.py",
        "agents/editor.py",
        "graph/topology.toml",
        "prompts/system.md",
        "TASK_MAIN.md",
    ]

    for file_path in priority_files:
        full_path = workspace_path / file_path
        if full_path.exists():
            try:
                content = full_path.read_text(encoding="utf-8")
                # Truncate if too long
                if len(content) > 2000:
                    content = content[:2000] + "\n... (truncated)"
                samples.append(f"**{file_path}:**\n```\n{content}\n```\n")
            except Exception as e:
                samples.append(f"**{file_path}:** (error: {e})")

    return "\n\n".join(samples)
