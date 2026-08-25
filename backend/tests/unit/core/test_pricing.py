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
            ("openai", "gpt-4o-2024-08-06", 2.50, 10.00),
            ("openai", "gpt-4o-mini-2024-07-18", 0.15, 0.60),
            ("anthropic", "claude-sonnet-4-20250514", 3.00, 15.00),
            ("anthropic", "claude-haiku-3.5-20241022", 0.80, 4.00),
            ("deepseek", "deepseek-chat", 0.27, 1.10),
            ("deepseek", "deepseek-v3", 0.27, 1.10),
            ("deepseek", "deepseek-r1", 0.55, 2.19),
            ("groq", "llama-3.3-70b-versatile", 0.0, 0.0),
            ("groq", "mixtral-8x7b-32768", 0.0, 0.0),
            ("perplexity", "sonar", 1.00, 1.00),
            ("perplexity", "sonar-pro", 3.00, 3.00),
            ("perplexity", "sonar-reasoning", 1.00, 5.00),
            ("togetherai", "mixtral-8x22b-instruct", 0.60, 0.60),
            ("togetherai", "Llama-3.3-70B-Instruct-Turbo", 0.80, 0.80),
            ("azure_openai", "gpt-4o-2024-08-06", 2.50, 10.00),
            ("azure_openai", "gpt-4o-mini-2024-07-18", 0.15, 0.60),
            ("azure_openai", "o4-mini", 1.10, 4.40),
        ],
        ids=[
            "gpt-4o",
            "gpt-4o-2024-08-06",
            "gpt-4o-mini-2024-07-18",
            "claude-sonnet-4-20250514",
            "claude-haiku-3.5-20241022",
            "deepseek-chat",
            "deepseek-v3",
            "deepseek-r1",
            "llama-3.3-70b-versatile",
            "mixtral-8x7b-32768",
            "sonar",
            "sonar-pro",
            "sonar-reasoning",
            "mixtral-8x22b-instruct",
            "llama-3.3-70b-instruct-turbo",
            "azure-gpt-4o-2024-08-06",
            "azure-gpt-4o-mini-2024-07-18",
            "azure-o4-mini",
        ],
    )
    def test_known_model_pricing(
        self, provider: str, model: str, expected_input: float, expected_output: float
    ) -> None:
        pricing = get_pricing(provider, model)
        assert pricing is not None
        assert pricing.input_price_per_1k == expected_input
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

    def test_provider_match_is_exact_and_case_sensitive(self) -> None:
        assert get_pricing("OPENAI", "gpt-4o") is None
        assert get_pricing("openai", "gpt-4o") is not None

    def test_provider_specific_star_pattern_does_not_leak(self) -> None:
        # groq is the only provider with a bare "*" pattern; a model that is
        # unknown to another provider must not be backfilled by groq's wildcard.
        assert get_pricing("openai", "totally-unknown-model") is None
        assert get_pricing("deepseek", "totally-unknown-model") is None

    @pytest.mark.parametrize(
        ("model", "expected_pattern"),
        [
            ("gpt-4o", "gpt-4o"),
            ("gpt-4o-2024-08-06", "gpt-4o*"),
            ("gpt-4o-mini-2024-07-18", "gpt-4o-mini*"),
            ("o3-mini", "o3*"),
            ("o4-mini-2024-07-18", "o4-mini*"),
        ],
    )
    def test_specific_pattern_matches_before_generic(self, model: str, expected_pattern: str) -> None:
        # The specific-* entry must win over the broader sibling (e.g. gpt-4o-mini*
        # over gpt-4o*) because it appears earlier in PRICING_TABLE.
        pricing = get_pricing("openai", model)
        assert pricing is not None
        assert pricing.model_pattern == expected_pattern

    def test_model_with_trailing_dots_and_slash(self) -> None:
        pricing = get_pricing("openai", "gpt-4o/2024")
        assert pricing is not None
        assert pricing.model_pattern == "gpt-4o*"


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
