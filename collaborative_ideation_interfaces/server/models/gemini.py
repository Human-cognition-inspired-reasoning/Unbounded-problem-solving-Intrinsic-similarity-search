"""Gemini request construction, history replay and streaming."""
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
    _jsonable,
    _new_search_trace,
    _normalize_messages,
    _provider_model_error,
    _provider_result,
    _provider_state,
    _summary_blocks,
)


def _gemini_interaction_input(
    messages: list[dict], provider_history: list[dict] | None
) -> list[dict]:
    history = _history_payloads(provider_history, "google")
    steps: list[dict] = []
    for message in messages:
        role = message.get("role")
        if role not in {"user", "assistant"}:
            continue
        if role == "assistant":
            stored_steps = history.get(str(message.get("id")), {}).get("steps")
            if isinstance(stored_steps, list) and stored_steps:
                steps.extend(stored_steps)
                continue
        content = str(message.get("content", ""))
        files = _attachment_blocks(message, 'google')
        if not content.strip() and not files:
            continue
        steps.append(
            {
                "type": "user_input" if role == "user" else "model_output",
                "content": [*([{"type": "text", "text": content}] if content.strip() else []), *files],
            }
        )
    if not any(step.get("type") == "user_input" for step in steps):
        raise ModelError("EMPTY_MESSAGE")
    return steps


def _gemini_step_text(step: object) -> str:
    return "".join(
        str(_field(content, "text") or "")
        for content in (_field(step, "content", []) or [])
        if _field(content, "type") == "text"
    )


def _gemini_interaction_text(steps: list[object]) -> str:
    model_outputs = [step for step in steps if _field(step, "type") == "model_output"]
    return _gemini_step_text(model_outputs[-1]) if model_outputs else ""


def _gemini_interaction_summary(steps: list[object]) -> list[dict]:
    texts: list[str] = []
    for step in steps:
        if _field(step, "type") != "thought":
            continue
        text = "".join(
            str(_field(content, "text") or "")
            for content in (_field(step, "summary", []) or [])
            if _field(content, "type") == "text"
        ).strip()
        if text:
            texts.append(text)
    return _summary_blocks(texts)


def _gemini_search_result_sources(result: object) -> list[dict]:
    """Extract public URL/title rows across evolving Interactions schemas."""
    pending = list(result) if isinstance(result, (list, tuple)) else [result]
    sources: list[dict] = []
    seen_urls: set[str] = set()
    while pending:
        row = pending.pop(0)
        if row is None or isinstance(row, (str, bytes)):
            continue
        url = _clean_optional_string(_field(row, "url") or _field(row, "uri"))
        if url and url not in seen_urls:
            seen_urls.add(url)
            sources.append(
                {
                    "url": url,
                    "title": _clean_optional_string(_field(row, "title")),
                }
            )
        for key in ("results", "items", "sources"):
            nested = _field(row, key)
            if isinstance(nested, (list, tuple)):
                pending.extend(nested)
    return sources


def _gemini_interaction_search_trace(steps: list[object]) -> dict | None:
    trace = _new_search_trace()
    for step in steps:
        step_type = _field(step, "type")
        if step_type == "google_search_call":
            arguments = _field(step, "arguments", {}) or {}
            queries = [
                str(query)
                for query in (_field(arguments, "queries", []) or [])
                if str(query).strip()
            ]
            trace["executions"].append(
                {
                    "providerSearchId": _clean_optional_string(_field(step, "id")),
                    "action": "search",
                    "queries": queries,
                    "status": "complete",
                }
            )
        elif step_type == "google_search_result":
            for source in _gemini_search_result_sources(_field(step, "result", []) or []):
                _add_search_source(trace, source)
        elif step_type == "model_output":
            for content in _field(step, "content", []) or []:
                for annotation in _field(content, "annotations", []) or []:
                    if _field(annotation, "type") != "url_citation":
                        continue
                    _add_search_citation(
                        trace,
                        {
                            "url": _field(annotation, "url"),
                            "title": _field(annotation, "title"),
                            "startIndex": _field(annotation, "start_index"),
                            "endIndex": _field(annotation, "end_index"),
                            "indexUnit": "utf8_byte",
                        },
                    )
    return trace if any(trace.values()) else None


def _gemini_search_count(usage: object, steps: list[object]) -> int:
    grounding_count = sum(
        int(_field(row, "count", 0) or 0)
        for row in (_field(usage, "grounding_tool_count", []) or [])
        if str(_field(row, "type") or "") == "google_search"
    )
    if grounding_count:
        return grounding_count
    return sum(1 for step in steps if _field(step, "type") == "google_search_call")


def _gemini_interaction_status(interaction: object) -> tuple[str, str | None]:
    status = str(_field(interaction, "status") or "completed")
    return ("completed", None) if status == "completed" else (status, status)


def stream_gemini(
    messages: list[dict],
    model_id: str,
    web_search_mode: str | bool,
    reasoning_effort: str | None,
    client: Any = None,
    provider_history: list[dict] | None = None,
):
    """Yield chronological Gemini Interactions API steps."""
    model = get_model(model_id)
    effort = get_reasoning_effort(model, reasoning_effort)
    web_search_mode = get_web_search_mode(web_search_mode)
    if model.provider != "google":
        raise ModelError("MODEL_PROVIDER_MISMATCH")
    normalized = _normalize_messages(messages)

    if client is None:
        api_key = os.getenv("GEMINI_API_KEY", "").strip() or os.getenv("GOOGLE_API_KEY", "").strip()
        if not api_key:
            raise ModelError("API_KEY_MISSING")
        try:
            from google import genai

            client = genai.Client(api_key=api_key)
        except (ImportError, TypeError) as exc:
            raise ModelError("PROVIDER_SDK_UNAVAILABLE") from exc

    interaction_input = _gemini_interaction_input(messages, provider_history)
    request_args: dict[str, Any] = {
        "model": model.id,
        "input": interaction_input,
        "store": False,
        "stream": True,
        "generation_config": {
            "thinking_level": effort,
            "thinking_summaries": "auto",
            # Interactions has no "require once, then allow text" mode for
            # server-side Google Search. `any` forces a tool call at every
            # model step and loops until the provider aborts, so both modes
            # must use the provider's supported automatic orchestration.
            "tool_choice": "auto",
        },
        "tools": [{"type": "google_search"}],
    }

    chunks: list[str] = []
    usage = None
    provider_response_id = None
    completed_interaction = None
    summary_chunks: dict[int, list[str]] = {}
    step_records: dict[int, dict] = {}
    step_types: dict[int, str] = {}
    emitted_queries: set[tuple[int, str]] = set()
    emitted_source_urls: set[str] = set()
    writing_started = False
    active_search_id: str | None = None
    try:
        for event in client.interactions.create(**request_args):
            event_type = _field(event, "event_type")
            if event_type == "interaction.created":
                interaction = _field(event, "interaction")
                provider_response_id = _field(interaction, "id") or provider_response_id
            elif event_type == "step.start":
                index = int(_field(event, "index", 0) or 0)
                step = _field(event, "step")
                step_type = str(_field(step, "type") or "")
                step_types[index] = step_type
                step_records[index] = _jsonable(step)
                if step_type == "google_search_call":
                    yield {"type": "status", "phase": "searching"}
                    search_id = str(_field(step, "id") or f"gemini-search-{index}")
                    active_search_id = search_id
                    yield {"type": "search_started", "id": search_id}
                    for query in _field(_field(step, "arguments", {}) or {}, "queries", []) or []:
                        query_text = str(query).strip()
                        if query_text and (index, query_text) not in emitted_queries:
                            emitted_queries.add((index, query_text))
                            yield {"type": "search_query", "id": search_id, "query": query_text}
                elif step_type == "google_search_result":
                    search_id = str(
                        _field(step, "call_id") or active_search_id or f"gemini-search-{index}"
                    )
                    for source in _gemini_search_result_sources(
                        _field(step, "result", []) or []
                    ):
                        if source["url"] in emitted_source_urls:
                            continue
                        emitted_source_urls.add(source["url"])
                        yield {
                            "type": "source_added",
                            "searchId": search_id,
                            "source": source,
                        }
                elif step_type == "model_output" and not writing_started:
                    writing_started = True
                    yield {"type": "status", "phase": "writing"}
            elif event_type == "step.delta":
                index = int(_field(event, "index", 0) or 0)
                delta = _field(event, "delta")
                delta_type = str(_field(delta, "type") or "")
                record = step_records.setdefault(index, {"type": step_types.get(index, "UNKNOWN")})
                if delta_type == "thought_summary":
                    content = _field(delta, "content")
                    text = str(_field(content, "text") or "")
                    if text:
                        summary_chunks.setdefault(index, []).append(text)
                        summary = record.setdefault("summary", [{"type": "text", "text": ""}])
                        if not summary:
                            summary.append({"type": "text", "text": ""})
                        summary[-1]["text"] = str(summary[-1].get("text", "")) + text
                        yield {
                            "type": "reasoning_delta",
                            "id": f"gemini:{index}",
                            "index": index,
                            "delta": text,
                        }
                elif delta_type == "thought_signature":
                    record["signature"] = _field(delta, "signature")
                elif delta_type == "text" and step_types.get(index) == "model_output":
                    text = str(_field(delta, "text") or "")
                    if text:
                        content = record.setdefault("content", [{"type": "text", "text": ""}])
                        if not content:
                            content.append({"type": "text", "text": ""})
                        content[-1]["text"] = str(content[-1].get("text", "")) + text
                        chunks.append(text)
                        yield {"type": "text_delta", "delta": text}
                elif delta_type == "google_search_call":
                    record["arguments"] = _jsonable(_field(delta, "arguments", {}) or {})
                    if _field(delta, "signature"):
                        record["signature"] = _field(delta, "signature")
                    search_id = str(record.get("id") or f"gemini-search-{index}")
                    for query in _field(_field(delta, "arguments", {}) or {}, "queries", []) or []:
                        query_text = str(query).strip()
                        if query_text and (index, query_text) not in emitted_queries:
                            emitted_queries.add((index, query_text))
                            yield {"type": "search_query", "id": search_id, "query": query_text}
                elif delta_type == "google_search_result":
                    result = _field(delta, "result", []) or []
                    record["result"] = _jsonable(result)
                    if _field(delta, "signature"):
                        record["signature"] = _field(delta, "signature")
                    search_id = str(
                        record.get("call_id") or active_search_id or f"gemini-search-{index}"
                    )
                    for source in _gemini_search_result_sources(result):
                        if source["url"] in emitted_source_urls:
                            continue
                        emitted_source_urls.add(source["url"])
                        yield {
                            "type": "source_added",
                            "searchId": search_id,
                            "source": source,
                        }
                elif delta_type == "text_annotation_delta":
                    annotations = list(_field(delta, "annotations", []) or [])
                    content = record.setdefault("content", [{"type": "text", "text": ""}])
                    if not content:
                        content.append({"type": "text", "text": ""})
                    content[-1].setdefault("annotations", []).extend(_jsonable(annotations))
                    for annotation in annotations:
                        url = _clean_optional_string(_field(annotation, "url"))
                        if _field(annotation, "type") != "url_citation" or not url:
                            continue
                        title = _clean_optional_string(_field(annotation, "title"))
                        if url not in emitted_source_urls:
                            emitted_source_urls.add(url)
                            yield {
                                "type": "source_added",
                                "searchId": active_search_id or f"gemini-search-{index}",
                                "source": {"url": url, "title": title},
                            }
                        yield {
                            "type": "citation_added",
                            "citation": {
                                "url": url,
                                "title": title,
                                "startIndex": int(_field(annotation, "start_index", 0) or 0),
                                "endIndex": int(_field(annotation, "end_index", 0) or 0),
                                "indexUnit": "utf8_byte",
                                "placement": "inline",
                            },
                        }
                metadata = _field(event, "metadata")
                usage = _field(metadata, "total_usage") or usage
            elif event_type == "step.stop":
                usage = _field(event, "usage") or usage
            elif event_type == "interaction.completed":
                completed_interaction = _field(event, "interaction")
                provider_response_id = _field(completed_interaction, "id") or provider_response_id
                usage = _field(completed_interaction, "usage") or usage
            elif event_type == "error":
                raise _provider_model_error(event)
    except ModelError:
        raise
    except Exception as exc:
        raise _provider_model_error(exc) from exc

    completed_steps = list(_field(completed_interaction, "steps", []) or [])
    steps: list[object] = completed_steps or [step_records[index] for index in sorted(step_records)]
    final_content = "".join(chunks) or _gemini_interaction_text(steps)
    usage_estimated = usage is None
    provider_status, finish_reason = _gemini_interaction_status(completed_interaction or {})
    final_summary = _gemini_interaction_summary(steps) or [
        {"index": order, "text": "".join(summary_chunks[index]).strip()}
        for order, index in enumerate(summary_chunks)
        if "".join(summary_chunks[index]).strip()
    ]
    yield {
        "type": "done",
        "generation": _provider_result(
            content=final_content,
            model=model,
            effort=effort,
            input_tokens=_int_attr(usage, "total_input_tokens") if usage else _estimated_input_tokens(normalized),
            cached_input_tokens=_int_attr(usage, "total_cached_tokens"),
            output_tokens=_int_attr(usage, "total_output_tokens") if usage else estimate_tokens(final_content),
            reasoning_tokens=_int_attr(usage, "total_thought_tokens"),
            search_queries=_gemini_search_count(usage, steps),
            provider_response_id=provider_response_id,
            provider_status=provider_status,
            finish_reason=finish_reason,
            usage_estimated=usage_estimated,
            web_search_trace=_gemini_interaction_search_trace(steps),
            reasoning_summary=final_summary,
            provider_state=_provider_state(
                "google",
                model.id,
                {"steps": [step for step in steps if _field(step, "type") != "user_input"]},
            ),
        ),
    }
