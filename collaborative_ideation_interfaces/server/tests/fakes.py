"""Deterministic provider doubles shared by unit and API tests."""
from types import SimpleNamespace

from models.catalog import calculate_cost, get_model

def completed_generation(events):
    """Consume the same stream as the app and require exactly one terminal result."""
    events = list(events)
    results = [event['generation'] for event in events if event['type'] == 'done']
    assert len(results) == 1 and events[-1]['type'] == 'done'
    return results[0]


class FakeOpenAIStream:
    def __init__(self, response, terminal_type="response.completed"):
        self.response = response
        self.terminal_type = terminal_type

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def __iter__(self):
        return iter(
            [
                SimpleNamespace(
                    type="response.reasoning_summary_text.delta",
                    item_id="reasoning-item-a",
                    output_index=0,
                    summary_index=0,
                    delta="문제를 분석하고 ",
                ),
                SimpleNamespace(
                    type="response.reasoning_summary_text.delta",
                    item_id="reasoning-item-b",
                    output_index=1,
                    summary_index=0,
                    delta="답변을 구성했습니다.",
                ),
                SimpleNamespace(type="response.output_text.delta", delta="스트리밍 "),
                SimpleNamespace(type="response.output_text.delta", delta="OpenAI 응답"),
                SimpleNamespace(
                    type="response.output_text.annotation.added",
                    annotation=SimpleNamespace(
                        type="url_citation",
                        url="https://example.com/openai-stream-source",
                        title="OpenAI stream source",
                        start_index=0,
                        end_index=10,
                    ),
                ),
                SimpleNamespace(type=self.terminal_type, response=self.response),
            ]
        )

    def get_final_response(self):
        return self.response


class FakeOpenAIResponses:
    def __init__(self, stream_status="completed", final_response=None):
        self.request = None
        self.stream_status = stream_status
        self.final_response = final_response

    @staticmethod
    def response():
        citation = SimpleNamespace(
            type="url_citation",
            url="https://example.com/openai-source",
            title="OpenAI source",
            start_index=0,
            end_index=10,
        )
        return SimpleNamespace(
            id="resp-test",
            status="completed",
            incomplete_details=None,
            output_text="실제 OpenAI 형식의 응답",
            output=[
                SimpleNamespace(
                    type="reasoning",
                    id="reasoning-openai-1",
                    encrypted_content="encrypted-openai-state",
                    summary=[SimpleNamespace(type="summary_text", text="문제를 분석하고 답변을 구성했습니다.")],
                ),
                SimpleNamespace(
                    type="web_search_call",
                    id="search-openai-1",
                    status="completed",
                    action=SimpleNamespace(
                        type="search",
                        query="openai research query",
                        queries=["openai research query"],
                        sources=[SimpleNamespace(url="https://example.com/openai-source")],
                    ),
                ),
                SimpleNamespace(
                    type="message",
                    content=[SimpleNamespace(type="output_text", annotations=[citation])],
                ),
            ],
            usage=SimpleNamespace(
                input_tokens=100,
                input_tokens_details=SimpleNamespace(cached_tokens=20, cache_write_tokens=10),
                output_tokens=60,
                output_tokens_details=SimpleNamespace(reasoning_tokens=15),
            ),
        )

    def stream(self, **kwargs):
        self.request = kwargs
        response = self.final_response or SimpleNamespace(
            id="resp-stream-test",
            status=self.stream_status,
            incomplete_details=(
                SimpleNamespace(reason="max_tokens")
                if self.stream_status == "incomplete"
                else None
            ),
            output_text="스트리밍 OpenAI 응답",
            output=[
                SimpleNamespace(
                    type="reasoning",
                    id="reasoning-openai-stream",
                    encrypted_content="encrypted-openai-stream-state",
                    summary=[SimpleNamespace(type="summary_text", text="문제를 분석하고 답변을 구성했습니다.")],
                ),
                SimpleNamespace(
                    type="message",
                    content=[SimpleNamespace(
                        type="output_text",
                        annotations=[SimpleNamespace(
                            type="url_citation",
                            url="https://example.com/openai-stream-source",
                            title="OpenAI stream source",
                            start_index=0,
                            end_index=10,
                        )],
                    )],
                ),
            ],
            usage=SimpleNamespace(
                input_tokens=80,
                input_tokens_details=SimpleNamespace(cached_tokens=10, cache_write_tokens=5),
                output_tokens=30,
                output_tokens_details=SimpleNamespace(reasoning_tokens=5),
            ),
        )
        return FakeOpenAIStream(response, f"response.{self.stream_status}")


class FakeGeminiInteractions:
    def __init__(self, final_response=None):
        self.request = None
        self.final_response = final_response

    @staticmethod
    def steps(content="실제 Gemini 형식의 응답", query="query one", source_url="https://example.com/gemini-source"):
        citation = SimpleNamespace(
            type="url_citation",
            url=source_url,
            title="Gemini source",
            start_index=0,
            end_index=12,
        )
        return [
            SimpleNamespace(
                type="thought",
                summary=[SimpleNamespace(type="text", text="관련 근거를 비교했습니다.")],
                signature="signed-thought",
            ),
            SimpleNamespace(
                type="google_search_call",
                id="gemini-search-1",
                arguments=SimpleNamespace(queries=[query, "query two"] if query == "query one" else [query]),
                signature="signed-search",
            ),
            SimpleNamespace(
                type="google_search_result",
                call_id="gemini-search-1",
                result=[SimpleNamespace(url=source_url, title="Gemini source")],
                signature="signed-result",
            ),
            SimpleNamespace(
                type="model_output",
                content=[SimpleNamespace(type="text", text=content, annotations=[citation])],
            ),
        ]

    def create(self, **kwargs):
        self.request = kwargs
        assert kwargs.get("stream") is True
        if self.final_response is not None:
            return iter([SimpleNamespace(event_type="interaction.completed", interaction=self.final_response)])
        return self.stream()

    @classmethod
    def response(cls):
        return SimpleNamespace(
            id="gemini-test",
            status="completed",
            output_text="실제 Gemini 형식의 응답",
            usage=SimpleNamespace(
                total_input_tokens=90,
                total_cached_tokens=0,
                total_output_tokens=40,
                total_thought_tokens=10,
                grounding_tool_count=[SimpleNamespace(type="google_search", count=2)],
            ),
            steps=cls.steps(),
        )

    def stream(self):
        completed_steps = self.steps(
            "스트리밍 Gemini 응답",
            "stream query",
            "https://example.com/gemini-stream-source",
        )
        usage = SimpleNamespace(
            total_input_tokens=70,
            total_cached_tokens=5,
            total_output_tokens=25,
            total_thought_tokens=4,
            grounding_tool_count=[SimpleNamespace(type="google_search", count=1)],
        )
        return iter(
            [
                SimpleNamespace(
                    event_type="interaction.created",
                    interaction=SimpleNamespace(id="gemini-stream-test"),
                ),
                SimpleNamespace(
                    event_type="step.start", index=0,
                    step=SimpleNamespace(type="thought", summary=[]),
                ),
                SimpleNamespace(
                    event_type="step.delta", index=0,
                    delta=SimpleNamespace(
                        type="thought_summary",
                        content=SimpleNamespace(type="text", text="관련 근거를 비교했습니다."),
                    ),
                    metadata=None,
                ),
                SimpleNamespace(
                    event_type="step.delta", index=0,
                    delta=SimpleNamespace(type="thought_signature", signature="signed-stream-thought"),
                    metadata=None,
                ),
                SimpleNamespace(
                    event_type="step.start", index=1,
                    step=SimpleNamespace(
                        type="google_search_call", id="gemini-search-stream",
                        arguments=SimpleNamespace(queries=["stream query"]),
                    ),
                ),
                SimpleNamespace(
                    event_type="step.start", index=2,
                    step=SimpleNamespace(
                        type="google_search_result",
                        call_id="gemini-search-stream",
                        result=[SimpleNamespace(
                            url="https://example.com/gemini-stream-source",
                            title="Gemini stream source",
                        )],
                    ),
                ),
                SimpleNamespace(
                    event_type="step.start", index=3,
                    step=SimpleNamespace(type="model_output", content=[]),
                ),
                SimpleNamespace(
                    event_type="step.delta", index=3,
                    delta=SimpleNamespace(type="text", text="스트리밍 "), metadata=None,
                ),
                SimpleNamespace(
                    event_type="step.delta", index=3,
                    delta=SimpleNamespace(type="text", text="Gemini 응답"), metadata=None,
                ),
                SimpleNamespace(
                    event_type="step.delta", index=3,
                    delta=SimpleNamespace(
                        type="text_annotation_delta",
                        annotations=[SimpleNamespace(
                            type="url_citation",
                            url="https://example.com/gemini-stream-source",
                            title="Gemini stream source",
                            start_index=0,
                            end_index=12,
                        )],
                    ),
                    metadata=None,
                ),
                SimpleNamespace(
                    event_type="interaction.completed",
                    interaction=SimpleNamespace(
                        id="gemini-stream-test", status="completed", usage=usage,
                        steps=completed_steps,
                    ),
                ),
            ]
        )


class FakeAnthropicStream:
    def __init__(self, response):
        self.response = response
        self.text_stream = iter(["스트리밍 ", "Claude 응답"])

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def __iter__(self):
        return iter([
            SimpleNamespace(
                type="content_block_delta",
                index=0,
                delta=SimpleNamespace(type="thinking_delta", thinking="질문을 분석하고 "),
            ),
            SimpleNamespace(
                type="content_block_delta",
                index=0,
                delta=SimpleNamespace(type="thinking_delta", thinking="근거를 비교했습니다."),
            ),
            SimpleNamespace(
                type="content_block_delta",
                index=1,
                delta=SimpleNamespace(type="text_delta", text="스트리밍 "),
            ),
            SimpleNamespace(
                type="content_block_delta",
                index=1,
                delta=SimpleNamespace(type="text_delta", text="Claude 응답"),
            ),
        ])

    def get_final_message(self):
        return self.response


class FakeAnthropicMessages:
    def __init__(self):
        self.request = None

    @staticmethod
    def response(content="실제 Claude 형식의 응답"):
        return SimpleNamespace(
            id="msg-test",
            content=[
                SimpleNamespace(
                    type="thinking",
                    thinking="질문을 분석하고 근거를 비교했습니다.",
                    signature="signed-claude-thinking",
                ),
                SimpleNamespace(
                    type="server_tool_use",
                    id="search-claude-1",
                    name="web_search",
                    input={"query": "claude research query"},
                ),
                SimpleNamespace(
                    type="web_search_tool_result",
                    tool_use_id="search-claude-1",
                    content=[
                        SimpleNamespace(
                            type="web_search_result",
                            url="https://example.com/claude-source",
                            title="Claude source",
                            page_age="July 2026",
                            encrypted_content="encrypted-result",
                        )
                    ],
                ),
                SimpleNamespace(
                    type="text",
                    text=content,
                    citations=[
                        SimpleNamespace(
                            type="web_search_result_location",
                            url="https://example.com/claude-source",
                            title="Claude source",
                            cited_text="Claude cited text",
                            encrypted_index="encrypted-index",
                        )
                    ],
                ),
            ],
            stop_reason="end_turn",
            usage=SimpleNamespace(
                input_tokens=100,
                cache_creation_input_tokens=10,
                cache_read_input_tokens=20,
                output_tokens=60,
                output_tokens_details=SimpleNamespace(thinking_tokens=15),
                server_tool_use=SimpleNamespace(web_search_requests=2),
            ),
        )

    def create(self, **kwargs):
        self.request = kwargs
        return self.response()

    def stream(self, **kwargs):
        self.request = kwargs
        return FakeAnthropicStream(self.response("스트리밍 Claude 응답"))


def fake_generation(
    messages,
    model_id,
    web_search_mode,
    reasoning_effort,
    thinking_enabled=None,
    provider_history=None,
    cache_key=None,
):
    model = get_model(model_id)
    reasoning_tokens = 0 if model.provider == "anthropic" and thinking_enabled is False else 20
    search_required = web_search_mode is True or web_search_mode == "required"
    search_queries = 1 if search_required else 0
    cost = calculate_cost(model, 100, 0, 100, reasoning_tokens, search_queries)
    return {
        "content": "테스트 provider 응답",
        "inputTokens": 100,
        "cachedInputTokens": 0,
        "outputTokens": 100,
        "reasoningTokens": reasoning_tokens,
        "reasoningEffort": reasoning_effort,
        "thinkingEnabled": bool(thinking_enabled),
        "searchQueries": search_queries,
        "webSearchUsed": bool(search_queries),
        "researchCreditCostUsd": cost,
        "researchPricingVersion": "study-2026-09-standard-v1",
        "researchPricingBand": "standard",
        "costUsd": cost,
        "responseMode": "live",
        "providerResponseId": "test-response",
        "providerStatus": "completed",
        "finishReason": "stop",
        "usageEstimated": False,
        "pricingSnapshot": model.pricing_snapshot(),
        "webSearchTrace": (
            {
                "executions": [
                    {
                        "providerSearchId": "test-search",
                        "action": "search",
                        "queries": ["test query"],
                        "status": "complete",
                    }
                ],
                "sources": [{"url": "https://example.com/test", "title": "Test source"}],
                "citations": [
                    {
                        "sourceIndex": 0,
                        "startIndex": 0,
                        "endIndex": 4,
                        "indexUnit": "unicode_codepoint",
                    }
                ],
            }
            if search_required
            else None
        ),
        "reasoningSummary": [{"index": 0, "text": "질문을 분석하고 답변을 구성했습니다."}],
        "providerState": {
            "schemaVersion": 1,
            "provider": model.provider,
            "modelId": model.id,
            "payload": {"opaque": "server-only"},
        },
    }


def fake_stream_response(*args, **kwargs):
    generation = fake_generation(*args, **kwargs)
    yield {"type": "reasoning_delta", "id": "fake-reasoning-0", "index": 0, "delta": "질문을 분석하고 "}
    yield {"type": "status", "phase": "writing"}
    yield {"type": "text_delta", "delta": "테스트 "}
    if generation["webSearchUsed"]:
        yield {"type": "status", "phase": "searching"}
        yield {"type": "search_started", "id": "fake-search-0"}
        yield {"type": "search_query", "id": "fake-search-0", "query": "test query"}
        yield {
            "type": "source_added",
            "searchId": "fake-search-0",
            "source": {"url": "https://example.com/test", "title": "Test source"},
        }
        yield {"type": "status", "phase": "thinking"}
    yield {"type": "reasoning_delta", "id": "fake-reasoning-1", "index": 1, "delta": "답변을 구성했습니다."}
    yield {"type": "status", "phase": "writing"}
    pieces = ["테스트 ", "provider 응답"]
    yield {"type": "text_delta", "delta": pieces[1]}
    generation["content"] = "".join(pieces)
    yield {"type": "done", "generation": generation}
