"""Select a provider behind the shared streaming contract."""
from __future__ import annotations

from models.anthropic import stream_anthropic
from models.catalog import ModelError, get_model
from models.gemini import stream_gemini
from models.openai import stream_openai


def stream_response(
    messages: list[dict],
    model_id: str,
    web_search_mode: str | bool,
    reasoning_effort: str | None,
    thinking_enabled: bool | None = None,
    provider_history: list[dict] | None = None,
    cache_key: str | None = None,
):
    """Dispatch a provider stream behind one text-delta event contract."""
    model = get_model(model_id)
    if model.provider == "openai":
        return stream_openai(
            messages, model_id, web_search_mode, reasoning_effort,
            provider_history=provider_history,
            cache_key=cache_key,
        )
    if model.provider == "anthropic":
        return stream_anthropic(
            messages, model_id, web_search_mode, reasoning_effort, thinking_enabled,
            provider_history=provider_history,
        )
    if model.provider == "google":
        return stream_gemini(
            messages, model_id, web_search_mode, reasoning_effort,
            provider_history=provider_history,
        )
    raise ModelError("MODEL_PROVIDER_UNSUPPORTED")
