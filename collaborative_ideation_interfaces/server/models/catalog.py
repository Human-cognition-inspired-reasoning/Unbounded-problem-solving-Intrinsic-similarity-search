"""Model capabilities and frozen research-credit pricing."""
from __future__ import annotations

from dataclasses import dataclass
import math
import os
import random


RESEARCH_PRICING_VERSION = "study-2026-09-standard-v1"


DEFAULT_MODEL_ID = "gpt-6.1-sol"


# Anthropic has no implicit caching, so every request declares one breakpoint.
# The default 5-minute entry fits an active chat session: a conversation turn
# arrives well inside the window, and the write premium (1.25x base input) pays
# for itself from the second request onward. A 1-hour entry survives longer
# gaps but doubles the write premium, so it only wins when participants leave
# the tab idle for minutes at a time — switch both constants together.
ANTHROPIC_CACHE_CONTROL = {"type": "ephemeral"}


ANTHROPIC_CACHE_WRITE_MULTIPLIER = 1.25


@dataclass(frozen=True)
class PricingBand:
    id: str
    min_input_tokens: int
    input_multiplier: float = 1.0
    cached_input_multiplier: float = 1.0
    output_multiplier: float = 1.0


@dataclass(frozen=True)
class ModelSpec:
    id: str
    label: str
    provider: str
    provider_label: str
    supports_web_search: bool
    reasoning_parameter: str
    reasoning_options: tuple[str, ...]
    default_reasoning: str
    input_per_1k: float
    cached_input_per_1k: float
    output_per_1k: float
    search_cost: float
    supports_thinking_toggle: bool = False
    max_output_tokens: int | None = None
    adaptive_thinking: bool = False
    pricing_bands: tuple[PricingBand, ...] = ()
    # Writing a cache entry can cost more than a plain input token. Anthropic
    # and current OpenAI models both expose this premium in provider usage.
    cache_write_multiplier: float = 1.0
    supports_forced_web_search: bool = True
    # Retired models stay in the registry so past model calls keep their frozen
    # tariff and their provider state stays replayable. They drop out of the
    # picker but must still resolve through get_model().
    selectable: bool = True
    # New Claude models reject replayed thinking when its earlier context
    # changed (for example after switching models or refreshing file IDs).
    thinking_prefix_binding: bool = False
    description: str = ""

    @property
    def always_thinks(self) -> bool:
        """Adaptive models with no participant toggle think on every request.

        `effort` already governs how much thinking to do, and Anthropic's
        guidance is to lower effort rather than disable thinking. So a model
        that exposes effort does not also expose a toggle, and thinking stays
        on -- which also removes the combination Opus 5 rejects outright
        (disabled thinking above effort `high`).
        """
        return self.adaptive_thinking and not self.supports_thinking_toggle

    @property
    def default_thinking_enabled(self) -> bool:
        return self.always_thinks or self.supports_thinking_toggle

    def public_dict(self, execution_mode: str) -> dict:
        return {
            "id": self.id,
            "apiModel": self.id,
            "label": self.label,
            "provider": self.provider,
            "providerLabel": self.provider_label,
            "description": self.description,
            "supportsWebSearch": self.supports_web_search,
            "supportsForcedWebSearch": self.supports_forced_web_search,
            "reasoningParameter": self.reasoning_parameter,
            "reasoningOptions": list(self.reasoning_options),
            "defaultReasoning": self.default_reasoning,
            "supportsThinkingToggle": self.supports_thinking_toggle,
            "defaultThinkingEnabled": self.default_thinking_enabled,
            "alwaysThinks": self.always_thinks,
            "selectable": self.selectable,
            "executionMode": execution_mode,
        }

    def pricing_band(self, input_tokens: int = 0) -> PricingBand:
        selected = PricingBand("standard", 0)
        for band in sorted(self.pricing_bands, key=lambda row: row.min_input_tokens):
            if input_tokens >= band.min_input_tokens:
                selected = band
        return selected

    def pricing_snapshot(self, input_tokens: int = 0) -> dict:
        """Return the frozen research-credit tariff applied to one model call."""
        band = self.pricing_band(input_tokens)
        return {
            "catalogVersion": RESEARCH_PRICING_VERSION,
            "provider": self.provider,
            "apiModel": self.id,
            "billingBasis": "frozen-research-credit",
            "pricingBand": band.id,
            "pricingBandMinInputTokens": band.min_input_tokens,
            "inputPer1MTokensUsd": self.input_per_1k * band.input_multiplier * 1000,
            "cachedInputPer1MTokensUsd": self.cached_input_per_1k * band.cached_input_multiplier * 1000,
            "cacheWriteInputPer1MTokensUsd": (
                self.input_per_1k * band.input_multiplier * self.cache_write_multiplier * 1000
            ),
            "outputPer1MTokensUsd": self.output_per_1k * band.output_multiplier * 1000,
            "searchPerQueryUsd": self.search_cost,
            "promotionsIncluded": False,
        }


MODELS = {
    model.id: model
    for model in (
        # Add new IDs to the frozen study tariff; never reprice historical IDs.
        # Standard API rates verified against provider docs on 2026-10-07.
        ModelSpec(
            "gpt-6.1-sol", "GPT-6.1 Sol", "openai", "OpenAI", True,
            "reasoning.effort", ("low", "medium", "high", "xhigh", "max"), "medium",
            0.002, 0.0001, 0.010, 0.010,
            pricing_bands=(PricingBand("long-context", 272_001, 2.0, 2.0, 1.5),),
            cache_write_multiplier=1.25,
            description="Advanced reasoning for complex work",
        ),
        ModelSpec(
            "gpt-6-sol", "GPT-6 Sol", "openai", "OpenAI", True,
            "reasoning.effort", ("none", "low", "medium", "high", "xhigh", "max"), "medium",
            0.002, 0.0002, 0.010, 0.010,
            pricing_bands=(PricingBand("long-context", 272_001, 2.0, 2.0, 1.5),),
            cache_write_multiplier=1.25,
            description="Reasoning for coding and research",
        ),
        ModelSpec(
            "gpt-6-luna", "GPT-6 Luna", "openai", "OpenAI", True,
            "reasoning.effort", ("none", "low", "medium", "high", "xhigh", "max"), "medium",
            0.0001, 0.00001, 0.0005, 0.010,
            pricing_bands=(PricingBand("long-context", 272_001, 2.0, 2.0, 1.5),),
            cache_write_multiplier=1.25,
            description="Lightweight, high-volume responses",
        ),
        ModelSpec(
            "gpt-5.6-sol", "GPT-5.6 Sol", "openai", "OpenAI", True,
            "reasoning.effort", ("none", "low", "medium", "high", "xhigh", "max"), "medium",
            0.005, 0.0005, 0.030, 0.010,
            pricing_bands=(PricingBand("long-context", 272_001, 2.0, 2.0, 1.5),),
            cache_write_multiplier=1.25,
            selectable=False,
            description="Advanced research reasoning",
        ),
        ModelSpec(
            "gpt-6-astra", "GPT-6 Astra", "openai", "OpenAI", True,
            "reasoning.effort", ("low", "medium", "high", "xhigh", "max"), "medium",
            0.010, 0.001, 0.050, 0.010,
            pricing_bands=(PricingBand("long-context", 272_001, 2.0, 2.0, 1.5),),
            cache_write_multiplier=1.25,
            # Too costly for the study budget; kept out of the picker and assignment.
            selectable=False,
            description="Frontier reasoning for complex work",
        ),
        ModelSpec(
            "gpt-5.6-terra", "GPT-5.6 Terra", "openai", "OpenAI", True,
            "reasoning.effort", ("none", "low", "medium", "high", "xhigh", "max"), "medium",
            0.0025, 0.00025, 0.015, 0.010,
            pricing_bands=(PricingBand("long-context", 272_001, 2.0, 2.0, 1.5),),
            cache_write_multiplier=1.25,
            description="Fast and efficient responses",
        ),
        ModelSpec(
            "gpt-5.6-luna", "GPT-5.6 Luna", "openai", "OpenAI", True,
            "reasoning.effort", ("none", "low", "medium", "high", "xhigh", "max"), "medium",
            0.001, 0.0001, 0.006, 0.010,
            pricing_bands=(PricingBand("long-context", 272_001, 2.0, 2.0, 1.5),),
            cache_write_multiplier=1.25,
            selectable=False,
            description="Lightweight, high-volume responses",
        ),
        ModelSpec(
            "claude-sonnet-5-5", "Claude Sonnet 5.5", "anthropic", "Anthropic", True,
            "output_config.effort", ("low", "medium", "high", "xhigh", "max"), "high",
            0.002, 0.0002, 0.010, 0.010, False, 128_000, True,
            cache_write_multiplier=ANTHROPIC_CACHE_WRITE_MULTIPLIER,
            supports_forced_web_search=False,
            thinking_prefix_binding=True,
            description="Balanced reasoning and speed",
        ),
        ModelSpec(
            "claude-opus-5-5", "Claude Opus 5.5", "anthropic", "Anthropic", True,
            "output_config.effort", ("low", "medium", "high", "xhigh", "max"), "medium",
            0.004, 0.0002, 0.020, 0.010, False, 128_000, True,
            cache_write_multiplier=ANTHROPIC_CACHE_WRITE_MULTIPLIER,
            supports_forced_web_search=False,
            thinking_prefix_binding=True,
            description="Deep analysis for complex tasks",
        ),
        ModelSpec(
            "claude-sonnet-5", "Claude Sonnet 5", "anthropic", "Anthropic", True,
            "output_config.effort", ("low", "medium", "high", "xhigh", "max"), "high",
            0.003, 0.0003, 0.015, 0.010, False, 128_000, True,
            cache_write_multiplier=ANTHROPIC_CACHE_WRITE_MULTIPLIER,
            selectable=False,
            description="Balanced reasoning and speed",
        ),
        ModelSpec(
            "claude-opus-5", "Claude Opus 5", "anthropic", "Anthropic", True,
            "output_config.effort", ("low", "medium", "high", "xhigh", "max"), "high",
            0.005, 0.0005, 0.025, 0.010, False, 128_000, True,
            cache_write_multiplier=ANTHROPIC_CACHE_WRITE_MULTIPLIER,
            selectable=False,
            description="Deep analysis for complex tasks",
        ),
        ModelSpec(
            "claude-fable-5-1", "Claude Fable 5.1", "anthropic", "Anthropic", True,
            "output_config.effort", ("low", "medium", "high", "xhigh", "max"), "high",
            0.010, 0.00025, 0.050, 0.010, False, 128_000, True,
            cache_write_multiplier=ANTHROPIC_CACHE_WRITE_MULTIPLIER,
            supports_forced_web_search=False,
            # Too costly for the study budget; kept out of the picker and assignment.
            selectable=False,
            description="Long-horizon reasoning and research",
        ),
        # Kept so historical calls still price out and replay signed thinking.
        ModelSpec(
            "claude-opus-4-8", "Claude Opus 4.8", "anthropic", "Anthropic", True,
            "output_config.effort", ("low", "medium", "high", "xhigh", "max"), "high",
            0.005, 0.0005, 0.025, 0.010, False, 128_000, True,
            cache_write_multiplier=ANTHROPIC_CACHE_WRITE_MULTIPLIER,
            selectable=False,
            description="Deep analysis for complex tasks",
        ),
        ModelSpec(
            "claude-haiku-4-5-20251001", "Claude Haiku 4.5", "anthropic", "Anthropic", True,
            "none", ("none",), "none",
            0.001, 0.0001, 0.005, 0.010, True, 64_000, False,
            cache_write_multiplier=ANTHROPIC_CACHE_WRITE_MULTIPLIER,
            description="Fastest Claude for lightweight tasks",
        ),
        ModelSpec(
            "gemini-3.7-flash", "Gemini 3.7 Flash", "google", "Google", True,
            "thinking_level", ("low", "medium", "high"), "medium",
            # The study tariff excludes Google's introductory 2026 promotion.
            0.0015, 0.00015, 0.0075, 0.014,
            supports_forced_web_search=False,
            description="Fast model for coding and agentic tasks",
        ),
        ModelSpec(
            "gemini-3.8-flash", "Gemini 3.8 Flash", "google", "Google", True,
            "thinking_level", ("low", "medium", "high"), "medium",
            # Keep the frozen study tariff aligned with Gemini 3.7 Flash and
            # exclude the introductory pricing that ends after 2026.
            0.0015, 0.00015, 0.0075, 0.014,
            supports_forced_web_search=False,
            description="Frontier Flash for complex agentic tasks",
        ),
        # Kept for historical call repricing and exact-model Gemini continuation.
        ModelSpec(
            "gemini-3.5-flash", "Gemini 3.5 Flash", "google", "Google", True,
            "thinking_level", ("minimal", "low", "medium", "high"), "medium",
            0.0015, 0.00015, 0.009, 0.014,
            supports_forced_web_search=False,
            selectable=False,
            description="Fast responses for everyday tasks",
        ),
        ModelSpec(
            "gemini-3.1-pro-preview", "Gemini 3.1 Pro", "google", "Google", True,
            "thinking_level", ("low", "medium", "high"), "high",
            0.002, 0.0002, 0.012, 0.014,
            pricing_bands=(PricingBand("long-context", 200_001, 2.0, 2.0, 1.5),),
            supports_forced_web_search=False,
            description="Advanced reasoning and coding",
        ),
        ModelSpec(
            "gemini-3.5-flash-lite", "Gemini 3.5 Flash-Lite", "google", "Google", True,
            "thinking_level", ("minimal", "low", "medium", "high"), "minimal",
            0.0003, 0.00003, 0.0025, 0.014,
            supports_forced_web_search=False,
            description="Fast, low-cost responses",
        ),
    )
}


class ModelError(RuntimeError):
    pass


def provider_execution_mode(provider: str) -> str:
    if provider == "openai":
        return "live" if os.getenv("OPENAI_API_KEY", "").strip() else "unavailable"
    if provider == "anthropic":
        return "live" if os.getenv("ANTHROPIC_API_KEY", "").strip() else "unavailable"
    if provider == "google":
        key = os.getenv("GEMINI_API_KEY", "").strip() or os.getenv("GOOGLE_API_KEY", "").strip()
        return "live" if key else "unavailable"
    return "unavailable"


def list_models() -> list[dict]:
    return [model.public_dict(provider_execution_mode(model.provider)) for model in MODELS.values()]


def get_model(model_id: str) -> ModelSpec:
    try:
        return MODELS[model_id]
    except KeyError as exc:
        raise ModelError("MODEL_NOT_FOUND") from exc


def random_initial_settings(rng: random.Random | None = None) -> dict:
    """Draw the model setting a new participant starts with.

    Provider, then model, then reasoning effort, each uniformly. Option counts
    differ (OpenAI offers six efforts, Gemini three, Haiku one), so a flat draw
    over (model, effort) pairs would tilt toward models with more options.
    """
    rng = rng or random.SystemRandom()
    selectable = [model for model in MODELS.values() if model.selectable]
    provider = rng.choice(list(dict.fromkeys(model.provider for model in selectable)))
    model = rng.choice([row for row in selectable if row.provider == provider])
    return {
        "modelId": model.id,
        "reasoningEffort": rng.choice(model.reasoning_options),
        "thinkingEnabled": get_thinking_enabled(model),
    }


def get_reasoning_effort(model: ModelSpec, value: str | None) -> str:
    effort = str(value or model.default_reasoning).lower()
    if effort not in model.reasoning_options:
        raise ModelError("INVALID_REASONING_EFFORT")
    return effort


def get_thinking_enabled(model: ModelSpec, value: object = None) -> bool:
    """Whether this call actually thinks -- not whether a toggle exists.

    A model with no toggle still thinks when it is adaptive, and the recorded
    `modelCall.request.thinkingEnabled` has to say so or the research data
    misreports the condition the response was produced under.
    """
    if not model.supports_thinking_toggle:
        return model.always_thinks
    return True if value is None else bool(value)


def get_web_search_mode(value: object = None) -> str:
    """Normalize legacy booleans to the new auto/required search policy."""
    if value is True or str(value).lower() == "required":
        return "required"
    if value is False or value is None or str(value).lower() == "auto":
        return "auto"
    raise ModelError("INVALID_WEB_SEARCH_MODE")


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


def calculate_cost(
    model: ModelSpec,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    reasoning_tokens: int,
    search_queries: int,
    cache_write_input_tokens: int = 0,
) -> float:
    """Price one call. Input tokens split three ways: read from cache, written
    to cache, and neither. Cache reads are the cheapest and cache writes the
    most expensive, so the three shares are billed separately."""
    input_tokens = max(0, input_tokens)
    cached_input_tokens = min(max(0, cached_input_tokens), input_tokens)
    cache_write_input_tokens = min(
        max(0, cache_write_input_tokens), input_tokens - cached_input_tokens
    )
    uncached_input_tokens = input_tokens - cached_input_tokens - cache_write_input_tokens
    band = model.pricing_band(input_tokens)
    input_per_1k = model.input_per_1k * band.input_multiplier
    return round(
        (uncached_input_tokens / 1000) * input_per_1k
        + (cache_write_input_tokens / 1000) * input_per_1k * model.cache_write_multiplier
        + (cached_input_tokens / 1000) * model.cached_input_per_1k * band.cached_input_multiplier
        + ((output_tokens + reasoning_tokens) / 1000) * model.output_per_1k * band.output_multiplier
        + search_queries * model.search_cost,
        8,
    )
