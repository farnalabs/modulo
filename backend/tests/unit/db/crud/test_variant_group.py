"""Unit tests for variant group CRUD — pure functions only (no DB)."""

import uuid

from modulo.db.crud.variant_group import pick_variant_weighted


class TestPickVariantWeighted:
    def test_empty_variants_returns_none(self) -> None:
        assert pick_variant_weighted([]) is None

    def test_single_variant_returns_directly(self) -> None:
        variant = {"name": "control", "snapshot_id": str(uuid.uuid4()), "weight": 1.0}
        assert pick_variant_weighted([variant]) is variant

    def test_weighted_selection_respects_weights(self) -> None:
        variants = [
            {"name": "control", "weight": 99.0},
            {"name": "variant_a", "weight": 1.0},
        ]
        # Run many iterations to ensure both can be picked.
        selections: set[str] = set()
        for _ in range(2000):
            v = pick_variant_weighted(variants)
            if v:
                selections.add(v["name"])
        assert "control" in selections
        assert "variant_a" in selections

    def test_all_zero_weights_falls_back_to_random(self) -> None:
        variants = [
            {"name": "a", "weight": 0.0},
            {"name": "b", "weight": 0.0},
        ]
        result = pick_variant_weighted(variants)
        assert result in variants

    def test_missing_weight_defaults_to_1(self) -> None:
        variants = [
            {"name": "a"},
            {"name": "b"},
        ]
        result = pick_variant_weighted(variants)
        assert result is not None
        assert result["name"] in ("a", "b")

    def test_weighted_selection_distribution(self) -> None:
        variants = [
            {"name": "control", "weight": 100},
            {"name": "variant_a", "weight": 1},
        ]
        control_count = 0
        trials = 5000
        for _ in range(trials):
            result = pick_variant_weighted(variants)
            assert result is not None
            if result["name"] == "control":
                control_count += 1
        # Control should be picked the vast majority of the time.
        assert control_count > trials * 0.85

    def test_variants_without_weight_key(self) -> None:
        variants = [{"name": "x"}, {"name": "y"}, {"name": "z"}]
        seen: set[str] = set()
        for _ in range(300):
            result = pick_variant_weighted(variants)
            assert result is not None
            seen.add(result["name"])
        assert seen == {"x", "y", "z"}
