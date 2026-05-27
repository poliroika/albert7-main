"""
Ouroboros — LLM client.

The only module that communicates with the LLM API.
Contract: chat(), default_model(), available_models(), add_usage().
"""

import copy
import json
import logging

from ouroboros.tool_args_repair import repair_tool_arguments
import os
import time
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
# The OpenAI SDK dumps full request payloads (including giant prompt bodies)
# at DEBUG via ``openai._base_client``. In verbose Umbrella runs this makes the
# terminal nearly unreadable and can hide the actual tool/loop events we need
# to inspect. Keep SDK internals quiet while preserving Ouroboros' own logs.
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("openai._base_client").setLevel(logging.WARNING)

DEFAULT_LLM_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_LIGHT_MODEL = "google/gemini-3-pro-preview"
DEFAULT_LLM_CONNECT_TIMEOUT = 30.0
# Single chat-completions call (one LLM round). Slow local gateways need headroom.
DEFAULT_LLM_READ_TIMEOUT = 1800.0
DEFAULT_LLM_REQUEST_TIMEOUT = 1800.0
DEFAULT_LLM_CLIENT_RETRIES = 0
_STRICT_PROXY_REPAIR_LOGGED: set[str] = set()


def resolve_llm_api_key(explicit: str | None = None) -> str:
    """Resolve the primary LLM API key with backward compatibility."""
    return str(
        explicit
        or os.environ.get("OUROBOROS_LLM_API_KEY")
        or os.environ.get("OPENROUTER_API_KEY")
        or ""
    ).strip()


def resolve_llm_base_url(explicit: str | None = None) -> str:
    """Resolve the primary LLM base URL."""
    return (
        str(
            explicit or os.environ.get("OUROBOROS_LLM_BASE_URL") or DEFAULT_LLM_BASE_URL
        )
        .strip()
        .rstrip("/")
    )


def _resolve_float_env(name: str, default: float, *, minimum: float = 0.1) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return max(minimum, float(raw))
    except (TypeError, ValueError):
        log.warning("Invalid %s=%r, defaulting to %.1f", name, raw, default)
        return default


def _resolve_int_env(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        log.warning("Invalid %s=%r, defaulting to %d", name, raw, default)
        return default


def resolve_llm_client_retries() -> int:
    """Hidden SDK retries should be low; loop-level retries are logged."""
    return _resolve_int_env(
        "OUROBOROS_LLM_CLIENT_RETRIES",
        DEFAULT_LLM_CLIENT_RETRIES,
        minimum=0,
    )


def resolve_llm_request_timeout() -> float:
    """Per chat-completions call timeout in seconds."""
    return _resolve_float_env(
        "OUROBOROS_LLM_REQUEST_TIMEOUT",
        DEFAULT_LLM_REQUEST_TIMEOUT,
    )


def is_openrouter_base_url(base_url: str) -> bool:
    return "openrouter.ai" in str(base_url or "").lower()


def use_anthropic_style_cache_extensions(base_url: str | None = None) -> bool:
    """True when we may send cache_control on tools / multipart system (OpenRouter + Anthropic caching)."""
    if str(
        os.environ.get("OUROBOROS_ANTHROPIC_MESSAGE_FORMAT", "")
    ).strip().lower() in {"1", "true", "yes", "on"}:
        return True
    return is_openrouter_base_url(resolve_llm_base_url(base_url))


def sanitize_messages_for_strict_openai_proxy(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Some OpenAI-compatible gateways json.loads() every tool_calls[].function.arguments on each request.
    Models occasionally emit truncated / invalid JSON there → 400 'Unterminated string' on the server.
    Replace invalid argument payloads with '{}' so the outer request stays acceptable.
    """
    out: list[dict[str, Any]] = []
    repaired_notes: dict[str, int] = {}
    unrepairable_notes: dict[str, int] = {}
    for msg in messages:
        m = msg
        if msg.get("role") != "assistant":
            out.append(msg)
            continue
        tcs = msg.get("tool_calls")
        if not tcs:
            out.append(msg)
            continue
        m = copy.deepcopy(msg)
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw = fn.get("arguments")
            if raw is None:
                fn["arguments"] = "{}"
                tc["function"] = fn
                continue
            s = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
            try:
                json.loads(s)
            except (json.JSONDecodeError, TypeError, ValueError):
                try:
                    repaired, note = repair_tool_arguments(str(fn.get("name", "")), s)
                    fn["arguments"] = json.dumps(repaired, ensure_ascii=False)
                    tool_name = str(fn.get("name", ""))
                    if note:
                        key = f"{tool_name}:{note}"
                        if str(note).startswith("unrepairable"):
                            unrepairable_notes[key] = unrepairable_notes.get(key, 0) + 1
                        else:
                            repaired_notes[key] = repaired_notes.get(key, 0) + 1
                except (json.JSONDecodeError, TypeError, ValueError):
                    log.warning(
                        "[LLM] Replacing invalid tool arguments JSON for strict proxy (tool=%s): %r",
                        fn.get("name", ""),
                        (s[:120] + "…") if len(s) > 120 else s,
                    )
                    fn["arguments"] = "{}"
                tc["function"] = fn
        out.append(m)
    if repaired_notes:
        new_keys = [
            key
            for key in sorted(repaired_notes)
            if f"repaired:{key}" not in _STRICT_PROXY_REPAIR_LOGGED
        ]
        if new_keys:
            for key in new_keys:
                _STRICT_PROXY_REPAIR_LOGGED.add(f"repaired:{key}")
            summary = ", ".join(f"{k} x{repaired_notes[k]}" for k in new_keys)
            log.info(
                "[LLM] Repaired tool arguments for strict proxy: %s", summary[:1200]
            )
    if unrepairable_notes:
        new_keys = [
            key
            for key in sorted(unrepairable_notes)
            if f"unrepairable:{key}" not in _STRICT_PROXY_REPAIR_LOGGED
        ]
        if new_keys:
            for key in new_keys:
                _STRICT_PROXY_REPAIR_LOGGED.add(f"unrepairable:{key}")
            summary = ", ".join(f"{k} x{unrepairable_notes[k]}" for k in new_keys)
            # Keep this at debug to avoid surfacing non-actionable proxy hygiene.
            log.debug(
                "[LLM] Unrepairable tool arguments normalized to {}: %s", summary[:1200]
            )
    return out


def chat_completions_url(base_url: str) -> str:
    """Return a chat-completions URL from a base URL or endpoint URL."""
    normalized = str(base_url or "").strip().rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"


def llm_error_looks_like_html_tunnel_page(message: str) -> bool:
    """True when the server returned HTML (e.g. frp/nginx 404) instead of an OpenAI error JSON body."""
    m = (message or "").lower()
    return "<html" in m or "<!doctype" in m or "faithfully yours, frp" in m


def format_llm_exception_for_user_log(exc: BaseException, *, max_len: int = 400) -> str:
    """Short summary for WARNING logs and JSONL (avoid flooding with HTML tunnel pages)."""
    text = str(exc)
    if llm_error_looks_like_html_tunnel_page(text):
        return (
            "HTML/tunnel 404 (frp/nginx), not OpenAI JSON - "
            "check VPN and frp remote_port -> backend; POST .../v1/chat/completions must return JSON"
        )
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def normalize_reasoning_effort(value: str, default: str = "medium") -> str:
    allowed = {"none", "minimal", "low", "medium", "high", "xhigh"}
    v = str(value or "").strip().lower()
    return v if v in allowed else default


def reasoning_rank(value: str) -> int:
    order = {"none": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5}
    return int(order.get(str(value or "").strip().lower(), 3))


def add_usage(total: dict[str, Any], usage: dict[str, Any]) -> None:
    """Accumulate usage from one LLM call into a running total."""
    for k in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cached_tokens",
        "cache_write_tokens",
    ):
        total[k] = int(total.get(k) or 0) + int(usage.get(k) or 0)
    if usage.get("cost"):
        total["cost"] = float(total.get("cost") or 0) + float(usage["cost"])


def fetch_openrouter_pricing() -> dict[str, tuple[float, float, float]]:
    """
    Fetch current pricing from OpenRouter API.

    Returns dict of {model_id: (input_per_1m, cached_per_1m, output_per_1m)}.
    Returns empty dict on failure.
    """
    import logging

    log = logging.getLogger("ouroboros.llm")

    try:
        import requests
    except ImportError:
        log.warning("requests not installed, cannot fetch pricing")
        return {}

    try:
        url = "https://openrouter.ai/api/v1/models"
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()

        data = resp.json()
        models = data.get("data", [])

        # Prefixes we care about
        prefixes = ("anthropic/", "openai/", "google/", "meta-llama/", "x-ai/", "qwen/")

        pricing_dict = {}
        for model in models:
            model_id = model.get("id", "")
            if not model_id.startswith(prefixes):
                continue

            pricing = model.get("pricing", {})
            if not pricing or not pricing.get("prompt"):
                continue

            # OpenRouter pricing is in dollars per token (raw values)
            raw_prompt = float(pricing.get("prompt", 0))
            raw_completion = float(pricing.get("completion", 0))
            raw_cached_str = pricing.get("input_cache_read")
            raw_cached = float(raw_cached_str) if raw_cached_str else None

            # Convert to per-million tokens
            prompt_price = round(raw_prompt * 1_000_000, 4)
            completion_price = round(raw_completion * 1_000_000, 4)
            if raw_cached is not None:
                cached_price = round(raw_cached * 1_000_000, 4)
            else:
                cached_price = round(prompt_price * 0.1, 4)  # fallback: 10% of prompt

            # Sanity check: skip obviously wrong prices
            if prompt_price > 1000 or completion_price > 1000:
                log.warning(
                    f"Skipping {model_id}: prices seem wrong (prompt={prompt_price}, completion={completion_price})"
                )
                continue

            pricing_dict[model_id] = (prompt_price, cached_price, completion_price)

        log.info(f"Fetched pricing for {len(pricing_dict)} models from OpenRouter")
        return pricing_dict

    except (requests.RequestException, ValueError, KeyError) as e:
        log.warning(f"Failed to fetch OpenRouter pricing: {e}")
        return {}


class LLMClient:
    """OpenAI-compatible API wrapper. All LLM calls go through this class."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        self._api_key = resolve_llm_api_key(api_key)
        self._base_url = resolve_llm_base_url(base_url)
        self._is_openrouter = is_openrouter_base_url(self._base_url)
        self._use_cache_extensions = use_anthropic_style_cache_extensions(
            self._base_url
        )
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI
            import httpx

            connect_timeout = _resolve_float_env(
                "OUROBOROS_LLM_CONNECT_TIMEOUT",
                DEFAULT_LLM_CONNECT_TIMEOUT,
            )
            read_timeout = _resolve_float_env(
                "OUROBOROS_LLM_READ_TIMEOUT",
                DEFAULT_LLM_READ_TIMEOUT,
            )
            llm_max_retries = resolve_llm_client_retries()

            kwargs: dict[str, Any] = {
                "base_url": self._base_url,
                "api_key": self._api_key,
                "timeout": httpx.Timeout(read_timeout, connect=connect_timeout),
                "max_retries": llm_max_retries,
            }
            if self._is_openrouter:
                kwargs["default_headers"] = {
                    "HTTP-Referer": "https://colab.research.google.com/",
                    "X-Title": "Ouroboros",
                }
            self._client = OpenAI(**kwargs)
            log.info(
                "[LLM] Client configured: hidden_sdk_retries=%d, connect_timeout=%.1fs, read_timeout=%.1fs",
                llm_max_retries,
                connect_timeout,
                read_timeout,
            )
        return self._client

    def _fetch_generation_cost(self, generation_id: str) -> float | None:
        """Fetch cost from OpenRouter Generation API as fallback."""
        if not self._is_openrouter:
            return None
        try:
            import requests

            url = f"{self._base_url.rstrip('/')}/generation?id={generation_id}"
            resp = requests.get(
                url, headers={"Authorization": f"Bearer {self._api_key}"}, timeout=5
            )
            if resp.status_code == 200:
                data = resp.json().get("data") or {}
                cost = data.get("total_cost") or data.get("usage", {}).get("cost")
                if cost is not None:
                    return float(cost)
            # Generation might not be ready yet — retry once after short delay
            time.sleep(0.5)
            resp = requests.get(
                url, headers={"Authorization": f"Bearer {self._api_key}"}, timeout=5
            )
            if resp.status_code == 200:
                data = resp.json().get("data") or {}
                cost = data.get("total_cost") or data.get("usage", {}).get("cost")
                if cost is not None:
                    return float(cost)
        except Exception:
            log.debug("Failed to fetch generation cost from OpenRouter", exc_info=True)
            pass
        return None

    def chat(
        self,
        messages: list[dict[str, Any]],
        model: str,
        tools: list[dict[str, Any]] | None = None,
        reasoning_effort: str = "medium",
        max_tokens: int = 16384,
        tool_choice: Any = "auto",
        temperature: float | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Single LLM call. Returns: (response_message_dict, usage_dict with cost)."""
        client = self._get_client()
        effort = normalize_reasoning_effort(reasoning_effort)

        # Debug: log request details without exposing custom endpoints.
        request_timeout = resolve_llm_request_timeout()
        log.info(
            "[LLM] Sending request: model=%s, base_url=%s, messages_count=%d, request_timeout=%.1fs",
            model,
            "set" if self._base_url else "unset",
            len(messages),
            request_timeout,
        )
        log.debug(
            "[LLM] Chat completions URL: %s", chat_completions_url(self._base_url)
        )

        extra_body: dict[str, Any] = {}
        if self._is_openrouter:
            extra_body["reasoning"] = {"effort": effort, "exclude": True}

            # Pin Anthropic models to Anthropic provider for prompt caching
            if model.startswith("anthropic/"):
                extra_body["provider"] = {
                    "order": ["Anthropic"],
                    "allow_fallbacks": False,
                    "require_parameters": True,
                }

        request_messages = messages
        if not self._use_cache_extensions:
            request_messages = sanitize_messages_for_strict_openai_proxy(messages)

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": request_messages,
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if extra_body:
            kwargs["extra_body"] = extra_body
        if tools:
            # Anthropic/OpenRouter: cache_control on last tool for prompt caching.
            # Generic OpenAI-compatible proxies (e.g. custom v1 gateways) often reject unknown keys → 400.
            tools_payload = [t for t in tools]  # shallow copy
            if self._use_cache_extensions and tools_payload:
                last_tool = {**tools_payload[-1]}
                last_tool["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
                tools_payload[-1] = last_tool
            kwargs["tools"] = tools_payload
            kwargs["tool_choice"] = tool_choice

        log.debug(
            f"[LLM] Request kwargs: model={kwargs.get('model')}, max_tokens={kwargs.get('max_tokens')}, has_tools={tools is not None}"
        )

        kwargs["timeout"] = request_timeout

        _t0 = time.monotonic()
        try:
            resp = client.chat.completions.create(**kwargs)
        except Exception as e:
            _elapsed = time.monotonic() - _t0
            endpoint = chat_completions_url(self._base_url)
            err_text = str(e)
            if llm_error_looks_like_html_tunnel_page(err_text):
                log.error(
                    "[LLM] Request failed after %.1fs: HTML tunnel page instead of OpenAI JSON. "
                    "POST %s model=%s. Check VPN/frp: upstream must serve OpenAI-compatible /v1/chat/completions.",
                    _elapsed,
                    endpoint,
                    model,
                )
            else:
                short = err_text if len(err_text) <= 800 else err_text[:797] + "..."
                log.error(
                    "[LLM] Request failed after %.1fs: %s model=%s endpoint=%s",
                    _elapsed,
                    short,
                    model,
                    endpoint,
                )
            raise
        _elapsed = time.monotonic() - _t0
        log.info("[LLM] Response received in %.1fs, model=%s", _elapsed, model)
        resp_dict = resp.model_dump()
        usage = resp_dict.get("usage") or {}
        choices = resp_dict.get("choices") or [{}]
        msg = (choices[0] if choices else {}).get("message") or {}

        # Extract cached_tokens from prompt_tokens_details if available
        if not usage.get("cached_tokens"):
            prompt_details = usage.get("prompt_tokens_details") or {}
            if isinstance(prompt_details, dict) and prompt_details.get("cached_tokens"):
                usage["cached_tokens"] = int(prompt_details["cached_tokens"])

        # Extract cache_write_tokens from prompt_tokens_details if available
        # OpenRouter: "cache_write_tokens"
        # Native Anthropic: "cache_creation_tokens" or "cache_creation_input_tokens"
        if not usage.get("cache_write_tokens"):
            prompt_details_for_write = usage.get("prompt_tokens_details") or {}
            if isinstance(prompt_details_for_write, dict):
                cache_write = (
                    prompt_details_for_write.get("cache_write_tokens")
                    or prompt_details_for_write.get("cache_creation_tokens")
                    or prompt_details_for_write.get("cache_creation_input_tokens")
                )
                if cache_write:
                    usage["cache_write_tokens"] = int(cache_write)

        # Ensure cost is present in usage (OpenRouter includes it, but fallback if missing)
        if not usage.get("cost"):
            gen_id = resp_dict.get("id") or ""
            if gen_id:
                cost = self._fetch_generation_cost(gen_id)
                if cost is not None:
                    usage["cost"] = cost

        return msg, usage

    def vision_query(
        self,
        prompt: str,
        images: list[dict[str, Any]],
        model: str = "anthropic/claude-sonnet-4.6",
        max_tokens: int = 1024,
        reasoning_effort: str = "low",
    ) -> tuple[str, dict[str, Any]]:
        """
        Send a vision query to an LLM. Lightweight — no tools, no loop.

        Args:
            prompt: Text instruction for the model
            images: List of image dicts. Each dict must have either:
                - {"url": "https://..."} — for URL images
                - {"base64": "<b64>", "mime": "image/png"} — for base64 images
            model: VLM-capable model ID
            max_tokens: Max response tokens
            reasoning_effort: Effort level

        Returns:
            (text_response, usage_dict)
        """
        # Build multipart content
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for img in images:
            if "url" in img:
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": img["url"]},
                    }
                )
            elif "base64" in img:
                mime = img.get("mime", "image/png")
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{img['base64']}"},
                    }
                )
            else:
                log.warning(
                    "vision_query: skipping image with unknown format: %s",
                    list(img.keys()),
                )

        messages = [{"role": "user", "content": content}]
        response_msg, usage = self.chat(
            messages=messages,
            model=model,
            tools=None,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
        )
        text = response_msg.get("content") or ""
        return text, usage

    def default_model(self) -> str:
        """Return the single default model from env. LLM switches via tool if needed."""
        return (
            os.environ.get("OUROBOROS_MODEL", "").strip()
            or "anthropic/claude-sonnet-4.6"
        )

    def available_models(self) -> list[str]:
        """Return list of available models from env (for switch_model tool schema)."""
        main = (
            os.environ.get("OUROBOROS_MODEL", "").strip()
            or "anthropic/claude-sonnet-4.6"
        )
        code = os.environ.get("OUROBOROS_MODEL_CODE", "")
        light = os.environ.get("OUROBOROS_MODEL_LIGHT", "")
        models = [main]
        if code and code != main:
            models.append(code)
        if light and light != main and light != code:
            models.append(light)
        return models
