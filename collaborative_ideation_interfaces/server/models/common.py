"""Shared provider input, result and event normalization. No SDK calls.

Adapters yield dictionaries with a ``type`` discriminator: text_delta,
text_metadata, reasoning_delta, status, search_started, search_query,
source_added, citation_added, and done. The done event carries the normalized
``generation`` result. Private providerState is retained for history replay;
only the allowlisted fields in api.transport are sent to clients.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from models.catalog import (
    ModelError,
    ModelSpec,
    RESEARCH_PRICING_VERSION,
    calculate_cost,
    estimate_tokens,
)


CONTEXT_LIMIT_ERROR_MARKERS = (
    "context_length_exceeded",
    "context window exceeded",
    "exceeds the context window",
    "maximum context length",
    "prompt is too long",
    "too many input tokens",
    "input token count exceeds",
    "input tokens exceed",
    "request input is too long",
)


def _provider_model_error(exc: object) -> ModelError:
    parts = [str(exc)]
    for attribute in ("code", "type", "message", "body", "error", "response"):
        value = getattr(exc, attribute, None)
        if value is not None:
            parts.append(str(value))
    details = " ".join(parts).lower()
    input_count_exceeded = (
        ("input token count" in details or "input tokens" in details)
        and ("exceed" in details or "maximum" in details)
    )
    if any(marker in details for marker in CONTEXT_LIMIT_ERROR_MARKERS) or input_count_exceeded:
        return ModelError("CONTEXT_LIMIT_EXCEEDED")
    return ModelError("PROVIDER_REQUEST_FAILED")


def _int_attr(value: object, name: str, fallback: str | None = None) -> int:
    raw = getattr(value, name, None)
    if raw is None and fallback:
        raw = getattr(value, fallback, None)
    return max(0, int(raw or 0))


def _field(value: object, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _clean_optional_string(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _new_search_trace() -> dict:
    return {"executions": [], "sources": [], "citations": []}


def _add_search_source(trace: dict, source: dict) -> int | None:
    url = _clean_optional_string(source.get("url"))
    if not url:
        return None
    for index, existing in enumerate(trace["sources"]):
        if existing["url"] == url:
            for key, value in source.items():
                if value is not None and not existing.get(key):
                    existing[key] = value
            return index
    normalized = {"url": url, "title": _clean_optional_string(source.get("title"))}
    for key in ("pageAge", "encryptedContent"):
        value = _clean_optional_string(source.get(key))
        if value:
            normalized[key] = value
    trace["sources"].append(normalized)
    return len(trace["sources"]) - 1


def _add_search_citation(trace: dict, citation: dict) -> None:
    source_index = _add_search_source(
        trace,
        {"url": citation.get("url"), "title": citation.get("title")},
    )
    if source_index is None:
        return
    normalized: dict[str, Any] = {"sourceIndex": source_index}
    for key in ("startIndex", "endIndex"):
        value = citation.get(key)
        if value is not None:
            normalized[key] = max(0, int(value))
    for key in ("citedText", "encryptedIndex", "indexUnit"):
        value = _clean_optional_string(citation.get(key))
        if value:
            normalized[key] = value
    if normalized not in trace["citations"]:
        trace["citations"].append(normalized)


def _normalize_messages(messages: list[dict]) -> list[dict[str, Any]]:
    normalized = [
        {"role": row["role"], "content": str(row.get("content", "")),
         "attachmentTokens": sum(f.get("estimatedTokens", 0) for f in row.get("_files", []))}
        for row in messages
        if row.get("role") in {"user", "assistant"} and (str(row.get("content", "")).strip() or row.get('_files'))
    ]
    if not any(row["role"] == "user" for row in normalized):
        raise ModelError("EMPTY_MESSAGE")
    return normalized


def _estimated_input_tokens(normalized):
    return estimate_tokens("\n".join(row["content"] for row in normalized)) + sum(row.get("attachmentTokens", 0) for row in normalized)


def _attachment_blocks(message: dict, provider: str) -> list[dict]:
    """Keep documents and images distinct from the participant's question."""
    blocks = []
    for file in message.get('_files', []):
        mime = file['mime']
        is_image = mime.startswith('image/')
        if provider == 'openai':
            if is_image:
                blocks.append({'type': 'input_image', 'image_url': f"data:{mime};base64,{file['base64']}"})
            else:
                blocks.append({'type': 'input_file', 'filename': file['name'], 'file_data': f"data:{mime};base64,{file['base64']}"})
        elif provider == 'anthropic':
            source = ({'type': 'file', 'file_id': file['fileId']} if file.get('fileId') else
                      {'type': 'base64', 'media_type': mime, 'data': file['base64']})
            blocks.append({'type': 'image' if is_image else 'document', 'source': source,
                           **({'title': file['name']} if not is_image else {})})
        else:
            blocks.append({'type': 'image' if is_image else 'document', 'mime_type': mime,
                           'data': file['base64']})
    return blocks


def _jsonable(value: Any) -> Any:
    """Convert provider SDK values to JSON without exposing them to the client."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return model_dump(mode="json", exclude_none=True)
        except TypeError:
            return model_dump(exclude_none=True)
    if hasattr(value, "__dict__"):
        return {
            str(key): _jsonable(item)
            for key, item in vars(value).items()
            if not str(key).startswith("_") and item is not None
        }
    return str(value)


def _summary_blocks(texts: list[object]) -> list[dict]:
    return [
        {"index": index, "text": text}
        for index, raw in enumerate(texts)
        if (text := str(raw or "").strip())
    ]


def _history_payloads(provider_history: list[dict] | None, provider: str) -> dict[str, dict]:
    history: dict[str, dict] = {}
    for entry in provider_history or []:
        if entry.get("provider") != provider or not isinstance(entry.get("payload"), dict):
            continue
        message_id = entry.get("outputMessageId")
        if isinstance(message_id, str) and message_id:
            history[message_id] = entry["payload"]
    return history


def _provider_state(provider: str, model_id: str, payload: dict) -> dict:
    return {
        "schemaVersion": 1,
        "provider": provider,
        "modelId": model_id,
        "payload": _jsonable(payload),
    }


def _provider_result(
    *,
    content: str,
    model: ModelSpec,
    effort: str,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    reasoning_tokens: int,
    search_queries: int,
    provider_response_id: str | None,
    cache_write_input_tokens: int = 0,
    provider_status: str = "completed",
    finish_reason: str | None = None,
    usage_estimated: bool = False,
    thinking_enabled: bool = False,
    web_search_trace: dict | None = None,
    reasoning_summary: list[dict] | None = None,
    provider_state: dict | None = None,
) -> dict:
    if not content.strip():
        raise ModelError("EMPTY_PROVIDER_RESPONSE")
    research_credit_cost_usd = calculate_cost(
        model,
        input_tokens,
        cached_input_tokens,
        output_tokens,
        reasoning_tokens,
        search_queries,
        cache_write_input_tokens,
    )
    pricing_band = model.pricing_band(input_tokens)
    return {
        "content": content,
        "inputTokens": input_tokens,
        "cachedInputTokens": cached_input_tokens,
        "cacheWriteInputTokens": cache_write_input_tokens,
        "outputTokens": output_tokens,
        "reasoningTokens": reasoning_tokens,
        "reasoningEffort": effort,
        "thinkingEnabled": thinking_enabled,
        "searchQueries": search_queries,
        "webSearchUsed": bool(search_queries),
        "researchCreditCostUsd": research_credit_cost_usd,
        "researchPricingVersion": RESEARCH_PRICING_VERSION,
        "researchPricingBand": pricing_band.id,
        # Keep the legacy field while clients and existing study data migrate.
        "costUsd": research_credit_cost_usd,
        "responseMode": "live",
        "providerResponseId": provider_response_id,
        "providerStatus": provider_status,
        "finishReason": finish_reason,
        "usageEstimated": usage_estimated,
        "pricingSnapshot": model.pricing_snapshot(input_tokens),
        "webSearchTrace": web_search_trace,
        "reasoningSummary": reasoning_summary or [],
        "providerState": provider_state,
    }
