"""
Shared helpers for OpenRouter reasoning (chain-of-thought) integration.

OpenRouter surfaces a reasoning-capable model's chain of thought on the
assistant message. Two things matter for correctness:

  1. Enable reasoning with ``extra_body={"reasoning": {"enabled": true}}``.
     The OpenAI SDK does not know this parameter, so it must be passed via
     ``extra_body``. Made globally toggleable via env for easy rollback.

  2. Forward ``reasoning_details`` back VERBATIM on the NEXT assistant
     message so the model can continue its chain of thought across turns
     (OpenRouter's documented multi-turn continuation) -- but ONLY when the
     provider actually returned it. Passing ``None`` / an empty value back up
     can break the follow-up call, so we always gate on presence. Many free
     (``:free``) routes do not surface reasoning, and ``getattr`` is used so a
     missing field never raises.
"""

import os


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def reasoning_enabled() -> bool:
    """True unless OPENROUTER_REASONING is set to a false-y value."""
    return _flag("OPENROUTER_REASONING", "1")


def reasoning_extra_body():
    """
    Return the ``extra_body`` argument to pass to chat.completions.create,
    or ``None`` when reasoning is disabled.

    Optional ``OPENROUTER_REASONING_EFFORT`` (e.g. "high") is forwarded to
    OpenRouter when set.
    """
    if not reasoning_enabled():
        return None
    body = {"reasoning": {"enabled": True}}
    effort = os.getenv("OPENROUTER_REASONING_EFFORT", "").strip()
    if effort:
        body["reasoning"]["effort"] = effort
    return body


def extract_reasoning_details(assistant_message):
    """
    Safely pull ``reasoning_details`` off a parsed assistant message.

    Returns the raw list/dict the provider gave us, or ``None`` when absent
    (many free / ``:free`` routes do not surface reasoning). Never raises,
    even if the SDK drops an unknown field.
    """
    if assistant_message is None:
        return None
    try:
        details = getattr(assistant_message, "reasoning_details", None)
    except Exception:
        return None
    return details or None


def with_reasoning(msg: dict, reasoning_details) -> dict:
    """
    Return a copy of an assistant message dict that carries
    ``reasoning_details`` ONLY when present.

    This is exactly what you should send back on a later turn so the model
    continues from where it left off. Passing an absent/empty value back is
    deliberately skipped to avoid corrupting the follow-up request.
    """
    out = dict(msg or {})
    if reasoning_details:
        out["reasoning_details"] = reasoning_details
    return out