from types import SimpleNamespace
import unittest

from models.anthropic import stream_anthropic
from models.catalog import ModelError, calculate_cost, get_model
from tests.fakes import completed_generation, FakeAnthropicStream, FakeAnthropicMessages


class AnthropicAdapterTest(unittest.TestCase):
    def test_anthropic_adapter_uses_live_messages_api_and_actual_usage(self):
        fake_messages = FakeAnthropicMessages()
        result = completed_generation(stream_anthropic(
            [{"role": "user", "content": "최근 연구를 검색해줘"}],
            "claude-sonnet-5",
            True,
            "high",
            True,
            client=SimpleNamespace(messages=fake_messages),
        ))
        self.assertEqual(fake_messages.request["model"], "claude-sonnet-5")
        self.assertEqual(fake_messages.request["output_config"], {"effort": "high"})
        self.assertEqual(
            fake_messages.request["thinking"],
            {"type": "adaptive", "display": "summarized"},
        )
        self.assertEqual(
            fake_messages.request["tools"],
            [{"type": "web_search_20250305", "name": "web_search"}],
        )
        self.assertEqual(
            fake_messages.request["tool_choice"],
            {"type": "tool", "name": "web_search"},
        )
        self.assertEqual(result["inputTokens"], 130)
        self.assertEqual(result["cachedInputTokens"], 20)
        self.assertEqual(result["outputTokens"], 45)
        self.assertEqual(result["reasoningTokens"], 15)
        self.assertEqual(result["searchQueries"], 2)
        self.assertTrue(result["thinkingEnabled"])
        self.assertEqual(result["responseMode"], "live")
        self.assertFalse(result["usageEstimated"])
        # 10 cache-creation tokens bill at 1.25x the base input rate.
        self.assertEqual(result["cacheWriteInputTokens"], 10)
        self.assertAlmostEqual(result["costUsd"], 0.0212435)
        self.assertEqual(result["webSearchTrace"]["executions"][0]["queries"], ["claude research query"])
        self.assertEqual(result["webSearchTrace"]["sources"][0]["encryptedContent"], "encrypted-result")
        self.assertEqual(result["webSearchTrace"]["citations"][0]["citedText"], "Claude cited text")
        self.assertEqual(result["reasoningSummary"][0]["text"], "질문을 분석하고 근거를 비교했습니다.")
        self.assertEqual(
            result["providerState"]["payload"]["contentBlocks"][0]["signature"],
            "signed-claude-thinking",
        )

    def test_anthropic_stream_adapter_yields_text_and_final_usage(self):
        fake_messages = FakeAnthropicMessages()
        events = list(
            stream_anthropic(
                [{"role": "user", "content": "응답을 스트리밍해줘"}],
                "claude-opus-4-8",
                False,
                "medium",
                False,
                client=SimpleNamespace(messages=fake_messages),
            )
        )
        self.assertEqual(
            [event["type"] for event in events],
            ["reasoning_delta", "reasoning_delta", "status", "text_delta", "text_delta", "done"],
        )
        self.assertEqual(
            "".join(event["delta"] for event in events if event["type"] == "text_delta"),
            "스트리밍 Claude 응답",
        )
        # No toggle on this model any more: effort is the depth control and
        # thinking stays adaptive, with the summary the reasoning UI streams.
        self.assertEqual(
            fake_messages.request["thinking"],
            {"type": "adaptive", "display": "summarized"},
        )
        self.assertEqual(fake_messages.request["tool_choice"], {"type": "auto"})
        generation = events[-1]["generation"]
        self.assertEqual(generation["content"], "스트리밍 Claude 응답")
        self.assertEqual(generation["providerStatus"], "completed")
        self.assertEqual(generation["finishReason"], "end_turn")

    def test_anthropic_preserves_final_stream_thinking_usage(self):
        for reported_tokens, snapshot_tokens, expected_tokens in (
            (800, None, 800),
            (0, 15, 0),
            (None, 15, 15),
        ):
            with self.subTest(reported_tokens=reported_tokens):
                response = FakeAnthropicMessages.response("스트리밍 Claude 응답")
                response.usage.output_tokens = 1000
                response.usage.output_tokens_details = (
                    SimpleNamespace(thinking_tokens=snapshot_tokens)
                    if snapshot_tokens is not None else None
                )

                class UsageStream(FakeAnthropicStream):
                    def __iter__(self):
                        yield from super().__iter__()
                        if reported_tokens is not None:
                            for tokens in (100, reported_tokens):
                                yield SimpleNamespace(
                                    type="message_delta",
                                    usage=SimpleNamespace(
                                        output_tokens_details=SimpleNamespace(thinking_tokens=tokens),
                                    ),
                                )
                        # A later event without a breakdown must not clear it.
                        yield SimpleNamespace(type="message_delta", usage=SimpleNamespace())

                client = SimpleNamespace(messages=SimpleNamespace(
                    stream=lambda **kwargs: UsageStream(response),
                ))
                result = stream_anthropic(
                    [{"role": "user", "content": "응답해줘"}],
                    "claude-fable-5-1", False, "high", True, client=client,
                )
                result = completed_generation(result)
                self.assertEqual(result["reasoningTokens"], expected_tokens)
                self.assertEqual(result["outputTokens"], 1000 - expected_tokens)
                self.assertEqual(result["content"], "스트리밍 Claude 응답")
                self.assertFalse(result["usageEstimated"])
                self.assertEqual(
                    result["costUsd"],
                    calculate_cost(
                        get_model("claude-fable-5-1"), 130, 20, 1000, 0, 2,
                        cache_write_input_tokens=10,
                    ),
                )

    def test_fable_refusal_is_returned_as_a_normal_provider_result(self):
        refusal = SimpleNamespace(
            id="msg-refusal",
            content=[],
            stop_reason="refusal",
            stop_details=SimpleNamespace(
                category="cyber",
                explanation="이 요청에는 응답할 수 없습니다.",
            ),
            usage=SimpleNamespace(
                input_tokens=25,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
                output_tokens=0,
                output_tokens_details=SimpleNamespace(thinking_tokens=0),
                server_tool_use=SimpleNamespace(web_search_requests=0),
            ),
        )

        class RefusalStream:
            def __iter__(self):
                return iter(())

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def get_final_message(self):
                return refusal

        class RefusalMessages:
            def stream(self, **kwargs):
                self.request = kwargs
                return RefusalStream()

        fake_messages = RefusalMessages()
        result = completed_generation(stream_anthropic(
            [{"role": "user", "content": "요청"}],
            "claude-fable-5-1",
            False,
            "high",
            True,
            client=SimpleNamespace(messages=fake_messages),
        ))

        self.assertEqual(fake_messages.request["tool_choice"], {"type": "auto"})
        self.assertEqual(
            fake_messages.request["thinking"],
            {"type": "adaptive", "display": "summarized"},
        )
        self.assertEqual(result["content"], "이 요청에는 응답할 수 없습니다.")
        self.assertEqual(result["providerStatus"], "refused")
        self.assertEqual(result["finishReason"], "refusal")

    def test_anthropic_replays_signed_thinking_block_for_the_same_model(self):
        fake_messages = FakeAnthropicMessages()
        completed_generation(stream_anthropic(
            [
                {"id": "u1", "role": "user", "content": "첫 질문"},
                {"id": "a1", "role": "assistant", "content": "공개 답변"},
                {"id": "u2", "role": "user", "content": "후속 질문"},
            ],
            "claude-sonnet-5",
            False,
            "low",
            True,
            client=SimpleNamespace(messages=fake_messages),
            provider_history=[{
                "outputMessageId": "a1",
                "provider": "anthropic",
                "modelId": "claude-sonnet-5",
                "payload": {
                    "contentBlocks": [
                        {"type": "thinking", "thinking": "summary", "signature": "opaque-signature"},
                        {"type": "text", "text": "native answer"},
                    ]
                },
            }],
        ))

        restored = fake_messages.request["messages"][1]["content"]
        self.assertEqual(restored[0]["signature"], "opaque-signature")
        self.assertNotIn("공개 답변", str(fake_messages.request["messages"]))

    def test_anthropic_drops_server_tool_use_left_unresolved_by_a_paused_turn(self):
        fake_messages = FakeAnthropicMessages()
        completed_generation(stream_anthropic(
            [
                {"id": "u1", "role": "user", "content": "첫 질문"},
                {"id": "a1", "role": "assistant", "content": "공개 답변"},
                {"id": "u2", "role": "user", "content": "후속 질문"},
            ],
            "claude-sonnet-5",
            False,
            "low",
            True,
            client=SimpleNamespace(messages=fake_messages),
            provider_history=[{
                "outputMessageId": "a1",
                "provider": "anthropic",
                "modelId": "claude-sonnet-5",
                "payload": {
                    "contentBlocks": [
                        {"type": "thinking", "thinking": "summary", "signature": "opaque-signature"},
                        {
                            "type": "server_tool_use",
                            "id": "srvtoolu_resolved",
                            "name": "web_search",
                            "input": {"query": "완료된 검색"},
                        },
                        {
                            "type": "web_search_tool_result",
                            "tool_use_id": "srvtoolu_resolved",
                            "content": [{"type": "web_search_result", "url": "https://example.com"}],
                        },
                        {
                            "type": "server_tool_use",
                            "id": "srvtoolu_pending",
                            "name": "web_search",
                            "input": {"query": "중단된 검색"},
                        },
                    ]
                },
            }],
        ))

        restored = fake_messages.request["messages"][1]["content"]
        self.assertEqual(
            [block["type"] for block in restored],
            ["thinking", "server_tool_use", "web_search_tool_result"],
        )
        self.assertNotIn("srvtoolu_pending", str(restored))

    def test_anthropic_declares_a_cache_breakpoint_on_every_request(self):
        fake_messages = FakeAnthropicMessages()
        completed_generation(stream_anthropic(
            [{"role": "user", "content": "질문"}],
            "claude-sonnet-5",
            False,
            "low",
            True,
            client=SimpleNamespace(messages=fake_messages),
        ))
        # Anthropic has no implicit caching: without this the whole replayed
        # history is billed at full price on every turn.
        self.assertEqual(fake_messages.request["cache_control"], {"type": "ephemeral"})

    def test_anthropic_haiku_uses_manual_thinking_budget(self):
        fake_messages = FakeAnthropicMessages()
        completed_generation(stream_anthropic(
            [{"role": "user", "content": "짧게 답해줘"}],
            "claude-haiku-4-5-20251001",
            False,
            "none",
            True,
            client=SimpleNamespace(messages=fake_messages),
        ))
        self.assertNotIn("output_config", fake_messages.request)
        self.assertEqual(
            fake_messages.request["thinking"],
            {"type": "enabled", "budget_tokens": 8_192},
        )

    def test_anthropic_haiku_rejects_forced_search_with_manual_thinking(self):
        with self.assertRaisesRegex(
            ModelError, "FORCED_SEARCH_WITH_MANUAL_THINKING_UNSUPPORTED"
        ):
            completed_generation(stream_anthropic(
                [{"role": "user", "content": "검색해줘"}],
                "claude-haiku-4-5-20251001",
                True,
                "none",
                True,
                client=SimpleNamespace(messages=FakeAnthropicMessages()),
            ))
