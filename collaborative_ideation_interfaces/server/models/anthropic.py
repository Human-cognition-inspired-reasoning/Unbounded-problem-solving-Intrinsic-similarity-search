"""Anthropic request construction, history replay and streaming."""
from __future__ import annotations

import json
import os
from typing import Any

from models.catalog import (
    ANTHROPIC_CACHE_CONTROL,
    ModelError,
    ModelSpec,
    estimate_tokens,
    get_model,
    get_reasoning_effort,
    get_thinking_enabled,
    get_web_search_mode,
)
from models.common import (
    _add_search_citation,
    _add_search_source,
    _attachment_blocks,
    _clean_optional_string,
    _estimated_input_tokens,
    _field,
    _history_payloads,
    _int_attr,
    _jsonable,
    _new_search_trace,
    _normalize_messages,
    _provider_model_error,
    _provider_result,
    _provider_state,
    _summary_blocks,
)


def _anthropic_search_trace(message: object) -> dict | None:
    trace = _new_search_trace()
    executions_by_provider_id: dict[str, dict] = {}
    for block in _field(message, "content", []) or []:
        block_type = _field(block, "type")
        if block_type == "server_tool_use" and _field(block, "name") == "web_search":
            provider_id = _clean_optional_string(_field(block, "id"))
            tool_input = _field(block, "input", {}) or {}
            query = _clean_optional_string(_field(tool_input, "query"))
            execution = {
                "providerSearchId": provider_id,
                "action": "search",
                "queries": [query] if query else [],
                "status": "started",
            }
            trace["executions"].append(execution)
            if provider_id:
                executions_by_provider_id[provider_id] = execution
        elif block_type == "web_search_tool_result":
            provider_id = _clean_optional_string(_field(block, "tool_use_id"))
            execution = executions_by_provider_id.get(provider_id or "")
            if execution is None:
                execution = {
                    "providerSearchId": provider_id,
                    "action": "search",
                    "queries": [],
                    "status": "started",
                }
                trace["executions"].append(execution)
            result_content = _field(block, "content", [])
            if isinstance(result_content, list):
                execution["status"] = "complete"
                for result in result_content:
                    if _field(result, "type") != "web_search_result":
                        continue
                    _add_search_source(
                        trace,
                        {
                            "url": _field(result, "url"),
                            "title": _field(result, "title"),
                            "pageAge": _field(result, "page_age"),
                            "encryptedContent": _field(result, "encrypted_content"),
                        },
                    )
            else:
                execution["status"] = "failed"
                execution["errorCode"] = _clean_optional_string(_field(result_content, "error_code"))
        elif block_type == "text":
            for citation in _field(block, "citations", []) or []:
                if _field(citation, "type") != "web_search_result_location":
                    continue
                _add_search_citation(
                    trace,
                    {
                        "url": _field(citation, "url"),
                        "title": _field(citation, "title"),
                        "citedText": _field(citation, "cited_text"),
                        "encryptedIndex": _field(citation, "encrypted_index"),
                    },
                )
    return trace if any(trace.values()) else None


def _anthropic_request_args(
    provider_messages: list[dict],
    model: ModelSpec,
    effort: str,
    thinking_enabled: bool,
    web_search_mode: str | bool,
) -> dict[str, Any]:
    web_search_mode = get_web_search_mode(web_search_mode)
    if web_search_mode == "required" and not model.supports_forced_web_search:
        raise ModelError("FORCED_SEARCH_UNSUPPORTED")
    if web_search_mode == "required" and thinking_enabled and not model.adaptive_thinking:
        raise ModelError("FORCED_SEARCH_WITH_MANUAL_THINKING_UNSUPPORTED")
    request_args: dict[str, Any] = {
        "model": model.id,
        "max_tokens": model.max_output_tokens or 64_000,
        "messages": provider_messages,
        # Top-level placement caches the longest stable prefix of the request:
        # the provider puts the breakpoint on the last cacheable block, which is
        # everything up to the message this turn appended.
        "cache_control": ANTHROPIC_CACHE_CONTROL,
    }
    if model.reasoning_parameter != "none":
        request_args["output_config"] = {"effort": effort}
    if model.supports_thinking_toggle and not thinking_enabled:
        request_args["thinking"] = {"type": "disabled"}
    elif model.adaptive_thinking:
        # `display` defaults to "omitted" on these models, which streams
        # thinking blocks with empty text. The reasoning UI needs the summary,
        # so ask for it explicitly rather than relying on the default.
        request_args["thinking"] = {"type": "adaptive", "display": "summarized"}
        if model.thinking_prefix_binding:
            # Keep intact history whenever possible. If the provider detects a
            # changed prefix, drop only invalid thinking instead of failing the
            # participant's next turn; retain its transformation report below.
            request_args["thinking"]["block_binding"] = {
                "prefix_mismatch_behavior": "drop_block",
            }
            request_args["extra_headers"] = {
                "anthropic-beta": "thinking-binding-controls-2026-08-01",
            }
    elif model.supports_thinking_toggle:
        request_args["thinking"] = {
            "type": "enabled",
            "budget_tokens": min(8_192, (model.max_output_tokens or 64_000) - 1),
        }
    request_args["tools"] = [
        {"type": "web_search_20250305", "name": "web_search"}
    ]
    request_args["tool_choice"] = (
        {"type": "tool", "name": "web_search"}
        if web_search_mode == "required"
        else {"type": "auto"}
    )
    return request_args


def _anthropic_text(message: object) -> str:
    return "".join(
        str(getattr(block, "text", "") or "")
        for block in (getattr(message, "content", None) or [])
        if getattr(block, "type", None) == "text"
    )


def _anthropic_replayable_blocks(blocks: list) -> list:
    """Drop server tool calls whose result block never arrived.

    A turn cut short by `max_tokens` or paused with `pause_turn` can end on a
    `server_tool_use` block with no matching `web_search_tool_result`. The
    provider rejects that unpaired call when the turn is replayed.
    """
    resolved = {
        _clean_optional_string(_field(block, "tool_use_id"))
        for block in blocks
        if _field(block, "type") == "web_search_tool_result"
    }
    return [
        block
        for block in blocks
        if _field(block, "type") != "server_tool_use"
        or _clean_optional_string(_field(block, "id")) in resolved
    ]


def _anthropic_messages(messages: list[dict], provider_history: list[dict] | None) -> list[dict]:
    history = _history_payloads(provider_history, "anthropic")
    result: list[dict] = []
    for message in messages:
        role = message.get("role")
        if role not in {"user", "assistant"}:
            continue
        if role == "assistant":
            blocks = history.get(str(message.get("id")), {}).get("contentBlocks")
            replayable = _anthropic_replayable_blocks(blocks) if isinstance(blocks, list) else []
            if replayable:
                result.append({"role": "assistant", "content": replayable})
                continue
        content = str(message.get("content", ""))
        files = _attachment_blocks(message, 'anthropic')
        if files:
            result.append({'role': role, 'content': [
                *([{'type': 'text', 'text': content}] if content.strip() else []), *files,
            ]})
        elif content.strip():
            result.append({"role": role, "content": content})
    if not any(row["role"] == "user" for row in result):
        raise ModelError("EMPTY_MESSAGE")
    return result


def _anthropic_reasoning_summary(message: object, summarized_display: bool) -> list[dict]:
    if not summarized_display:
        return []
    return _summary_blocks([
        _field(block, "thinking") or _field(block, "text")
        for block in (_field(message, "content", []) or [])
        if _field(block, "type") == "thinking"
    ])


def _anthropic_stream_thinking_tokens(event: object, previous: int | None) -> int | None:
    # SDK 0.116.0 does not copy this final message_delta breakdown into
    # get_final_message(). Usage is cumulative, so replace rather than add.
    if _field(event, "type") == "message_delta":
        details = _field(_field(event, "usage"), "output_tokens_details")
        tokens = _field(details, "thinking_tokens")
        if tokens is not None:
            return max(0, int(tokens))
    return previous


def _anthropic_result(
    message: object,
    normalized: list[dict[str, Any]],
    model: ModelSpec,
    effort: str,
    thinking_enabled: bool,
    fallback_content: str = "",
    stream_thinking_tokens: int | None = None,
) -> dict:
    content = _anthropic_text(message) or fallback_content
    usage = getattr(message, "usage", None)
    usage_estimated = usage is None
    cache_creation_tokens = _int_attr(usage, "cache_creation_input_tokens")
    cached_input_tokens = _int_attr(usage, "cache_read_input_tokens")
    input_tokens = (
        _int_attr(usage, "input_tokens") + cache_creation_tokens + cached_input_tokens
        if usage
        else _estimated_input_tokens(normalized)
    )
    total_output_tokens = _int_attr(usage, "output_tokens")
    reasoning_tokens = _int_attr(getattr(usage, "output_tokens_details", None), "thinking_tokens")
    if stream_thinking_tokens is not None:
        reasoning_tokens = stream_thinking_tokens
    visible_output_tokens = (
        max(0, total_output_tokens - reasoning_tokens)
        if usage
        else estimate_tokens(content)
    )
    search_queries = _int_attr(getattr(usage, "server_tool_use", None), "web_search_requests")
    finish_reason = str(getattr(message, "stop_reason", None) or "") or None
    if finish_reason == "refusal" and not content.strip():
        stop_details = _field(message, "stop_details", {}) or {}
        content = (
            _clean_optional_string(_field(stop_details, "explanation"))
            or "Claude가 이 요청에 응답할 수 없다고 판단했습니다."
        )
    if finish_reason == "refusal":
        provider_status = "refused"
    elif finish_reason in {"max_tokens", "pause_turn", "model_context_window_exceeded"}:
        provider_status = "incomplete"
    else:
        provider_status = "completed"
    return _provider_result(
        content=content,
        model=model,
        effort=effort,
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        cache_write_input_tokens=cache_creation_tokens,
        output_tokens=visible_output_tokens,
        reasoning_tokens=reasoning_tokens,
        search_queries=search_queries,
        provider_response_id=getattr(message, "id", None),
        provider_status=provider_status,
        finish_reason=finish_reason,
        usage_estimated=usage_estimated,
        thinking_enabled=thinking_enabled,
        web_search_trace=_anthropic_search_trace(message),
        reasoning_summary=_anthropic_reasoning_summary(message, model.adaptive_thinking),
        provider_state=_provider_state(
            "anthropic",
            model.id,
            {"contentBlocks": _field(message, "content", []) or []},
        ),
    )


def stream_anthropic(
    messages: list[dict],
    model_id: str,
    web_search_mode: str | bool,
    reasoning_effort: str | None,
    thinking_enabled: bool | None,
    client: Any = None,
    provider_history: list[dict] | None = None,
):
    """Yield Claude text deltas followed by normalized provider usage."""
    model = get_model(model_id)
    effort = get_reasoning_effort(model, reasoning_effort)
    thinking_enabled = get_thinking_enabled(model, thinking_enabled)
    if model.provider != "anthropic":
        raise ModelError("MODEL_PROVIDER_MISMATCH")
    normalized = _normalize_messages(messages)
    provider_messages = _anthropic_messages(messages, provider_history)

    if client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise ModelError("API_KEY_MISSING")
        try:
            from anthropic import Anthropic

            client = Anthropic(api_key=api_key)
        except (ImportError, TypeError) as exc:
            raise ModelError("PROVIDER_SDK_UNAVAILABLE") from exc

    request_args = _anthropic_request_args(
        provider_messages, model, effort, thinking_enabled, web_search_mode
    )
    chunks: list[str] = []
    summary_chunks: dict[int, list[str]] = {}
    current_phase = "thinking"
    search_ids_by_index: dict[int, str] = {}
    search_input_chunks: dict[int, list[str]] = {}
    emitted_source_urls: set[str] = set()
    stream_thinking_tokens = None
    input_transformations = []
    try:
        with client.messages.stream(**request_args) as stream:
            for event in stream:
                stream_thinking_tokens = _anthropic_stream_thinking_tokens(event, stream_thinking_tokens)
                event_type = _field(event, "type")
                if event_type == "message_start":
                    input_transformations = (
                        _field(event, "input_transformations")
                        or _field(_field(event, "message"), "input_transformations")
                        or []
                    )
                if event_type == "content_block_start":
                    block = _field(event, "content_block")
                    block_type = _field(block, "type")
                    index = int(_field(event, "index", 0) or 0)
                    if block_type == "server_tool_use" and _field(block, "name") == "web_search":
                        search_id = str(_field(block, "id") or f"anthropic-search-{index}")
                        search_ids_by_index[index] = search_id
                        if current_phase != "searching":
                            current_phase = "searching"
                            yield {"type": "status", "phase": "searching"}
                        yield {"type": "search_started", "id": search_id}
                        query = _clean_optional_string(_field(_field(block, "input", {}) or {}, "query"))
                        if query:
                            yield {"type": "search_query", "id": search_id, "query": query}
                    elif block_type == "web_search_tool_result":
                        search_id = str(_field(block, "tool_use_id") or f"anthropic-search-{index}")
                        content = _field(block, "content", [])
                        for result in content if isinstance(content, list) else []:
                            if _field(result, "type") != "web_search_result":
                                continue
                            url = _clean_optional_string(_field(result, "url"))
                            if not url or url in emitted_source_urls:
                                continue
                            emitted_source_urls.add(url)
                            yield {
                                "type": "source_added",
                                "searchId": search_id,
                                "source": {
                                    "url": url,
                                    "title": _clean_optional_string(_field(result, "title")),
                                },
                            }
                if event_type == "content_block_stop":
                    index = int(_field(event, "index", 0) or 0)
                    partial = "".join(search_input_chunks.pop(index, []))
                    search_id = search_ids_by_index.get(index)
                    if partial and search_id:
                        try:
                            query = _clean_optional_string(json.loads(partial).get("query"))
                        except (json.JSONDecodeError, AttributeError):
                            query = None
                        if query:
                            yield {"type": "search_query", "id": search_id, "query": query}
                if event_type != "content_block_delta":
                    continue
                delta = _field(event, "delta")
                delta_type = _field(delta, "type")
                index = int(_field(event, "index", 0) or 0)
                if delta_type == "input_json_delta" and index in search_ids_by_index:
                    search_input_chunks.setdefault(index, []).append(
                        str(_field(delta, "partial_json") or "")
                    )
                    continue
                text = str(_field(delta, "text") or _field(delta, "thinking") or "")
                if delta_type == "thinking_delta" and text and model.adaptive_thinking:
                    if current_phase != "thinking":
                        current_phase = "thinking"
                        yield {"type": "status", "phase": "thinking"}
                    summary_chunks.setdefault(index, []).append(text)
                    yield {
                        "type": "reasoning_delta",
                        "id": f"anthropic:{index}",
                        "index": index,
                        "delta": text,
                    }
                elif delta_type == "text_delta" and text:
                    if current_phase != "writing":
                        current_phase = "writing"
                        yield {"type": "status", "phase": "writing"}
                    chunks.append(text)
                    yield {"type": "text_delta", "delta": text}
            final_message = stream.get_final_message()
    except ModelError:
        raise
    except Exception as exc:
        raise _provider_model_error(exc) from exc

    generation = _anthropic_result(
        final_message,
        normalized,
        model,
        effort,
        thinking_enabled,
        fallback_content="".join(chunks),
        stream_thinking_tokens=stream_thinking_tokens,
    )
    if model.thinking_prefix_binding:
        generation["providerState"]["payload"]["inputTransformations"] = _jsonable(
            input_transformations or _field(final_message, "input_transformations", []) or []
        )
    if not generation["reasoningSummary"]:
        generation["reasoningSummary"] = [
            {"index": index, "text": "".join(parts).strip()}
            for index, parts in sorted(summary_chunks.items())
            if "".join(parts).strip()
        ]
    yield {
        "type": "done",
        "generation": generation,
    }
