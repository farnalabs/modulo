"""Unit tests for modulo.core.pricing."""

import pytest

from modulo.core.pricing import PRICING_TABLE, PricingConfig, get_pricing


class TestPricingConfig:
    def test_dataclass_fields(self) -> None:
        cfg = PricingConfig("openai", "gpt-4o", 2.50, 10.00)
        assert cfg.provider == "openai"
        assert cfg.model_pattern == "gpt-4o"
        assert cfg.input_price_per_1k == 2.50
        assert cfg.output_price_per_1k == 10.00
        assert cfg.currency == "USD"

    def test_custom_currency(self) -> None:
        cfg = PricingConfig("test", "*", 1.0, 2.0, currency="EUR")
        assert cfg.currency == "EUR"

    def test_frozen(self) -> None:
        cfg = PricingConfig("test", "*", 1.0, 2.0)
        with pytest.raises(AttributeError):
            cfg.input_price_per_1k = 5.0  # type: ignore[misc]


class TestGetPricing:
    def test_known_openai_model(self) -> None:
        pricing = get_pricing("openai", "gpt-4o")
        assert pricing is not None
        assert pricing.input_price_per_1k == 2.50
        assert pricing.output_price_per_1k == 10.00

    def test_known_openai_model_variant(self) -> None:
        pricing = get_pricing("openai", "gpt-4o-2024-08-06")
        assert pricing is not None
        assert pricing.input_price_per_1k == 2.50

    def test_known_openai_mini_variant(self) -> None:
        pricing = get_pricing("openai", "gpt-4o-mini-2024-07-18")
        assert pricing is not None
        assert pricing.input_price_per_1k == 0.15

    def test_known_anthropic_model(self) -> None:
        pricing = get_pricing("anthropic", "claude-sonnet-4-20250514")
        assert pricing is not None
        assert pricing.input_price_per_1k == 3.00

    def test_known_anthropic_haiku(self) -> None:
        pricing = get_pricing("anthropic", "claude-haiku-3.5-20241022")
        assert pricing is not None
        assert pricing.input_price_per_1k == 0.80

    def test_known_deepseek_chat(self) -> None:
        pricing = get_pricing("deepseek", "deepseek-chat")
        assert pricing is not None
        assert pricing.input_price_per_1k == 0.27
        assert pricing.output_price_per_1k == 1.10

    def test_known_deepseek_v3(self) -> None:
        pricing = get_pricing("deepseek", "deepseek-v3")
        assert pricing is not None
        assert pricing.input_price_per_1k == 0.27

    def test_known_deepseek_r1(self) -> None:
        pricing = get_pricing("deepseek", "deepseek-r1")
        assert pricing is not None
        assert pricing.input_price_per_1k == 0.55

    def test_groq_free_tier(self) -> None:
        pricing = get_pricing("groq", "llama-3.3-70b-versatile")
        assert pricing is not None
        assert pricing.input_price_per_1k == 0.0
        assert pricing.output_price_per_1k == 0.0

    def test_groq_any_model(self) -> None:
        pricing = get_pricing("groq", "mixtral-8x7b-32768")
        assert pricing is not None
        assert pricing.input_price_per_1k == 0.0

    def test_perplexity_sonar(self) -> None:
        pricing = get_pricing("perplexity", "sonar")
        assert pricing is not None
        assert pricing.input_price_per_1k == 1.00

    def test_perplexity_sonar_pro(self) -> None:
        pricing = get_pricing("perplexity", "sonar-pro")
        assert pricing is not None
        assert pricing.input_price_per_1k == 3.00

    def test_perplexity_sonar_reasoning(self) -> None:
        pricing = get_pricing("perplexity", "sonar-reasoning")
        assert pricing is not None
        assert pricing.input_price_per_1k == 1.00
        assert pricing.output_price_per_1k == 5.00

    def test_togetherai_mixtral(self) -> None:
        pricing = get_pricing("togetherai", "mixtral-8x22b-instruct")
        assert pricing is not None
        assert pricing.input_price_per_1k == 0.60

    def test_togetherai_llama(self) -> None:
        pricing = get_pricing("togetherai", "Llama-3.3-70B-Instruct-Turbo")
        assert pricing is not None
        assert pricing.input_price_per_1k == 0.80

    def test_azure_openai(self) -> None:
        pricing = get_pricing("azure_openai", "gpt-4o-2024-08-06")
        assert pricing is not None
        assert pricing.input_price_per_1k == 2.50

    def test_unknown_provider_returns_none(self) -> None:
        pricing = get_pricing("nonexistent", "gpt-4o")
        assert pricing is None

    def test_unknown_model_returns_none(self) -> None:
        pricing = get_pricing("openai", "gpt-3.5-turbo")
        assert pricing is None

    def test_wrong_provider_returns_none(self) -> None:
        pricing = get_pricing("anthropic", "gpt-4o")
        assert pricing is None


class TestPricingTable:
    def test_all_paid_models_have_positive_prices(self) -> None:
        for entry in PRICING_TABLE:
            if entry.provider == "groq":
                continue
            assert entry.input_price_per_1k > 0, f"{entry.provider}/{entry.model_pattern} has zero input price"
            assert entry.output_price_per_1k > 0, f"{entry.provider}/{entry.model_pattern} has zero output price"

    def test_groq_is_free(self) -> None:
        for entry in PRICING_TABLE:
            if entry.provider == "groq":
                assert entry.input_price_per_1k == 0.0
                assert entry.output_price_per_1k == 0.0

    def test_table_is_not_empty(self) -> None:
        assert len(PRICING_TABLE) > 0

    def test_duplicates_take_first_match(self) -> None:
        gpt4o_exact = get_pricing("openai", "gpt-4o")
        gpt4o_star = get_pricing("openai", "gpt-4o-anything")
        assert gpt4o_exact is not None
        assert gpt4o_star is not None
        assert gpt4o_exact.model_pattern == "gpt-4o"
        assert gpt4o_star.model_pattern == "gpt-4o*"
