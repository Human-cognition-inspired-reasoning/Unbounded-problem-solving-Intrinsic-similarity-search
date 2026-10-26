"""October catalog migration: tariffs, request contracts, and SDK compatibility."""
import json
import random
from types import SimpleNamespace
import unittest

from anthropic import Anthropic
import httpx

from models.anthropic import stream_anthropic
from models.catalog import (
    MODELS,
    ModelError,
    calculate_cost,
    get_model,
    get_reasoning_effort,
    random_initial_settings,
)
from models.gemini import stream_gemini
from models.openai import stream_openai
from tests.fakes import (
    FakeAnthropicMessages,
    FakeGeminiInteractions,
    FakeOpenAIResponses,
    completed_generation,
)


# USD per million tokens: input, cache read, 5m/default cache write, output.
NEW_RATES = {
    "gpt-6.1-sol": (2, 0.10, 2.50, 10),
    "gpt-6-sol": (2, 0.20, 2.50, 10),
    "gpt-6-luna": (0.10, 0.01, 0.125, 0.50),
    "claude-sonnet-5-5": (2, 0.20, 2.50, 10),
    "claude-opus-5-5": (4, 0.20, 5, 20),
    "gemini-3.5-flash-lite": (0.30, 0.03, 0.30, 2.50),
}


class ModelCatalogTest(unittest.TestCase):
    def test_selection_and_random_assignment_use_updated_catalog(self):
        selectable = {m.id for m in MODELS.values() if m.selectable}
        self.assertEqual(selectable, set(NEW_RATES) | {
            "gpt-5.6-terra", "claude-haiku-4-5-20251001",
            "gemini-3.7-flash", "gemini-3.8-flash", "gemini-3.1-pro-preview",
        })
        draws = [random_initial_settings(random.Random(seed)) for seed in range(1000)]
        self.assertEqual({d["modelId"] for d in draws}, selectable)
        for draw in draws:
            model = get_model(draw["modelId"])
            self.assertIn(draw["reasoningEffort"], model.reasoning_options)
        with self.assertRaisesRegex(ModelError, "INVALID_REASONING_EFFORT"):
            get_reasoning_effort(get_model("gpt-6.1-sol"), "none")

    def test_partitioned_cache_billing_includes_reasoning_once(self):
        for model_id, (plain, read, write, output) in NEW_RATES.items():
            with self.subTest(model=model_id):
                model = get_model(model_id)
                snapshot = model.pricing_snapshot(100_000)
                for key, expected in zip((
                    "inputPer1MTokensUsd", "cachedInputPer1MTokensUsd",
                    "cacheWriteInputPer1MTokensUsd", "outputPer1MTokensUsd",
                ), (plain, read, write, output)):
                    self.assertAlmostEqual(snapshot[key], expected)
                # 70K plain + 20K read + 10K write, 1K visible + 2K thinking.
                expected = .07 * plain + .02 * read + .01 * write + .003 * output
                expected += 2 * (.014 if model.provider == "google" else .01)
                self.assertAlmostEqual(
                    calculate_cost(model, 100_000, 20_000, 1_000, 2_000, 2, 10_000),
                    expected, places=8,
                )

    def test_long_context_threshold_prices_the_whole_request(self):
        for model_id in ("gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna"):
            model = get_model(model_id)
            plain, read, write, output = NEW_RATES[model_id]
            for count, input_factor, output_factor in ((272_000, 1, 1), (272_001, 2, 1.5)):
                with self.subTest(model=model_id, input=count):
                    expected = ((count - 30_000) * plain + 20_000 * read + 10_000 * write)
                    expected *= input_factor / 1_000_000
                    expected += .003 * output * output_factor
                    self.assertAlmostEqual(
                        calculate_cost(model, count, 20_000, 1_000, 2_000, 0, 10_000),
                        expected, places=8,
                    )
        for model_id in ("claude-sonnet-5-5", "claude-opus-5-5", "gemini-3.5-flash-lite"):
            model = get_model(model_id)
            self.assertEqual(model.pricing_snapshot(900_000), model.pricing_snapshot())

    def test_retired_tariffs_are_not_replaced_by_current_provider_prices(self):
        for model_id, rates in {
            "gpt-5.6-sol": (.005, .0005, .030),
            "gpt-5.6-luna": (.001, .0001, .006),
            "claude-sonnet-5": (.003, .0003, .015),
            "claude-opus-5": (.005, .0005, .025),
        }.items():
            model = get_model(model_id)
            self.assertFalse(model.selectable)
            self.assertEqual((model.input_per_1k, model.cached_input_per_1k, model.output_per_1k), rates)
        # Existing Gemini study tariffs continue to exclude temporary promotions.
        for model_id in ("gemini-3.7-flash", "gemini-3.8-flash"):
            self.assertEqual(get_model(model_id).pricing_snapshot()["outputPer1MTokensUsd"], 7.5)

    def test_new_openai_models_use_responses_files_and_conversation_cache_key(self):
        for model_id in ("gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna"):
            for effort in get_model(model_id).reasoning_options:
                with self.subTest(model=model_id, effort=effort):
                    responses = FakeOpenAIResponses(final_response=FakeOpenAIResponses.response())
                    result = completed_generation(stream_openai(
                        [{"role": "user", "content": "Read the file", "_files": [
                            {"name": "study.pdf", "mime": "application/pdf", "base64": "JVBERg=="},
                        ]}], model_id, "auto", effort,
                        client=SimpleNamespace(responses=responses), cache_key="conversation-test",
                    ))
                    request = responses.request
                    self.assertEqual(request["model"], model_id)
                    self.assertEqual(request["reasoning"]["effort"], effort)
                    self.assertEqual(request["prompt_cache_key"], "conversation-test")
                    self.assertEqual(request["input"][0]["content"][1]["type"], "input_file")
                    self.assertEqual(request["tool_choice"], "auto")
                    self.assertFalse(request["store"])
                    self.assertNotIn("temperature", request)
                    self.assertEqual(result["cacheWriteInputTokens"], 10)
                    self.assertEqual(result["outputTokens"] + result["reasoningTokens"], 60)
                    self.assertEqual(result["providerState"]["modelId"], model_id)

    def test_claude_55_uses_adaptive_thinking_and_rejects_forced_search(self):
        for model_id, default in (("claude-sonnet-5-5", "high"), ("claude-opus-5-5", "medium")):
            messages = FakeAnthropicMessages()
            result = completed_generation(stream_anthropic(
                [{"role": "user", "content": "Read the file", "_files": [
                    {"name": "data.csv", "mime": "text/csv", "fileId": "file-test"},
                ]}], model_id, "auto", None, False,
                client=SimpleNamespace(messages=messages),
            ))
            request = messages.request
            self.assertEqual(request["output_config"]["effort"], default)
            self.assertEqual(request["thinking"]["type"], "adaptive")
            self.assertEqual(request["thinking"]["display"], "summarized")
            self.assertEqual(request["cache_control"], {"type": "ephemeral"})
            self.assertEqual(request["tool_choice"], {"type": "auto"})
            self.assertEqual(request["max_tokens"], 128_000)
            self.assertEqual(request["messages"][0]["content"][1]["source"]["file_id"], "file-test")
            self.assertTrue(result["thinkingEnabled"])
            self.assertEqual(result["outputTokens"] + result["reasoningTokens"], 60)
            messages.request = None
            with self.assertRaisesRegex(ModelError, "FORCED_SEARCH_UNSUPPORTED"):
                list(stream_anthropic(
                    [{"role": "user", "content": "Search"}], model_id, "required", default,
                    True, client=SimpleNamespace(messages=messages),
                ))
            self.assertIsNone(messages.request)

    def test_flash_lite_supports_all_efforts_files_and_implicit_cache_usage(self):
        for effort in (None, "minimal", "low", "medium", "high"):
            interactions = FakeGeminiInteractions()
            result = completed_generation(stream_gemini(
                [{"role": "user", "content": "Read the file", "_files": [
                    {"name": "study.pdf", "mime": "application/pdf", "base64": "JVBERg=="},
                ]}], "gemini-3.5-flash-lite", "auto", effort,
                client=SimpleNamespace(interactions=interactions),
            ))
            request = interactions.request
            self.assertEqual(request["model"], "gemini-3.5-flash-lite")
            self.assertEqual(request["generation_config"]["thinking_level"], effort or "minimal")
            self.assertEqual(request["input"][0]["content"][1]["type"], "document")
            self.assertEqual(request["tools"], [{"type": "google_search"}])
            self.assertFalse(request["store"])
            self.assertEqual(result["cachedInputTokens"], 5)
            self.assertEqual(result["outputTokens"], 25)
            self.assertEqual(result["reasoningTokens"], 4)
            self.assertAlmostEqual(result["costUsd"], .01409215)

    def test_anthropic_sdk_sends_binding_control_and_preserves_stream_report(self):
        transformations = [{
            "type": "thinking_dropped", "path": "messages.1.content.0",
            "reason": "prefix_binding_mismatch",
        }]
        events = [
            {"type": "message_start", "message": {
                "id": "msg-sdk-test", "type": "message", "role": "assistant",
                "model": "claude-opus-5-5", "content": [], "stop_reason": None,
                "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 0},
            }, "input_transformations": transformations},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
             "usage": {"output_tokens": 2, "output_tokens_details": {"thinking_tokens": 1}}},
            {"type": "message_stop"},
        ]
        captured = []

        def handle(request):
            captured.append(request)
            body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

        with Anthropic(api_key="test-key", http_client=httpx.Client(transport=httpx.MockTransport(handle))) as client:
            result = completed_generation(stream_anthropic(
                [{"role": "user", "content": "First"},
                 {"id": "a1", "role": "assistant", "content": "Answer"},
                 {"role": "user", "content": "Continue"}],
                "claude-opus-5-5", "auto", "medium", True, client=client,
                provider_history=[{"provider": "anthropic", "outputMessageId": "a1", "payload": {
                    "contentBlocks": [{"type": "thinking", "thinking": "summary", "signature": "opaque"},
                                      {"type": "text", "text": "Answer"}],
                }}],
            ))
        body = json.loads(captured[0].content)
        self.assertEqual(captured[0].headers["anthropic-beta"], "thinking-binding-controls-2026-08-01")
        self.assertEqual(body["thinking"]["block_binding"], {"prefix_mismatch_behavior": "drop_block"})
        self.assertEqual(body["messages"][1]["content"][0]["signature"], "opaque")
        self.assertEqual(result["providerState"]["payload"]["inputTransformations"], transformations)
        self.assertEqual(result["outputTokens"], 1)
        self.assertEqual(result["reasoningTokens"], 1)


if __name__ == "__main__":
    unittest.main()
