"""Redaction layers + the fail-closed gate (FAR-2305).

Every planted value here is obviously fake; the point is the SHAPE, which the
shared canonical patterns must recognise.
"""

from __future__ import annotations

import pytest

from modulo.support.redaction import (
    REDACTED,
    RedactionSelfCheckError,
    assert_redaction_proven,
    collect_seed_values,
    is_sensitive_key,
    redact_kv_lines,
    redact_text,
)

POSTGRES_PASSWORD = "Xk9!pQz7-weird-real-looking-pg-secret"
GITHUB_PAT = "ghp_" + "A" * 36
OPENAI_KEY = "sk-" + "a" * 26
BEARER_TOKEN = "Bearer fAkE-token-value-123456"
FAKE_PRIVATE_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\nZmFrZWZha2VmYWtlZmFrZWZha2VmYWtlZmFrZWZha2U=\n-----END RSA PRIVATE KEY-----"
)


def _allow(_name: str) -> bool:
    return True


def test_seeded_values_are_scrubbed_including_percent_encoded() -> None:
    from urllib.parse import quote

    password = "p@ssword"
    secret_values = collect_seed_values(password)
    text = f"postgresql://modulo:{password}@127.0.0.1/db redis://:{quote(password, safe='')}@host/0"
    result = redact_text(text, secret_values)
    assert password not in result.text
    assert quote(password, safe="") not in result.text
    assert result.counts.seed_exact >= 2


def test_structural_kv_masks_canonical_sensitive_keys() -> None:
    text = "\n".join(
        [
            "SECRET_KEY=not-a-real-secret",
            "ALERT_WEBHOOK_URL=https://hooks.example.com/services/fake",
            "MODULO_USERS=admin@example.com:fake-hash",
            "PLAIN_VALUE=forty-two",
        ]
    )
    redacted, substitutions = redact_kv_lines(text)
    assert substitutions == 3
    assert "SECRET_KEY=<redacted>" in redacted
    assert "ALERT_WEBHOOK_URL=<redacted>" in redacted
    assert "MODULO_USERS=<redacted>" in redacted
    assert "PLAIN_VALUE=forty-two" in redacted


@pytest.mark.parametrize(
    "planted",
    [
        GITHUB_PAT,
        OPENAI_KEY,
        BEARER_TOKEN,
        FAKE_PRIVATE_KEY,
        "redis://modulo:Xk9!pQz7-weird-real-looking-pg-secret@127.0.0.1:16379/0",
    ],
)
def test_pattern_layer_masks_shared_secret_shapes(planted: str) -> None:
    result = redact_text(f"before {planted} after", {})
    assert result.counts.patterns >= 1
    assert planted not in result.text
    assert "before" in result.text
    assert "after" in result.text


def test_counts_record_each_layer_that_applied() -> None:
    secret = "generated-value-that-is-seeded"
    text = f"password={secret}\n{GITHUB_PAT}\n"
    result = redact_text(text, collect_seed_values(secret))
    assert result.counts.seed_exact >= 1
    assert result.counts.structural >= 1
    assert result.counts.patterns >= 1
    assert result.counts.total == (result.counts.seed_exact + result.counts.structural + result.counts.patterns)
    assert set(result.counts.applied_layers()) == {"seed_exact", "structural", "patterns"}


def test_gate_refuses_a_surviving_seeded_value() -> None:
    with pytest.raises(RedactionSelfCheckError) as caught:
        assert_redaction_proven(
            {"doctor.txt": f"leaked {POSTGRES_PASSWORD}"},
            collect_seed_values(POSTGRES_PASSWORD),
            member_allowed=_allow,
        )
    assert caught.value.member == "doctor.txt"
    assert POSTGRES_PASSWORD not in str(caught.value)


def test_gate_refuses_secret_shaped_content_without_a_seed_map() -> None:
    """The gate is the backstop for a shape no earlier layer happened to cover."""
    with pytest.raises(RedactionSelfCheckError) as caught:
        assert_redaction_proven({"runs.json": f'{{"note": "{GITHUB_PAT}"}}'}, {}, member_allowed=_allow)
    assert caught.value.member == "runs.json"


def test_gate_refuses_a_surviving_sensitive_kv_pair() -> None:
    with pytest.raises(RedactionSelfCheckError) as caught:
        assert_redaction_proven({"settings.txt": "SECRET_KEY=still-here"}, {}, member_allowed=_allow)
    assert caught.value.member == "settings.txt"


def test_gate_refuses_an_unknown_member() -> None:
    with pytest.raises(RedactionSelfCheckError) as caught:
        assert_redaction_proven({"future-member.txt": "clean"}, {}, member_allowed=lambda name: name == "version.txt")
    assert caught.value.member == "future-member.txt"


def test_gate_refuses_the_raw_secrets_file() -> None:
    with pytest.raises(RedactionSelfCheckError) as caught:
        assert_redaction_proven({"secrets.json": "{}"}, {}, member_allowed=_allow)
    assert "secrets file" in caught.value.reason


def test_gate_accepts_clean_members() -> None:
    assert_redaction_proven(
        {
            "version.txt": "modulo version: 0.3.323\nos: Linux",
            "MANIFEST.txt": "format: modulo-support-bundle/v1\nmissing: []",
            "report.json": '{"format": "modulo-support-bundle/v1"}',
        },
        collect_seed_values(POSTGRES_PASSWORD),
        member_allowed=_allow,
    )


def test_gate_does_not_false_positive_on_the_redaction_markers() -> None:
    """A short seed value can be a substring of '<redacted>'; that is not a leak."""
    assert_redaction_proven({"doctor.txt": "value=<redacted>"}, {"redacted": REDACTED}, member_allowed=_allow)


def test_sensitive_key_classifier_matches_modulo_env() -> None:
    assert is_sensitive_key("SECRET_KEY")
    assert is_sensitive_key("ALERT_WEBHOOK_URL")
    assert is_sensitive_key("MODULO_USERS")
    assert not is_sensitive_key("MODULO_DB")
    assert not is_sensitive_key("PLAN_TIER")
