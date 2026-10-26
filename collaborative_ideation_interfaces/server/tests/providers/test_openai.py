from types import SimpleNamespace
import unittest

from models.openai import stream_openai
from tests.fakes import completed_generation, FakeOpenAIResponses


class OpenaiAdapterTest(unittest.TestCase):
    def test_openai_adapter_uses_responses_api_and_actual_usage(self):
        fake_responses = FakeOpenAIResponses(final_response=FakeOpenAIResponses.response())
        result = completed_generation(stream_openai(
            [{"role": "user", "content": "최근 연구를 검색해줘"}],
            "gpt-5.6-sol",
            True,
            "high",
            client=SimpleNamespace(responses=fake_responses),
        ))
        self.assertEqual(fake_responses.request["model"], "gpt-5.6-sol")
        self.assertEqual(
            fake_responses.request["reasoning"],
            {"effort": "high", "summary": "auto", "context": "all_turns"},
        )
        self.assertEqual(fake_responses.request["tools"], [{"type": "web_search"}])
        self.assertEqual(fake_responses.request["tool_choice"], "required")
        self.assertEqual(
            fake_responses.request["include"],
            ["web_search_call.action.sources", "reasoning.encrypted_content"],
        )
        self.assertFalse(fake_responses.request["store"])
        self.assertNotIn("max_output_tokens", fake_responses.request)
        self.assertEqual(result["inputTokens"], 100)
        self.assertEqual(result["cachedInputTokens"], 20)
        self.assertEqual(result["cacheWriteInputTokens"], 10)
        self.assertEqual(result["outputTokens"], 45)
        self.assertEqual(result["reasoningTokens"], 15)
        self.assertEqual(result["searchQueries"], 1)
        self.assertEqual(result["responseMode"], "live")
        self.assertFalse(result["usageEstimated"])
        self.assertEqual(result["pricingSnapshot"]["catalogVersion"], "study-2026-09-standard-v1")
        self.assertEqual(result["pricingSnapshot"]["outputPer1MTokensUsd"], 30.0)
        self.assertAlmostEqual(result["costUsd"], 0.0122225)
        self.assertEqual(result["researchCreditCostUsd"], result["costUsd"])
        self.assertEqual(result["webSearchTrace"]["executions"][0]["queries"], ["openai research query"])
        self.assertEqual(result["webSearchTrace"]["sources"][0]["url"], "https://example.com/openai-source")
        self.assertEqual(result["webSearchTrace"]["citations"][0]["sourceIndex"], 0)
        self.assertEqual(result["reasoningSummary"][0]["text"], "문제를 분석하고 답변을 구성했습니다.")
        self.assertEqual(
            result["providerState"]["payload"]["outputItems"][0]["encrypted_content"],
            "encrypted-openai-state",
        )

    def test_openai_replays_private_output_items_without_duplicating_public_answer(self):
        fake_responses = FakeOpenAIResponses()
        completed_generation(stream_openai(
            [
                {"id": "u1", "role": "user", "content": "첫 질문"},
                {"id": "a1", "role": "assistant", "content": "공개 답변"},
                {"id": "u2", "role": "user", "content": "후속 질문"},
            ],
            "gpt-5.6-sol",
            False,
            "max",
            client=SimpleNamespace(responses=fake_responses),
            provider_history=[{
                "outputMessageId": "a1",
                "provider": "openai",
                "modelId": "gpt-5.6-sol",
                "payload": {
                    "outputItems": [
                        {"type": "reasoning", "encrypted_content": "opaque-state"},
                        {"type": "message", "role": "assistant", "content": "native answer"},
                    ]
                },
            }],
        ))

        self.assertEqual(fake_responses.request["input"][1]["encrypted_content"], "opaque-state")
        self.assertEqual(fake_responses.request["input"][2]["content"], "native answer")
        self.assertNotIn("공개 답변", str(fake_responses.request["input"]))
        self.assertEqual(fake_responses.request["tool_choice"], "auto")

    def test_openai_sends_a_stable_prompt_cache_key_per_conversation(self):
        fake_responses = FakeOpenAIResponses()
        completed_generation(stream_openai(
            [{"role": "user", "content": "질문"}],
            "gpt-5.6-sol",
            False,
            "low",
            client=SimpleNamespace(responses=fake_responses),
            cache_key="conversation-abc",
        ))
        self.assertEqual(fake_responses.request["prompt_cache_key"], "conversation-abc")

    def test_openai_omits_prompt_cache_key_when_no_conversation_is_given(self):
        fake_responses = FakeOpenAIResponses()
        completed_generation(stream_openai(
            [{"role": "user", "content": "질문"}],
            "gpt-5.6-sol",
            False,
            "low",
            client=SimpleNamespace(responses=fake_responses),
        ))
        self.assertNotIn("prompt_cache_key", fake_responses.request)

    def _openai_replay_input(self, output_items):
        fake_responses = FakeOpenAIResponses()
        completed_generation(stream_openai(
            [
                {"id": "u1", "role": "user", "content": "첫 질문"},
                {"id": "a1", "role": "assistant", "content": "공개 답변"},
                {"id": "u2", "role": "user", "content": "후속 질문"},
            ],
            "gpt-5.6-sol",
            False,
            "low",
            client=SimpleNamespace(responses=fake_responses),
            provider_history=[{
                "outputMessageId": "a1",
                "provider": "openai",
                "modelId": "gpt-5.6-sol",
                "payload": {"outputItems": output_items},
            }],
        ))
        return fake_responses.request["input"]

    def test_openai_skips_reasoning_item_without_encrypted_content(self):
        replayed = self._openai_replay_input([
            {"type": "reasoning", "id": "rs_1"},
            {"type": "message", "role": "assistant", "content": "native answer"},
        ])

        self.assertEqual([item.get("type") for item in replayed[1:2]], ["message"])
        self.assertNotIn("reasoning", str(replayed))

    def test_openai_drops_trailing_reasoning_item_left_by_truncated_turn(self):
        replayed = self._openai_replay_input([
            {"type": "message", "role": "assistant", "content": "native answer"},
            {"type": "reasoning", "id": "rs_2", "encrypted_content": "opaque-state"},
        ])

        self.assertEqual(replayed[1]["content"], "native answer")
        self.assertNotIn("opaque-state", str(replayed))

    def test_openai_falls_back_to_public_text_when_nothing_is_replayable(self):
        replayed = self._openai_replay_input([{"type": "reasoning", "id": "rs_3"}])

        self.assertEqual(replayed[1], {"role": "assistant", "content": "공개 답변"})

    def test_openai_stream_adapter_yields_text_and_final_usage(self):
        fake_responses = FakeOpenAIResponses()
        events = list(
            stream_openai(
                [{"role": "user", "content": "응답을 스트리밍해줘"}],
                "gpt-5.6-sol",
                False,
                "medium",
                client=SimpleNamespace(responses=fake_responses),
            )
        )
        self.assertEqual(
            [event["type"] for event in events],
            [
                "reasoning_delta", "reasoning_delta", "status", "text_delta", "text_delta",
                "source_added", "citation_added", "done",
            ],
        )
        reasoning_events = [event for event in events if event["type"] == "reasoning_delta"]
        self.assertNotEqual(reasoning_events[0]["id"], reasoning_events[1]["id"])
        self.assertEqual([event["index"] for event in reasoning_events], [0, 1])
        self.assertEqual(
            "".join(event["delta"] for event in events if event["type"] == "text_delta"),
            "스트리밍 OpenAI 응답",
        )
        generation = events[-1]["generation"]
        self.assertEqual(generation["content"], "스트리밍 OpenAI 응답")
        self.assertEqual(generation["inputTokens"], 80)
        self.assertEqual(generation["cacheWriteInputTokens"], 5)
        self.assertEqual(generation["outputTokens"], 25)
        self.assertEqual(generation["reasoningTokens"], 5)
        self.assertNotIn("max_output_tokens", fake_responses.request)
        self.assertEqual(generation["providerStatus"], "completed")
        self.assertEqual(
            next(event for event in events if event["type"] == "citation_added")["citation"]["indexUnit"],
            "unicode_codepoint",
        )

    def test_openai_incomplete_stream_persists_partial_response(self):
        fake_responses = FakeOpenAIResponses(stream_status="incomplete")
        events = list(
            stream_openai(
                [{"role": "user", "content": "아주 긴 응답을 생성해줘"}],
                "gpt-5.6-terra",
                False,
                "low",
                client=SimpleNamespace(responses=fake_responses),
            )
        )

        self.assertEqual(events[-1]["type"], "done")
        generation = events[-1]["generation"]
        self.assertEqual(generation["content"], "스트리밍 OpenAI 응답")
        self.assertEqual(generation["providerStatus"], "incomplete")
        self.assertEqual(generation["finishReason"], "max_tokens")
        self.assertGreater(generation["costUsd"], 0)
