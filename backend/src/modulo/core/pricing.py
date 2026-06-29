"""Configurable pricing for model backends.

Provides a PRICING_TABLE of known model costs and a get_pricing() lookup
that uses glob/prefix matching on model_id.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class PricingConfig:
    provider: str
    model_pattern: str  # glob/prefix pattern, e.g. "gpt-4o*"
    input_price_per_1k: float  # USD per 1K input tokens
    output_price_per_1k: float  # USD per 1K output tokens
    currency: str = "USD"


PRICING_TABLE: Final[list[PricingConfig]] = [
    # ── OpenAI ──────────────────────────────────────────────────────────────
    # Specific patterns BEFORE generic ones so fnmatch hits the right entry first
    PricingConfig("openai", "gpt-4o-mini", 0.15, 0.60),
    PricingConfig("openai", "gpt-4o-mini*", 0.15, 0.60),
    PricingConfig("openai", "gpt-4o", 2.50, 10.00),
    PricingConfig("openai", "gpt-4o*", 2.50, 10.00),
    PricingConfig("openai", "o3", 10.00, 40.00),
    PricingConfig("openai", "o3*", 10.00, 40.00),
    PricingConfig("openai", "o4-mini", 1.10, 4.40),
    PricingConfig("openai", "o4-mini*", 1.10, 4.40),
    # ── Anthropic ───────────────────────────────────────────────────────────
    PricingConfig("anthropic", "claude-sonnet-4", 3.00, 15.00),
    PricingConfig("anthropic", "claude-sonnet-4*", 3.00, 15.00),
    PricingConfig("anthropic", "claude-sonnet-4.5", 3.00, 15.00),
    PricingConfig("anthropic", "claude-sonnet-4.5*", 3.00, 15.00),
    PricingConfig("anthropic", "claude-haiku-3.5", 0.80, 4.00),
    PricingConfig("anthropic", "claude-haiku-3.5*", 0.80, 4.00),
    # ── Groq (free tier) ────────────────────────────────────────────────────
    PricingConfig("groq", "*", 0.0, 0.0),
    # ── DeepSeek ────────────────────────────────────────────────────────────
    PricingConfig("deepseek", "deepseek-chat", 0.27, 1.10),
    PricingConfig("deepseek", "deepseek-chat*", 0.27, 1.10),
    PricingConfig("deepseek", "deepseek-reasoner", 0.55, 2.19),
    PricingConfig("deepseek", "deepseek-reasoner*", 0.55, 2.19),
    PricingConfig("deepseek", "deepseek-v3", 0.27, 1.10),
    PricingConfig("deepseek", "deepseek-v3*", 0.27, 1.10),
    PricingConfig("deepseek", "deepseek-r1", 0.55, 2.19),
    PricingConfig("deepseek", "deepseek-r1*", 0.55, 2.19),
    # ── Perplexity ──────────────────────────────────────────────────────────
    PricingConfig("perplexity", "sonar-reasoning*", 1.00, 5.00),
    PricingConfig("perplexity", "sonar-pro*", 3.00, 3.00),
    PricingConfig("perplexity", "sonar*", 1.00, 1.00),
    # ── TogetherAI ──────────────────────────────────────────────────────────
    PricingConfig("togetherai", "mistral*", 0.60, 0.60),
    PricingConfig("togetherai", "mixtral*", 0.60, 0.60),
    PricingConfig("togetherai", "llama*", 0.80, 0.80),
    PricingConfig("togetherai", "Llama*", 0.80, 0.80),
    # ── Azure OpenAI (same as OpenAI) ───────────────────────────────────────
    PricingConfig("azure_openai", "gpt-4o*", 2.50, 10.00),
    PricingConfig("azure_openai", "gpt-4o-mini*", 0.15, 0.60),
    PricingConfig("azure_openai", "o3*", 10.00, 40.00),
    PricingConfig("azure_openai", "o4-mini*", 1.10, 4.40),
]


def get_pricing(provider: str, model_id: str) -> PricingConfig | None:
    """Look up pricing for *provider*/*model_id* using glob matching.

    Iterates ``PRICING_TABLE`` in definition order and returns the **first**
    entry whose ``provider`` matches exactly and whose ``model_pattern``
    matches *model_id* via :func:`fnmatch.fnmatch`.

    Returns *None* when no entry matches.
    """
    for entry in PRICING_TABLE:
        if entry.provider == provider and fnmatch.fnmatch(model_id, entry.model_pattern):
            return entry
    return None
