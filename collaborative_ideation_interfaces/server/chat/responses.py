"""Assemble ordered response parts during streaming and at completion."""
from __future__ import annotations

import copy

from models.catalog import ModelSpec, calculate_cost, estimate_tokens


class StreamingGenerationAccumulator:
    """Collect provider deltas and maintain an intentionally soft usage estimate."""

    def __init__(self, messages: list[dict], model: ModelSpec) -> None:
        self.model = model
        self.input_tokens = sum(
            estimate_tokens(content)
            for message in messages
            if (content := str(message.get("content") or "")).strip()
        )
        self.input_tokens += sum(
            int(file.get('estimatedTokens', 0))
            for message in messages for attachment in message.get('attachments', [])
            for file in attachment.get('files', [])
        )
        self.output_tokens = 0
        self.reasoning_tokens = 0
        self.content_chunks: list[str] = []
        self.reasoning_blocks: dict[str, dict] = {}
        self.response_parts: list[dict] = []
        self.searches: dict[str, dict] = {}
        self.sources: list[dict] = []
        self.source_indexes: dict[str, int] = {}
        self.citations: list[dict] = []
        self.generation: dict | None = None

    @property
    def estimated_cost_usd(self) -> float:
        return calculate_cost(
            self.model,
            self.input_tokens,
            0,
            self.output_tokens,
            self.reasoning_tokens,
            len(self.searches),
            0,
        )

    def _append_text(self, delta: str, metadata: dict) -> None:
        last = self.response_parts[-1] if self.response_parts else {}
        if last.get("type") == "text" and all(
            last.get(key) == metadata.get(key) for key in ("messageId", "contentIndex", "phase")
        ):
            self.response_parts[-1]["text"] += delta
        else:
            self.response_parts.append(
                {"id": f"text:{len(self.response_parts)}", "type": "text", "text": delta, **metadata}
            )

    def _append_reasoning(self, stream_id: str | None, index: int, delta: str) -> None:
        normalized_id = stream_id or f"reasoning:{index}"
        block = self.reasoning_blocks.get(normalized_id)
        if block is None:
            block = {
                "index": len(self.reasoning_blocks),
                "text": "",
            }
            self.reasoning_blocks[normalized_id] = block
        block["text"] += delta

        if (
            self.response_parts
            and self.response_parts[-1].get("type") == "reasoning"
            and self.response_parts[-1].get("streamId") == normalized_id
        ):
            self.response_parts[-1]["text"] += delta
        else:
            self.response_parts.append(
                {
                    "id": f"{normalized_id}:segment:{len(self.response_parts)}",
                    "streamId": normalized_id,
                    "type": "reasoning",
                    "index": index,
                    "text": delta,
                }
            )

    def _search(self, search_id: str) -> dict:
        normalized_id = str(search_id or "search")
        search = self.searches.get(normalized_id)
        if search is None:
            search = {
                "providerSearchId": normalized_id,
                "action": "search",
                "queries": [],
                "status": "started",
            }
            self.searches[normalized_id] = search
        if not any(
            part.get("type") == "search" and part.get("id") == normalized_id
            for part in self.response_parts
        ):
            self.response_parts.append(
                {"id": normalized_id, "type": "search", "sources": []}
            )
        return search

    def _add_source(self, search_id: str, source: dict) -> int | None:
        url = str(source.get("url") or "").strip()
        if not url:
            return None
        title = str(source.get("title") or "").strip() or None
        source_index = self.source_indexes.get(url)
        if source_index is None:
            source_index = len(self.sources)
            self.source_indexes[url] = source_index
            self.sources.append({"url": url, "title": title})
        elif title and not self.sources[source_index].get("title"):
            self.sources[source_index]["title"] = title

        self._search(search_id)
        search_part = next(
            part
            for part in self.response_parts
            if part.get("type") == "search" and part.get("id") == str(search_id or "search")
        )
        existing = next((row for row in search_part["sources"] if row["url"] == url), None)
        if existing is None:
            search_part["sources"].append({"url": url, "title": title})
        elif title and not existing.get("title"):
            existing["title"] = title
        return source_index

    def observe(self, event: dict) -> None:
        event_type = event.get("type")
        if event_type == "text_delta":
            delta = str(event.get("delta") or "")
            if delta:
                self.content_chunks.append(delta)
                self.output_tokens = estimate_tokens("".join(self.content_chunks))
                self._append_text(delta, {
                    key: event[key] for key in ("messageId", "contentIndex", "phase") if key in event
                })
        elif event_type == "text_metadata":
            for part in self.response_parts:
                if part.get("type") == "text" and part.get("messageId") == event.get("messageId"):
                    part["phase"] = event["phase"]
        elif event_type == "reasoning_delta":
            delta = str(event.get("delta") or "")
            if delta:
                self._append_reasoning(
                    event.get("id"),
                    int(event.get("index", 0)),
                    delta,
                )
                visible_reasoning = "".join(
                    block["text"] for block in self.reasoning_blocks.values()
                )
                self.reasoning_tokens = estimate_tokens(visible_reasoning)
        elif event_type == "search_started":
            self._search(str(event.get("id") or "search"))
        elif event_type == "search_query":
            search = self._search(str(event.get("id") or "search"))
            query = str(event.get("query") or "").strip()
            if query and query not in search["queries"]:
                search["queries"].append(query)
        elif event_type == "source_added":
            self._add_source(
                str(event.get("searchId") or "search"),
                event.get("source") or {},
            )
        elif event_type == "citation_added":
            citation = event.get("citation") or {}
            url = str(citation.get("url") or "").strip()
            source_index = self.source_indexes.get(url) if url else None
            if url and source_index is None:
                source_index = len(self.sources)
                self.source_indexes[url] = source_index
                self.sources.append(
                    {
                        "url": url,
                        "title": str(citation.get("title") or "").strip() or None,
                    }
                )
            if source_index is not None:
                normalized = {
                    "sourceIndex": source_index,
                    **{
                        key: citation[key]
                        for key in (
                            "startIndex",
                            "endIndex",
                            "indexUnit",
                            "citedText",
                        )
                        if citation.get(key) is not None
                    },
                }
                if normalized not in self.citations:
                    self.citations.append(normalized)
        elif event_type == "done":
            self.generation = event.get("generation")

    def partial_generation(self, completion_reason: str) -> dict:
        trace = None
        if self.searches or self.sources or self.citations:
            trace = {
                "executions": [copy.deepcopy(row) for row in self.searches.values()],
                "sources": copy.deepcopy(self.sources),
                "citations": copy.deepcopy(self.citations),
            }
        return {
            "content": "".join(self.content_chunks),
            "inputTokens": self.input_tokens,
            "cachedInputTokens": 0,
            "cacheWriteInputTokens": 0,
            "outputTokens": self.output_tokens,
            # Interrupted calls intentionally omit unobservable hidden reasoning.
            "reasoningTokens": self.reasoning_tokens,
            "searchQueries": len(self.searches),
            "providerResponseId": None,
            "providerStatus": "incomplete",
            "finishReason": completion_reason,
            "usageEstimated": True,
            "webSearchTrace": trace,
            "reasoningSummary": [
                copy.deepcopy(block)
                for block in self.reasoning_blocks.values()
                if block["text"]
            ],
            "responseParts": copy.deepcopy(self.response_parts),
        }


def append_response_text(parts: list[dict], delta: str) -> None:
    if not delta:
        return
    if parts and parts[-1].get("type") == "text":
        parts[-1]["text"] += delta
        return
    parts.append({"id": f"text:{len(parts)}", "type": "text", "text": delta})


def finalized_response_parts(parts: list[dict] | None, generation: dict) -> list[dict]:
    result = copy.deepcopy(parts or [])
    if not any(part.get("type") == "reasoning" for part in result):
        fallback_reasoning = [
            {
                "id": f"reasoning:final:{position}",
                "type": "reasoning",
                "index": int(block.get("index", position)),
                "text": str(block.get("text") or ""),
            }
            for position, block in enumerate(generation.get("reasoningSummary", []))
            if str(block.get("text") or "").strip()
        ]
        result = fallback_reasoning + result

    if not any(part.get("type") == "text" for part in result):
        state = generation.get("providerState") or {}
        # Some streams provide text only in the completed response. Retain the
        # OpenAI message boundaries and phases on that fallback path too.
        if state.get("provider") == "openai":
            for item in state.get("payload", {}).get("outputItems", []):
                if item.get("type") != "message":
                    continue
                for index, block in enumerate(item.get("content", [])):
                    if block.get("type") != "output_text" or not block.get("text"):
                        continue
                    part = {"id": f"text:{len(result)}", "type": "text", "text": block["text"], "contentIndex": index}
                    if item.get("id"):
                        part["messageId"] = item["id"]
                    if item.get("phase") in {"commentary", "final_answer"}:
                        part["phase"] = item["phase"]
                    result.append(part)
        if not any(part.get("type") == "text" for part in result):
            append_response_text(result, str(generation.get("content") or ""))

    trace = generation.get("webSearchTrace") or {}
    public_sources = [
        {
            "url": str(source.get("url") or "").strip(),
            "title": str(source.get("title") or "").strip() or None,
        }
        for source in trace.get("sources", [])
        if str(source.get("url") or "").strip()
    ]
    represented_urls = {
        source.get("url")
        for part in result
        if part.get("type") == "search"
        for source in part.get("sources", [])
    }
    missing_sources = [source for source in public_sources if source["url"] not in represented_urls]
    if missing_sources:
        searches = [part for part in result if part.get("type") == "search"]
        if searches:
            target = searches[-1]
        else:
            target = {"id": "search:final", "type": "search", "sources": []}
            first_text = next(
                (index for index, part in enumerate(result) if part.get("type") == "text"),
                len(result),
            )
            result.insert(first_text, target)
        target["sources"].extend(missing_sources)

    cleaned: list[dict] = []
    for part in result:
        part_type = part.get("type")
        if part_type in {"text", "reasoning"} and not str(part.get("text") or ""):
            continue
        if part_type == "search" and not part.get("sources"):
            continue
        cleaned.append({key: value for key, value in part.items() if key != "streamId"})
    return cleaned
