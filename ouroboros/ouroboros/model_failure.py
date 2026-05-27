"""Detect provider/model transport failures surfaced as agent text."""


def is_model_response_failure(text: str) -> bool:
    normalized = str(text or "").strip()
    if not normalized:
        return False
    lowered = normalized.lower()
    return (
        "failed to get a response from model" in lowered
        or "failed to get a response from the model" in lowered
        or "model returned an empty response" in lowered
    )
