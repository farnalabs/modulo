"""YAML loader for ``modulo apply`` (FAR-681, slice 1).

Accepts single- or multi-document YAML files. Every document must validate
as an ApplyConfig; entities from all documents are merged into one config.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from modulo.cli.apply.models import ApplyConfig, ApplyConfigError


class ApplyLoadError(ValueError):
    """Raised when an apply YAML file cannot be loaded/validated."""


def parse_apply_documents(text: str) -> ApplyConfig:
    """Parse YAML text (single- or multi-doc) into one merged ApplyConfig."""
    try:
        docs = list(yaml.safe_load_all(text))
    except yaml.YAMLError as exc:
        msg = f"YAML parse error: {exc}"
        raise ApplyLoadError(msg) from None
    docs = [d for d in docs if d is not None]
    if not docs:
        msg = "apply config file is empty"
        raise ApplyLoadError(msg)
    merged: ApplyConfig | None = None
    for index, doc in enumerate(docs):
        if not isinstance(doc, dict):
            msg = f"YAML document {index} must be a mapping, got {type(doc).__name__}"
            raise ApplyLoadError(msg)
        try:
            config = ApplyConfig.model_validate(doc)
        except ValidationError as exc:
            msg = f"YAML document {index} is not a valid apply config: {exc}"
            raise ApplyLoadError(msg) from None
        if merged is None:
            merged = config
        else:
            try:
                merged = merged.merge_entities(config)
            except ApplyConfigError as exc:
                msg = f"YAML documents {index - 1} and {index} are incompatible: {exc}"
                raise ApplyLoadError(msg) from None
    assert merged is not None
    return merged


def load_apply_file(path: Path) -> ApplyConfig:
    """Load an apply config from disk, normalising parse/IO failures."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"cannot read apply config file {path}: {exc}"
        raise ApplyLoadError(msg) from None
    return parse_apply_documents(text)
