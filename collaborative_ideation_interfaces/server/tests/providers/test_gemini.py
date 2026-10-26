from types import SimpleNamespace
import unittest

from models.gemini import stream_gemini
from tests.fakes import completed_generation, FakeGeminiInteractions


class GeminiAdapterTest(unittest.TestCase):
    def test_gemini_adapter_uses_thinking_and_actual_usage(self):
        fake_interactions = FakeGeminiInteractions(final_response=FakeGeminiInteractions.response())
        result = completed_generation(stream_gemini(
            [{"role": "user", "content": "최근 연구를 검색해줘"}],
            "gemini-3.7-flash",
            True,
            "medium",
            client=SimpleNamespace(interactions=fake_interactions),
        ))
        self.assertEqual(fake_interactions.request["model"], "gemini-3.7-flash")
        self.assertFalse(fake_interactions.request["store"])
        self.assertEqual(fake_interactions.request["generation_config"]["thinking_level"], "medium")
        self.assertEqual(fake_interactions.request["generation_config"]["thinking_summaries"], "auto")
        self.assertEqual(fake_interactions.request["generation_config"]["tool_choice"], "auto")
        self.assertEqual(fake_interactions.request["tools"], [{"type": "google_search"}])
        self.assertEqual(result["inputTokens"], 90)
        self.assertEqual(result["outputTokens"], 40)
        self.assertEqual(result["reasoningTokens"], 10)
        self.assertEqual(result["searchQueries"], 2)
        self.assertEqual(result["responseMode"], "live")
        self.assertFalse(result["usageEstimated"])
        self.assertEqual(result["pricingSnapshot"]["outputPer1MTokensUsd"], 7.5)
        self.assertAlmostEqual(result["costUsd"], 0.02851)
        self.assertEqual(result["webSearchTrace"]["executions"][0]["queries"], ["query one", "query two"])
        self.assertEqual(result["webSearchTrace"]["sources"][0]["url"], "https://example.com/gemini-source")
        self.assertEqual(result["webSearchTrace"]["citations"][0]["indexUnit"], "utf8_byte")
        self.assertEqual(result["reasoningSummary"][0]["text"], "관련 근거를 비교했습니다.")
        self.assertEqual(
            result["providerState"]["payload"]["steps"][0]["signature"],
            "signed-thought",
        )

    def test_gemini_stream_adapter_yields_text_and_final_usage(self):
        fake_interactions = FakeGeminiInteractions()
        events = list(
            stream_gemini(
                [{"role": "user", "content": "응답을 스트리밍해줘"}],
                "gemini-3.7-flash",
                True,
                "medium",
                client=SimpleNamespace(interactions=fake_interactions),
            )
        )
        event_types = [event["type"] for event in events]
        self.assertEqual(event_types[0], "reasoning_delta")
        self.assertIn("search_started", event_types)
        self.assertIn("search_query", event_types)
        self.assertIn("source_added", event_types)
        self.assertIn("citation_added", event_types)
        self.assertLess(event_types.index("source_added"), event_types.index("text_delta"))
        self.assertEqual(event_types[-1], "done")
        self.assertEqual(
            "".join(event["delta"] for event in events if event["type"] == "text_delta"),
            "스트리밍 Gemini 응답",
        )
        generation = events[-1]["generation"]
        self.assertEqual(generation["content"], "스트리밍 Gemini 응답")
        self.assertEqual(generation["cachedInputTokens"], 5)
        self.assertEqual(generation["reasoningTokens"], 4)
        self.assertEqual(generation["searchQueries"], 1)
        self.assertTrue(fake_interactions.request["stream"])
        self.assertEqual(fake_interactions.request["generation_config"]["tool_choice"], "auto")

    def test_gemini_replays_private_interaction_steps_for_the_same_model(self):
        fake_interactions = FakeGeminiInteractions()
        completed_generation(stream_gemini(
            [
                {"id": "u1", "role": "user", "content": "첫 질문"},
                {"id": "a1", "role": "assistant", "content": "공개 답변"},
                {"id": "u2", "role": "user", "content": "후속 질문"},
            ],
            "gemini-3.5-flash",
            False,
            "medium",
            client=SimpleNamespace(interactions=fake_interactions),
            provider_history=[{
                "outputMessageId": "a1",
                "provider": "google",
                "modelId": "gemini-3.5-flash",
                "payload": {
                    "steps": [
                        {"type": "thought", "signature": "opaque-thought"},
                        {
                            "type": "model_output",
                            "content": [{"type": "text", "text": "native answer"}],
                        },
                    ]
                },
            }],
        ))

        interaction_input = fake_interactions.request["input"]
        self.assertEqual(interaction_input[1]["signature"], "opaque-thought")
        self.assertEqual(interaction_input[2]["content"][0]["text"], "native answer")
        self.assertEqual(interaction_input[-1]["content"][0]["text"], "후속 질문")
        self.assertNotIn("공개 답변", str(interaction_input))
        self.assertEqual(fake_interactions.request["generation_config"]["tool_choice"], "auto")
