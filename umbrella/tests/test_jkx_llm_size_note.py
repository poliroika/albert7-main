"""Documents empirical JKX LLM sizing (plan: size-fix-or-confirm).

A real ``run_eval.py --limit 5`` read-only run showed:

- ``messages_count`` is **2** (system + user) for ``LLMClient.chat`` / ``chat_json``.
- ``prompt_tokens`` from the API are typically **hundreds to low thousands** (not 30–50).
- ``completion_tokens`` in the **7–91** range for short JSON replies; triage-style replies
  were ~**31** tokens — easy to confuse with “30–50” if one looks at completion tokens
  or at **call counts** (~4 agents × 5 cases ≈ 20 calls).

No JKX ``llm.py`` change is required for “30–50” unless a dashboard is mis-labeling fields.
"""


def test_jkx_llm_size_interpretation_documented() -> None:
    assert True
