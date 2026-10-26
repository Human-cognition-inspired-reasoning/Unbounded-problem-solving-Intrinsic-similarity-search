"""OpenAI request construction, history replay and streaming."""
from __future__ import annotations

import os
from typing import Any

from models.catalog import (
    ModelError,
    estimate_tokens,
    get_model,
    get_reasoning_effort,
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
    _new_search_trace,
    _normalize_messages,
    _provider_model_error,
    _provider_result,
    _provider_state,
    _summary_blocks,
)


def _openai_search_trace(response: object) -> dict | None:
    trace = _new_search_trace()
    text_offset = 0
    for item in _field(response, "output", []) or []:
        item_type = _field(item, "type")
        if item_type == "web_search_call":
            action = _field(item, "action")
            action_type = _clean_optional_string(_field(action, "type")) or "search"
            query_values = list(_field(action, "queries", None) or [])
            single_query = _clean_optional_string(_field(action, "query"))
            if single_query and single_query not in query_values:
                query_values.append(single_query)
            execution: dict[str, Any] = {
                "providerSearchId": _clean_optional_string(_field(item, "id")),
                "action": action_type,
                "queries": [str(query) for query in query_values if str(query).strip()],
                "status": _clean_optional_string(_field(item, "status")) or "complete",
            }
            for source in _field(action, "sources", []) or []:
                _add_search_source(
                    trace,
                    {
                        "url": _field(source, "url"),
                        "title": _field(source, "title"),
                    },
                )
            trace["executions"].append(execution)
        if item_type == "message":
            for content_block in _field(item, "content", []) or []:
                for annotation in _field(content_block, "annotations", []) or []:
                    if _field(annotation, "type") != "url_citation":
                        continue
                    _add_search_citation(
                        trace,
                        {
                            "url": _field(annotation, "url"),
                            "title": _field(annotation, "title"),
                            "startIndex": text_offset + int(_field(annotation, "start_index", 0) or 0),
                            "endIndex": text_offset + int(_field(annotation, "end_index", 0) or 0),
                            "indexUnit": "unicode_codepoint",
                        },
                    )
                if _field(content_block, "type") == "output_text":
                    text_offset += len(str(_field(content_block, "text", "") or ""))
    return trace if any(trace.values()) else None


def _openai_replayable_items(items: list) -> list:
    """Keep only the output items a stateless follow-up request can accept.

    With `store: False` the provider holds no server-side copy of the reasoning,
    so a reasoning item is replayable only while it carries its encrypted payload.
    It must also be followed by the item it produced, which an `incomplete`
    response cannot guarantee: truncation can leave a trailing reasoning item.
    """
    replayable = [
        item
        for item in items
        if _field(item, "type") != "reasoning"
        or _clean_optional_string(_field(item, "encrypted_content"))
    ]
    while replayable and _field(replayable[-1], "type") == "reasoning":
        replayable.pop()
    return replayable


def _openai_input(messages: list[dict], provider_history: list[dict] | None) -> list[dict]:
    history = _history_payloads(provider_history, "openai")
    result: list[dict] = []
    for message in messages:
        if message.get("role") not in {"user", "assistant"}:
            continue
        content = str(message.get("content", ""))
        if message.get("role") == "assistant":
            output_items = history.get(str(message.get("id")), {}).get("outputItems")
            replayable = _openai_replayable_items(output_items) if isinstance(output_items, list) else []
            if replayable:
                result.extend(replayable)
                continue
            # Public text parts retain phase even when opaque state cannot be
            # reused (for example after switching to another OpenAI model).
            parts = [p for p in message.get("responseParts", []) or [] if p.get("type") == "text"]
            if parts and any(p.get("phase") in {"commentary", "final_answer"} for p in parts):
                for part in parts:
                    row = {"role": "assistant", "content": part["text"]}
                    if part.get("phase") in {"commentary", "final_answer"}:
                        row["phase"] = part["phase"]
                    result.append(row)
                continue
        files = _attachment_blocks(message, 'openai')
        if files:
            result.append({'role': message['role'], 'content': [
                *([{'type': 'input_text', 'text': content}] if content.strip() else []), *files,
            ]})
        elif content.strip():
            result.append({"role": message["role"], "content": content})
    if not any(row.get("role") == "user" for row in result if isinstance(row, dict)):
        raise ModelError("EMPTY_MESSAGE")
    return result


def _openai_reasoning_summary(response: object) -> list[dict]:
    texts: list[object] = []
    for item in _field(response, "output", []) or []:
        if _field(item, "type") != "reasoning":
            continue
        for summary in _field(item, "summary", []) or []:
            texts.append(_field(summary, "text"))
    return _summary_blocks(texts)


def _openai_response_status(response: object) -> tuple[str, str | None]:
    status = str(getattr(response, "status", None) or "completed")
    details = getattr(response, "incomplete_details", None)
    reason = str(getattr(details, "reason", None) or "") or None
    return status, reason


def stream_openai(
    messages: list[dict],
    model_id: str,
    web_search_mode: str | bool,
    reasoning_effort: str | None,
    client: Any = None,
    provider_history: list[dict] | None = None,
    cache_key: str | None = None,
):
    """Yield OpenAI text deltas followed by one normalized result event."""
    model = get_model(model_id)
    effort = get_reasoning_effort(model, reasoning_effort)
    web_search_mode = get_web_search_mode(web_search_mode)
    if model.provider != "openai":
        raise ModelError("MODEL_PROVIDER_MISMATCH")
    normalized = _normalize_messages(messages)
    provider_input = _openai_input(messages, provider_history)

    if client is None:
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise ModelError("API_KEY_MISSING")
        try:
            from openai import OpenAI

            client = OpenAI(api_key=api_key)
        except (ImportError, TypeError) as exc:
            raise ModelError("PROVIDER_SDK_UNAVAILABLE") from exc

    request_args: dict[str, Any] = {
        "model": model.id,
        "input": provider_input,
        "reasoning": {"effort": effort, "summary": "auto", "context": "all_turns"},
        "store": False,
    }
    request_args["tools"] = [{"type": "web_search"}]
    request_args["tool_choice"] = "required" if web_search_mode == "required" else "auto"
    # `store: False` keeps the transcript off the provider, so the encrypted
    # reasoning payload is the only way a later turn can continue the reasoning.
    # Stateless responses populate it by default; asking for it explicitly keeps
    # that guarantee independent of the provider's default.
    request_args["include"] = [
        "web_search_call.action.sources",
        "reasoning.encrypted_content",
    ]
    # Caching is automatic, but every turn of one conversation repeats the same
    # long prefix. A stable key routes those turns to the backend that already
    # holds it, which is what turns an available cache into an actual hit.
    if cache_key:
        request_args["prompt_cache_key"] = cache_key

    chunks: list[str] = []
    summary_chunks: dict[str, list[str]] = {}
    summary_order: dict[str, int] = {}
    emitted_source_urls: set[str] = set()
    response = None
    current_phase = "thinking"
    active_search_id: str | None = None
    message_phases: dict[str, str] = {}
    text_offsets: dict[tuple, int] = {}
    text_length = 0

    def message_metadata(item):
        if _field(item, "type") != "message":
            return None
        item_id = _field(item, "id")
        phase = _field(item, "phase")
        if item_id and phase in {"commentary", "final_answer"} and message_phases.get(item_id) != phase:
            message_phases[item_id] = phase
            return {"type": "text_metadata", "messageId": item_id, "phase": phase}
        return None

    try:
        with client.responses.stream(**request_args) as stream:
            for event in stream:
                event_type = getattr(event, "type", None)
                if event_type == "response.output_item.added":
                    metadata = message_metadata(_field(event, "item"))
                    if metadata:
                        yield metadata
                elif event_type == "response.reasoning_summary_text.delta":
                    delta = str(getattr(event, "delta", "") or "")
                    if delta:
                        if current_phase != "thinking":
                            current_phase = "thinking"
                            yield {"type": "status", "phase": "thinking"}
                        output_index = int(getattr(event, "output_index", 0) or 0)
                        summary_index = int(getattr(event, "summary_index", 0) or 0)
                        item_id = str(getattr(event, "item_id", "") or f"output-{output_index}")
                        block_id = f"openai:{item_id}:{summary_index}"
                        if block_id not in summary_order:
                            summary_order[block_id] = len(summary_order)
                        summary_chunks.setdefault(block_id, []).append(delta)
                        yield {
                            "type": "reasoning_delta",
                            "id": block_id,
                            "index": summary_order[block_id],
                            "delta": delta,
                        }
                elif event_type == "response.output_text.delta":
                    delta = str(getattr(event, "delta", "") or "")
                    if delta:
                        if current_phase != "writing":
                            current_phase = "writing"
                            yield {"type": "status", "phase": "writing"}
                        chunks.append(delta)
                        item_id = _field(event, "item_id")
                        content_index = int(_field(event, "content_index", 0) or 0)
                        text_offsets.setdefault((item_id, content_index), text_length)
                        text_length += len(delta)
                        text_event = {"type": "text_delta", "delta": delta}
                        if item_id:
                            text_event.update(messageId=item_id, contentIndex=content_index)
                            if item_id in message_phases:
                                text_event["phase"] = message_phases[item_id]
                        yield text_event
                elif event_type and "web_search_call" in event_type:
                    if event_type.endswith(("in_progress", "searching")):
                        active_search_id = str(
                            getattr(event, "item_id", "") or active_search_id or "openai-search"
                        )
                    if current_phase != "searching" and event_type.endswith(("in_progress", "searching")):
                        current_phase = "searching"
                        yield {"type": "status", "phase": "searching"}
                    if event_type.endswith(("in_progress", "searching")):
                        yield {
                            "type": "search_started",
                            "id": active_search_id,
                        }
                elif event_type == "response.output_item.done":
                    item = getattr(event, "item", None)
                    metadata = message_metadata(item)
                    if metadata:
                        yield metadata
                    if _field(item, "type") == "web_search_call":
                        search_id = str(_field(item, "id") or "openai-search")
                        active_search_id = search_id
                        action = _field(item, "action")
                        queries = list(_field(action, "queries", None) or [])
                        single_query = _clean_optional_string(_field(action, "query"))
                        if single_query and single_query not in queries:
                            queries.append(single_query)
                        for query in queries:
                            if str(query).strip():
                                yield {
                                    "type": "search_query",
                                    "id": search_id,
                                    "query": str(query),
                                }
                        for source in _field(action, "sources", []) or []:
                            url = _clean_optional_string(_field(source, "url"))
                            if not url or url in emitted_source_urls:
                                continue
                            emitted_source_urls.add(url)
                            yield {
                                "type": "source_added",
                                "searchId": search_id,
                                "source": {
                                    "url": url,
                                    "title": _clean_optional_string(_field(source, "title")),
                                },
                            }
                elif event_type == "response.output_text.annotation.added":
                    annotation = getattr(event, "annotation", None)
                    url = _clean_optional_string(_field(annotation, "url"))
                    if _field(annotation, "type") == "url_citation" and url:
                        offset = text_offsets.get((
                            _field(event, "item_id"), int(_field(event, "content_index", 0) or 0),
                        ), 0)
                        title = _clean_optional_string(_field(annotation, "title"))
                        if url not in emitted_source_urls:
                            emitted_source_urls.add(url)
                            yield {
                                "type": "source_added",
                                "searchId": active_search_id or "openai-search",
                                "source": {"url": url, "title": title},
                            }
                        yield {
                            "type": "citation_added",
                            "citation": {
                                "url": url,
                                "title": title,
                                "startIndex": offset + int(_field(annotation, "start_index", 0) or 0),
                                "endIndex": offset + int(_field(annotation, "end_index", 0) or 0),
                                "indexUnit": "unicode_codepoint",
                                "placement": "inline",
                            },
                        }
                elif event_type in {"response.completed", "response.incomplete"}:
                    response = getattr(event, "response", None)
                elif event_type == "response.failed":
                    failed_response = getattr(event, "response", None) or event
                    raise _provider_model_error(failed_response)
    except ModelError:
        raise
    except Exception as exc:
        raise _provider_model_error(exc) from exc

    if response is None:
        raise ModelError("EMPTY_PROVIDER_RESPONSE")

    for item in _field(response, "output", []) or []:
        metadata = message_metadata(item)
        if metadata:
            yield metadata

    final_content = str(getattr(response, "output_text", "") or "") or "".join(chunks)
    usage = getattr(response, "usage", None)
    usage_estimated = usage is None
    input_tokens = _int_attr(usage, "input_tokens") if usage else _estimated_input_tokens(normalized)
    cached_input_tokens = _int_attr(getattr(usage, "input_tokens_details", None), "cached_tokens")
    cache_write_input_tokens = _int_attr(
        getattr(usage, "input_tokens_details", None), "cache_write_tokens"
    )
    total_output_tokens = _int_attr(usage, "output_tokens")
    reasoning_tokens = _int_attr(getattr(usage, "output_tokens_details", None), "reasoning_tokens")
    visible_output_tokens = max(0, total_output_tokens - reasoning_tokens) if usage else estimate_tokens(final_content)
    search_queries = sum(
        1 for item in (getattr(response, "output", None) or []) if getattr(item, "type", None) == "web_search_call"
    )
    provider_status, finish_reason = _openai_response_status(response)
    final_summary = _openai_reasoning_summary(response) or [
        {"index": summary_order[block_id], "text": "".join(parts).strip()}
        for block_id, parts in summary_chunks.items()
        if "".join(parts).strip()
    ]
    yield {
        "type": "done",
        "generation": _provider_result(
            content=final_content,
            model=model,
            effort=effort,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            cache_write_input_tokens=cache_write_input_tokens,
            output_tokens=visible_output_tokens,
            reasoning_tokens=reasoning_tokens,
            search_queries=search_queries,
            provider_response_id=getattr(response, "id", None),
            provider_status=provider_status,
            finish_reason=finish_reason,
            usage_estimated=usage_estimated,
            web_search_trace=_openai_search_trace(response),
            reasoning_summary=final_summary,
            provider_state=_provider_state(
                "openai",
                model.id,
                {"outputItems": _field(response, "output", []) or []},
            ),
        ),
    }
