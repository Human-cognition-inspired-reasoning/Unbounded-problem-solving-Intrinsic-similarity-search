from types import SimpleNamespace
import unittest

from models.catalog import ModelError, calculate_cost, get_model
from models.openai import stream_openai
from tests.fakes import completed_generation


class PricingAdapterTest(unittest.TestCase):
    def test_provider_context_error_is_normalized(self):
        class ContextLimitError(Exception):
            code = "context_length_exceeded"
            body = {"error": {"message": "maximum context length exceeded"}}

        class FailingResponses:
            @staticmethod
            def stream(**_kwargs):
                raise ContextLimitError("request rejected")

        with self.assertRaisesRegex(ModelError, "CONTEXT_LIMIT_EXCEEDED"):
            completed_generation(stream_openai(
                [{"role": "user", "content": "긴 대화"}],
                "gpt-5.6-sol",
                False,
                "medium",
                client=SimpleNamespace(responses=FailingResponses()),
            ))

    def test_long_context_pricing_bands_apply_to_full_request(self):
        openai_model = get_model("gpt-5.6-sol")
        self.assertAlmostEqual(calculate_cost(openai_model, 272_000, 0, 1_000, 0, 0), 1.39)
        self.assertAlmostEqual(calculate_cost(openai_model, 272_001, 0, 1_000, 0, 0), 2.76501)
        openai_snapshot = openai_model.pricing_snapshot(272_001)
        self.assertEqual(openai_snapshot["pricingBand"], "long-context")
        self.assertEqual(openai_snapshot["inputPer1MTokensUsd"], 10.0)
        self.assertEqual(openai_snapshot["outputPer1MTokensUsd"], 45.0)

        gemini_model = get_model("gemini-3.1-pro-preview")
        self.assertAlmostEqual(calculate_cost(gemini_model, 200_001, 0, 1_000, 0, 0), 0.818004)
        gemini_snapshot = gemini_model.pricing_snapshot(200_001)
        self.assertEqual(gemini_snapshot["pricingBand"], "long-context")
        self.assertEqual(gemini_snapshot["cachedInputPer1MTokensUsd"], 0.4)

    def test_cache_writes_cost_more_than_plain_input_tokens_on_supported_models(self):
        anthropic_model = get_model("claude-sonnet-5")
        written = calculate_cost(anthropic_model, 1000, 0, 0, 0, 0, cache_write_input_tokens=1000)
        plain = calculate_cost(anthropic_model, 1000, 0, 0, 0, 0)
        self.assertAlmostEqual(written, plain * 1.25)

        openai_model = get_model("gpt-5.6-sol")
        written = calculate_cost(openai_model, 1000, 0, 0, 0, 0, cache_write_input_tokens=1000)
        plain = calculate_cost(openai_model, 1000, 0, 0, 0, 0)
        self.assertAlmostEqual(written, plain * 1.25)

    def test_cache_write_tokens_never_exceed_the_uncached_share_of_input(self):
        model = get_model("claude-sonnet-5")
        clamped = calculate_cost(model, 100, 80, 0, 0, 0, cache_write_input_tokens=999)
        # 80 read + at most 20 written; nothing is billed twice.
        self.assertAlmostEqual(
            clamped,
            (20 / 1000) * model.input_per_1k * 1.25 + (80 / 1000) * model.cached_input_per_1k,
        )
