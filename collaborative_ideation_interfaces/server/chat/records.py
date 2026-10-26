"""Stored chat records and their public representation, independent of HTTP."""
from __future__ import annotations

from datetime import datetime, timezone
import uuid

from models.catalog import calculate_cost, get_model


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def public_conversation(
    conversation: dict,
    messages: list[dict] | None = None,
    call_timestamps: list[str] = (),
    message_stats: tuple[int, str] | None = None,
    is_generating: bool = False,
) -> dict:
    """Compose the client shape. `updatedAt` stays derived, never stored.

    `messages` is only carried on single-conversation responses. The list
    endpoint passes `message_stats` -- a count and the last timestamp from one
    indexed aggregate -- because serialising every message of every
    conversation is what put a 1.13 MB response and tens of milliseconds of
    GIL-held CPU on every page load.
    """
    message_count, last_message_at = message_stats or (
        len(messages or []),
        max((message["createdAt"] for message in messages), default=None) if messages else None,
    )
    timestamps = [conversation["createdAt"], *call_timestamps]
    if last_message_at:
        timestamps.append(last_message_at)
    settings = conversation["settings"]
    row = {
        "id": conversation["id"],
        "title": conversation["title"],
        "modelId": settings["modelId"],
        "reasoningEffort": settings["reasoningEffort"],
        "thinkingEnabled": settings["thinkingEnabled"],
        "createdAt": conversation["createdAt"],
        "updatedAt": max(timestamps),
        "messageCount": message_count,
        "isGenerating": is_generating,
    }
    if messages is not None:
        row["messages"] = messages
    return row


def usage_from_calls(calls: list[dict], credit_limit: float) -> dict:
    used = round(
        sum(model_call_cost(call) for call in calls if call["status"] == "complete"),
        8,
    )
    limit = float(credit_limit)
    return {"limitUsd": limit, "usedUsd": used, "remainingUsd": max(0.0, round(limit - used, 8))}


def participant_usage(transaction) -> dict:
    return usage_from_calls(transaction.usage_rows(), transaction.credit_limit())


def model_call_cost(call: dict) -> float:
    usage = call.get("usage")
    request_settings = call.get("request")
    if not usage or not request_settings:
        return 0.0
    model = get_model(request_settings["modelId"])
    return calculate_cost(
        model,
        int(usage.get("inputTokens", 0)),
        int(usage.get("cachedInputTokens", 0)),
        int(usage.get("outputTokens", 0)),
        int(usage.get("reasoningTokens", 0)),
        int(usage.get("webSearchRequests", 0)),
        int(usage.get("cacheWriteInputTokens", 0)),
    )


def public_model_call(call: dict, searches: list[dict], pricing_policy_id: str) -> dict:
    request_settings = call["request"]
    usage = call.get("usage", {})
    provider_result = call.get("providerResult", {})
    input_tokens = int(usage.get("inputTokens", 0))
    search_requests = int(usage.get("webSearchRequests", 0))
    cost = model_call_cost(call)
    model = get_model(request_settings["modelId"])
    public_searches = [
        {
            **{key: value for key, value in row.items() if key not in {"sources", "citations"}},
            "sources": [
                {key: value for key, value in source.items() if key != "encryptedContent"}
                for source in row["sources"]
            ],
            "citations": [
                {key: value for key, value in citation.items() if key != "encryptedIndex"}
                for citation in row["citations"]
            ],
        }
        for row in searches
    ]
    return {
        "id": call["id"],
        "modelId": request_settings["modelId"],
        "reasoningEffort": request_settings["reasoningEffort"],
        "thinkingEnabled": request_settings["thinkingEnabled"],
        "webSearchMode": request_settings.get(
            "webSearchMode",
            "required" if request_settings.get("webSearchEnabled") else "auto",
        ),
        "webSearchEnabled": request_settings.get("webSearchMode") == "required"
        if "webSearchMode" in request_settings
        else bool(request_settings.get("webSearchEnabled")),
        "inputTokens": input_tokens,
        "cachedInputTokens": int(usage.get("cachedInputTokens", 0)),
        "cacheWriteInputTokens": int(usage.get("cacheWriteInputTokens", 0)),
        "outputTokens": int(usage.get("outputTokens", 0)),
        "reasoningTokens": int(usage.get("reasoningTokens", 0)),
        "searchQueries": search_requests,
        "webSearchUsed": bool(search_requests),
        "researchCreditCostUsd": cost,
        "costUsd": cost,
        "researchPricingVersion": pricing_policy_id,
        "researchPricingBand": model.pricing_band(input_tokens).id,
        "pricingSnapshot": model.pricing_snapshot(input_tokens),
        "responseMode": "live",
        "providerResponseId": provider_result.get("responseId"),
        "providerStatus": provider_result.get("status"),
        "finishReason": provider_result.get("finishReason"),
        "usageEstimated": bool(usage.get("estimated", False)),
        "completionReason": call.get("completionReason", "completed"),
        "usageAccuracy": call.get(
            "usageAccuracy",
            "estimated" if usage.get("estimated", False) else "exact",
        ),
        "webSearches": public_searches,
    }


def persisted_web_search(call_id: str, message_id: str, trace: dict | None) -> dict | None:
    if not trace:
        return None
    source_ids: list[str] = []
    sources = []
    for source in trace.get("sources", []):
        source_id = str(uuid.uuid4())
        source_ids.append(source_id)
        sources.append({"id": source_id, **source})
    citations = []
    for citation in trace.get("citations", []):
        source_index = citation.get("sourceIndex")
        if not isinstance(source_index, int) or not 0 <= source_index < len(source_ids):
            continue
        citations.append(
            {
                "id": str(uuid.uuid4()),
                "messageId": message_id,
                "sourceId": source_ids[source_index],
                **{key: value for key, value in citation.items() if key != "sourceIndex"},
            }
        )
    executions = [
        {"id": str(uuid.uuid4()), **execution}
        for execution in trace.get("executions", [])
    ]
    return {
        "id": str(uuid.uuid4()),
        "modelCallId": call_id,
        "executions": executions,
        "sources": sources,
        "citations": citations,
    }


def public_message_citations(trace: dict | None, content: str, provider: str) -> list[dict]:
    if not trace:
        return []
    sources = trace.get("sources", [])
    positioned_source_indexes = {
        citation.get("sourceIndex")
        for citation in trace.get("citations", [])
        if citation.get("endIndex") is not None
    }
    result: list[dict] = []
    for citation in trace.get("citations", []):
        source_index = citation.get("sourceIndex")
        if not isinstance(source_index, int) or not 0 <= source_index < len(sources):
            continue
        source = sources[source_index]
        url = str(source.get("url") or "").strip()
        if not url:
            continue
        if provider == "anthropic":
            normalized = {
                "sourceNumber": source_index + 1,
                "url": url,
                "title": str(source.get("title") or "").strip() or None,
                "placement": "footer",
            }
            if normalized not in result:
                result.append(normalized)
            continue
        if citation.get("endIndex") is None and source_index in positioned_source_indexes:
            continue
        end_index = citation.get("endIndex")
        if end_index is None:
            end_index = len(content)
        normalized = {
            "sourceNumber": source_index + 1,
            "url": url,
            "title": str(source.get("title") or "").strip() or None,
            "startIndex": max(0, int(citation.get("startIndex", end_index))),
            "endIndex": max(0, int(end_index)),
            "indexUnit": str(citation.get("indexUnit") or "unicode_codepoint"),
            "placement": "inline",
        }
        cited_text = str(citation.get("citedText") or "").strip()
        if cited_text:
            normalized["citedText"] = cited_text
        if normalized not in result:
            result.append(normalized)
    return result
