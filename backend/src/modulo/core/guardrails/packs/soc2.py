"""SOC 2 policy pack content — Trust Services Criteria controls mapped to guardrails.

FAR-216 PR B. The pack framework ships in
:mod:`modulo.core.guardrails.policy_pack` (PR A); this module is the SOC 2
CONTENT — the concrete TSC controls and their guardrail mappings, expressed as
a CI-ready :class:`~modulo.core.guardrails.policy_pack.PolicyPack`.

The pack covers the input-side data-safety controls that deterministic
guardrails can enforce (regex / json_schema detection only — no LLM judges):

* **CC6.1** — logical & physical access control: block payloads carrying AWS
  access-key identifiers.
* **CC6.6** — malware prevention: block executable / script content markers.
* **CC7.2** — anomaly monitoring: observe SQL-injection / path-traversal
  markers (warn-mode-first — the control is advisory by default and is
  promoted to warn/block only after it proves not to false-positive).
* **CC8.1** — change management: warn on payloads that are not a well-formed
  object.
* **A1.2** — availability: warn on payloads that exceed size bounds.
* **P4.1** — personal-information handling: redact PII fields (SSN, credit
  card, email) via STATIC field-path redaction at the ingestion edge.

Every control is mapped and the pack passes ``assert_pack_ci_ready`` — zero
unmapped, zero uninstantiable. Roll it out warn-mode-first with
:func:`~modulo.core.guardrails.policy_pack.pack_rollout_config`.
"""

from __future__ import annotations

from modulo.core.guardrails import GuardrailAction
from modulo.core.guardrails.config import GuardrailConfigItem, GuardrailDetection, RedactionRule
from modulo.core.guardrails.policy_pack import PolicyControl, PolicyPack

# CC6.1 — AWS access-key identifiers (AKIA/ASIA prefix) are sourced from the
# canonical shared list in :mod:`modulo.core.secret_patterns` so the
# secret-format knowledge is never duplicated or drifted across redaction sites.
# ``GuardrailDetection`` requires a *string* pattern, so we use the compiled
# pattern's ``.pattern`` source string rather than the compiled object.
from modulo.core.secret_patterns import AWS_ACCESS_KEY_PATTERN as _AWS_ACCESS_KEY_RE

# ---------------------------------------------------------------------------
# Detection patterns (deterministic, linear — no nested quantifiers, no ReDoS)
# ---------------------------------------------------------------------------

# CC6.1 — AWS access-key identifiers: AKIA/ASIA prefix + 16 uppercase alnum.
# Core format sourced from the canonical shared list above; word boundaries are
# added here because the guardrail must not fire on a 21-char string that merely
# *starts* with a valid key (see test_soc2_pack_detection_patterns...[cc61]).
AWS_ACCESS_KEY_PATTERN = r"\b" + _AWS_ACCESS_KEY_RE.pattern + r"\b"

# CC6.6 — executable / script content markers: <script> tags, javascript:
# URI handlers, and the base64 of the DOS MZ/PE header ("TVpQ").
EXECUTABLE_CONTENT_PATTERN = r"(?:<script[^>]*>|javascript:|TVpQ)"

# CC7.2 — SQL-injection markers (' OR '1'='1 / OR 1=1) and path traversal.
# The SQLi alternative is case-insensitive ((?i:...)) because SQL keywords are
# case-insensitive — 'or 1=1' is an equally valid marker. The scope is confined
# to the tautology structure (1 ... = ... 1), which keeps benign text (e.g.
# 'or 1=2', 'score 1=1') from false-matching.
ANOMALY_PATTERN = r"(?:(?i:OR\s+['\"]?1['\"]?\s*=\s*['\"]?1)|\.\./)"

# P4.1 — PII marker detection for the redact-action guardrail (SSN, card, email).
SSN_PATTERN = r"\b\d{3}-\d{2}-\d{4}\b"
CARD_PATTERN = r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"
EMAIL_PATTERN = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
PII_DETECTION_PATTERN = rf"(?:{SSN_PATTERN}|{CARD_PATTERN}|{EMAIL_PATTERN})"

# Static redaction field paths (author config — NEVER payload-derived).
PII_REDACTION_PATHS: tuple[str, ...] = ("user.ssn", "user.credit_card", "user.email")

# CC8.1 — a well-formed change payload is a non-empty object.
WELL_FORMED_PAYLOAD_SCHEMA: dict[str, object] = {"type": "object", "minProperties": 1}

# A1.2 — bounded payload: at most 500 top-level keys, string values bounded.
SIZE_BOUNDED_PAYLOAD_SCHEMA: dict[str, object] = {
    "type": "object",
    "maxProperties": 500,
    "additionalProperties": {"maxLength": 100_000},
}


# ---------------------------------------------------------------------------
# Guardrail builders (one per control mapping)
# ---------------------------------------------------------------------------


def _cc61_access_key_block() -> GuardrailConfigItem:
    return GuardrailConfigItem(
        id="soc2-cc61-access-keys",
        name="Block AWS access keys in payloads",
        action=GuardrailAction.BLOCK,
        detection=GuardrailDetection(type="regex", pattern=AWS_ACCESS_KEY_PATTERN, field="body"),
    )


def _cc66_executable_content_block() -> GuardrailConfigItem:
    return GuardrailConfigItem(
        id="soc2-cc66-executable-content",
        name="Block executable content markers in payloads",
        action=GuardrailAction.BLOCK,
        detection=GuardrailDetection(type="regex", pattern=EXECUTABLE_CONTENT_PATTERN, field="body"),
    )


def _cc72_anomaly_observe() -> GuardrailConfigItem:
    return GuardrailConfigItem(
        id="soc2-cc72-anomaly-observe",
        name="Observe SQL injection and path-traversal markers",
        action=GuardrailAction.OBSERVE,
        detection=GuardrailDetection(type="regex", pattern=ANOMALY_PATTERN, field="body"),
    )


def _cc81_change_validation_warn() -> GuardrailConfigItem:
    return GuardrailConfigItem(
        id="soc2-cc81-change-validation",
        name="Warn on malformed change payloads",
        action=GuardrailAction.WARN,
        detection=GuardrailDetection(type="json_schema", schema=WELL_FORMED_PAYLOAD_SCHEMA),
    )


def _a12_payload_size_warn() -> GuardrailConfigItem:
    return GuardrailConfigItem(
        id="soc2-a12-payload-size",
        name="Warn on oversized payloads",
        action=GuardrailAction.WARN,
        detection=GuardrailDetection(type="json_schema", schema=SIZE_BOUNDED_PAYLOAD_SCHEMA),
    )


def _p41_pii_redact() -> GuardrailConfigItem:
    return GuardrailConfigItem(
        id="soc2-p41-pii-redact",
        name="Redact personal information fields",
        action=GuardrailAction.REDACT,
        detection=GuardrailDetection(type="regex", pattern=PII_DETECTION_PATTERN, field="body"),
        redaction=[RedactionRule(path=path, mode="transform") for path in PII_REDACTION_PATHS],
    )


# ---------------------------------------------------------------------------
# Pack assembly
# ---------------------------------------------------------------------------


def build_soc2_pack() -> PolicyPack:
    """Build the SOC 2 pack — every control mapped to a concrete guardrail.

    Returns a fresh :class:`PolicyPack` each call so callers never share
    mutable state; the module-level :data:`SOC2_PACK` constant is a single
    immutable instance of it.
    """
    controls = [
        PolicyControl(
            id="CC6.1",
            title="Logical and physical access control",
            description=(
                "Prevent unauthorized access to systems and data. The guardrail blocks payloads "
                "carrying AWS access-key identifiers (AKIA/ASIA prefixes), which would otherwise "
                "permit credential theft and unauthorized logical access."
            ),
            guardrail=_cc61_access_key_block(),
            mapped=True,
        ),
        PolicyControl(
            id="CC6.6",
            title="Prevent or detect malware",
            description=(
                "Detect and prevent executable/script content. The guardrail blocks payloads "
                "carrying script tags, javascript: URI handlers, or base64-encoded executable "
                "(MZ/PE) markers at the ingestion edge."
            ),
            guardrail=_cc66_executable_content_block(),
            mapped=True,
        ),
        PolicyControl(
            id="CC7.2",
            title="Monitor system components for anomalies",
            description=(
                "Observe suspicious input patterns — SQL-injection markers and path-traversal "
                "sequences — so anomaly evidence is surfaced before it reaches a model. The "
                "control is observe-mode (warn-mode-first) by design: it is promoted to "
                "warn/block only after it is proven not to false-positive on legitimate traffic."
            ),
            guardrail=_cc72_anomaly_observe(),
            mapped=True,
        ),
        PolicyControl(
            id="CC8.1",
            title="Validate changes before deployment",
            description=(
                "Validate payload structure at the ingestion edge: a change payload that is not "
                "a well-formed, non-empty object is flagged before it drives a run."
            ),
            guardrail=_cc81_change_validation_warn(),
            mapped=True,
        ),
        PolicyControl(
            id="A1.2",
            title="Maintain availability through capacity and performance monitoring",
            description=(
                "Bound payload size at the ingestion edge (top-level property count and string "
                "length) so oversized inputs cannot degrade pipeline availability."
            ),
            guardrail=_a12_payload_size_warn(),
            mapped=True,
        ),
        PolicyControl(
            id="P4.1",
            title="Personal information is identified and handled per privacy commitments",
            description=(
                "Mask personal-information fields (SSN, credit card, email) at the ingestion edge "
                "via STATIC field-path redaction, so personal data is never persisted in raw form "
                "(SOC 2 TSC privacy criteria; GDPR-adjacent data-privacy posture)."
            ),
            guardrail=_p41_pii_redact(),
            mapped=True,
        ),
    ]
    return PolicyPack(id="soc2", name="SOC 2 Trust Services Criteria", version="1.0.0", controls=controls)


SOC2_PACK: PolicyPack = build_soc2_pack()


__all__ = [
    "ANOMALY_PATTERN",
    "AWS_ACCESS_KEY_PATTERN",
    "CARD_PATTERN",
    "EMAIL_PATTERN",
    "EXECUTABLE_CONTENT_PATTERN",
    "PII_DETECTION_PATTERN",
    "PII_REDACTION_PATHS",
    "SIZE_BOUNDED_PAYLOAD_SCHEMA",
    "SOC2_PACK",
    "SSN_PATTERN",
    "WELL_FORMED_PAYLOAD_SCHEMA",
    "build_soc2_pack",
]
