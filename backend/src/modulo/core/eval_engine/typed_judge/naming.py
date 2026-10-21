"""Judge display composition — the ONE place a judge label is built (ADR 066 §0).

The naming rule is enforced structurally, not by review discipline: every
display string is composed here, so no view, route or template can invent a
label that presents a generative LLM as a calibrated decision model.

The mechanism is always "Typed questions". A backing is rendered as
``<mechanism> — <vendor/model> (<evidence>)``, where the evidence names what
the confidence value actually is:

* ``vendor_claimed_calibrated`` — a single-pass decision model whose vendor
  claims calibration (e.g. Jev).
* ``model_self_reported`` — a model backend returning a number in the same
  field with a different meaning. This label never contains the phrase
  "System One": an LLM prompting a JSON schema is not a decision model.

Confidence values of different kinds are never averaged or shown on one scale
(ADR 066 §0). :func:`confidence_label` is the only label a confidence band may
carry.
"""

from __future__ import annotations

from modulo.core.eval_engine.typed_judge.envelope import JudgeConfidenceKind

#: The mechanism label used on every user-facing surface.
TYPED_QUESTIONS_MECHANISM = "Typed questions"

#: The ONE legal spelling of the decision-model class. It names a model class,
#: never a request format or a mode.
SYSTEM_ONE_MODEL_LABEL = "System One model"

#: Evidence phrase per confidence provenance.
_EVIDENCE_BY_KIND: dict[JudgeConfidenceKind, str] = {
    JudgeConfidenceKind.VENDOR_CLAIMED_CALIBRATED: "vendor-claimed calibration",
    JudgeConfidenceKind.MODEL_SELF_REPORTED: "LLM, self-reported confidence",
    JudgeConfidenceKind.UNAVAILABLE: "confidence unavailable",
}

#: Human label per confidence provenance — never compressed to a number.
_CONFIDENCE_LABEL_BY_KIND: dict[JudgeConfidenceKind, str] = {
    JudgeConfidenceKind.VENDOR_CLAIMED_CALIBRATED: "Vendor-claimed calibrated",
    JudgeConfidenceKind.MODEL_SELF_REPORTED: "Model self-reported",
    JudgeConfidenceKind.UNAVAILABLE: "Unavailable",
}


def _coerce_kind(value: JudgeConfidenceKind | str) -> JudgeConfidenceKind:
    try:
        return JudgeConfidenceKind(value)
    except ValueError as exc:
        raise ValueError(f"Unknown judge confidence kind: {value!r}") from exc


def confidence_label(kind: JudgeConfidenceKind | str) -> str:
    """Return the label for a confidence value's provenance."""
    return _CONFIDENCE_LABEL_BY_KIND[_coerce_kind(kind)]


def mechanism_label() -> str:
    """Return the mechanism label shared by every backing."""
    return TYPED_QUESTIONS_MECHANISM


def backing_display(
    *,
    kind: str,
    vendor: str | None = None,
    model: str | None = None,
    confidence_kind: JudgeConfidenceKind | str = JudgeConfidenceKind.UNAVAILABLE,
) -> str:
    """Compose a backing's display string (ADR 066 §0).

    ``kind`` is the backing axis (``decision_model`` | ``model_backend``), never
    an ``eval_type``. A model-backed display is proven by the naming test to
    never contain the decision-model phrase.
    """
    provenance = _coerce_kind(confidence_kind)
    if kind == "decision_model":
        asked = vendor or model or "decision model"
        return f"{TYPED_QUESTIONS_MECHANISM} — {asked} ({SYSTEM_ONE_MODEL_LABEL})"
    if kind == "model_backend":
        asked = model or "model"
        if vendor:
            asked = f"{asked} via {vendor}"
        return f"{TYPED_QUESTIONS_MECHANISM} — {asked} ({_EVIDENCE_BY_KIND[provenance]})"
    raise ValueError(f"Unknown judge backing kind: {kind!r}")


__all__ = [
    "SYSTEM_ONE_MODEL_LABEL",
    "TYPED_QUESTIONS_MECHANISM",
    "backing_display",
    "confidence_label",
    "mechanism_label",
]
