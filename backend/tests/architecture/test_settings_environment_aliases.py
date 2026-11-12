"""Structural checks for canonical FARNA_CONTROL_ environment aliases."""

from __future__ import annotations

from typing import Any

from farna.settings import Settings


def _alias_names(field: Any) -> tuple[str, ...]:
    validation_alias = field.validation_alias
    choices = getattr(validation_alias, "choices", None)
    if choices is not None:
        return tuple(choices)
    if isinstance(validation_alias, str):
        return (validation_alias,)
    return ()


def test_modulo_settings_have_single_canonical_prefix() -> None:
    modulo_fields = {name: field for name, field in Settings.model_fields.items() if name.startswith("modulo_")}

    assert modulo_fields
    for name, field in modulo_fields.items():
        aliases = _alias_names(field)
        assert aliases, f"{name} must declare validation_alias"
        assert aliases[0].startswith("FARNA_CONTROL_")
        assert not aliases[0].startswith("FARNA_CONTROL_MODULO_")


def test_no_product_canonical_alias_uses_doubled_prefix() -> None:
    """Doubled FARNA_CONTROL_MODULO_ names remain fallbacks, never canonical."""
    for name, field in Settings.model_fields.items():
        aliases = _alias_names(field)
        if name.startswith("modulo_"):
            assert not aliases[0].startswith("FARNA_CONTROL_MODULO_")
