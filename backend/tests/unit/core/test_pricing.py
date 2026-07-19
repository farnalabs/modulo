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
    @pytest.mark.parametrize(
        ("provider", "model", "expected_input", "expected_output"),
        [
            ("openai", "gpt-4o", 2.50, 10.00),
            ("openai", "gpt-4o-2024-08-06", 2.50, None),
            ("openai", "gpt-4o-mini-2024-07-18", 0.15, None),
            ("anthropic", "claude-sonnet-4-20250514", 3.00, None),
            ("anthropic", "claude-haiku-3.5-20241022", 0.80, None),
            ("deepseek", "deepseek-chat", 0.27, 1.10),
            ("deepseek", "deepseek-v3", 0.27, None),
            ("deepseek", "deepseek-r1", 0.55, None),
            ("groq", "llama-3.3-70b-versatile", 0.0, 0.0),
            ("groq", "mixtral-8x7b-32768", 0.0, None),
            ("perplexity", "sonar", 1.00, None),
            ("perplexity", "sonar-pro", 3.00, None),
            ("perplexity", "sonar-reasoning", 1.00, 5.00),
            ("togetherai", "mixtral-8x22b-instruct", 0.60, None),
            ("togetherai", "Llama-3.3-70B-Instruct-Turbo", 0.80, None),
            ("azure_openai", "gpt-4o-2024-08-06", 2.50, None),
        ],
    )
    def test_known_model_pricing(
        self, provider: str, model: str, expected_input: float, expected_output: float | None
    ) -> None:
        pricing = get_pricing(provider, model)
        assert pricing is not None
        assert pricing.input_price_per_1k == expected_input
        if expected_output is not None:
            assert pricing.output_price_per_1k == expected_output

    @pytest.mark.parametrize(
        ("provider", "model"),
        [
            ("nonexistent", "gpt-4o"),
            ("openai", "gpt-3.5-turbo"),
            ("anthropic", "gpt-4o"),
        ],
    )
    def test_unknown_returns_none(self, provider: str, model: str) -> None:
        pricing = get_pricing(provider, model)
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
