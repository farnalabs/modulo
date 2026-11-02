"""Redaction layers + the fail-closed gate (FAR-2305).

Every planted value here is obviously fake; the point is the SHAPE, which the
shared canonical patterns must recognise.
"""

from __future__ import annotations

import pytest

import modulo.support.redaction as redaction_module
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


# ---------------------------------------------------------------------------
# FAR-2305 review follow-up: a deployment credential can be a common word.
# ---------------------------------------------------------------------------


def test_weak_seed_value_is_scrubbed_only_in_a_credential_context() -> None:
    """`POSTGRES_PASSWORD=modulo` (the documented quickstart) must not mangle text."""
    seed = collect_seed_values("modulo")
    text = "\n".join(
        [
            "modulo version: 0.3.323",
            "requirepass modulo",
            "POSTGRES_PASSWORD=modulo",
            "redis://:modulo@127.0.0.1:6379/0",
        ]
    )
    result = redact_text(text, seed)
    assert "modulo version: 0.3.323" in result.text
    assert "requirepass modulo" not in result.text
    assert "POSTGRES_PASSWORD=modulo" not in result.text
    assert "modulo@127.0.0.1" not in result.text
    # Two occurrences came from the seed layer itself (requirepass + userinfo);
    # the KEY=value pair is handled by the structural layer.
    assert result.counts.seed_exact >= 2
    assert result.counts.structural >= 1


def test_distinctive_seed_value_is_scrubbed_anywhere() -> None:
    seed = collect_seed_values("Xk9!pQz7-weird-real-looking-pg-secret")
    result = redact_text("boot note Xk9!pQz7-weird-real-looking-pg-secret end", seed)
    assert "Xk9!pQz7-weird-real-looking-pg-secret" not in result.text
    assert result.text == "boot note <redacted> end"


def test_seed_tier_classification() -> None:
    from modulo.support.redaction import is_distinctive_seed

    assert not is_distinctive_seed("modulo")
    assert not is_distinctive_seed("changeme")
    assert is_distinctive_seed("p@ssword")
    assert is_distinctive_seed("hunter99")
    assert is_distinctive_seed("a" * 12)


def test_gate_accepts_the_same_weak_word_in_boilerplate() -> None:
    assert_redaction_proven(
        {"MANIFEST.txt": "modulo support bundle\nformat: modulo-support-bundle/v1"},
        collect_seed_values("modulo"),
        member_allowed=_allow,
    )


def test_gate_refuses_the_same_weak_word_behind_a_credential_key() -> None:
    with pytest.raises(RedactionSelfCheckError) as caught:
        assert_redaction_proven(
            {"logs/postgres.log.tail": "FATAL password=modulo rejected"},
            collect_seed_values("modulo"),
            member_allowed=_allow,
        )
    assert caught.value.member == "logs/postgres.log.tail"
    assert "credential context" in caught.value.reason


def test_empty_seed_value_is_skipped() -> None:
    """An empty pattern would match everywhere; the layer must ignore it."""
    result = redact_text("modulo version: 0.3.323", {"": REDACTED})
    assert result.text == "modulo version: 0.3.323"
    assert result.counts.seed_exact == 0


# ---------------------------------------------------------------------------
# FAR-2305 review: quoting / JSON between the key and the value must not hide a
# weak seed. The narrowed `[:=]`-only separator leaked every shape below (the
# pre-fix bare-substring gate covered them), and the gate re-scanned with the
# same detector, so the layer miss was invisible to the gate too.
# ---------------------------------------------------------------------------

#: Every separator shape a real leak takes: shell/redis quoting, YAML/TOML,
#: JSON (double- and single-quoted keys), and an env var with a quoted value.
QUOTED_LEAK_SHAPES = (
    'requirepass "modulo"',
    "requirepass 'modulo'",
    'password "modulo"',
    'AUTH "modulo"',
    '{"password": "modulo"}',
    "{'password': 'modulo'}",
    '{"POSTGRES_PASSWORD":"modulo"}',
)


@pytest.mark.parametrize("shape", QUOTED_LEAK_SHAPES)
def test_quoted_and_json_credential_shapes_are_scrubbed(shape: str) -> None:
    result = redact_text(shape, collect_seed_values("modulo"))
    assert "modulo" not in result.text
    assert REDACTED in result.text
    assert result.counts.seed_exact >= 1


@pytest.mark.parametrize("shape", QUOTED_LEAK_SHAPES)
def test_gate_refuses_quoted_and_json_shapes_when_the_scrub_layer_is_disabled(
    shape: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prove-the-fix: the gate, not only the scrub layer, recognises these shapes."""
    monkeypatch.setattr(redaction_module, "_scrub_seed", lambda text, _value, _replacement: (text, 0))
    with pytest.raises(RedactionSelfCheckError) as caught:
        assert_redaction_proven({"member.txt": shape}, collect_seed_values("modulo"), member_allowed=_allow)
    assert caught.value.member == "member.txt"
    assert "credential context" in caught.value.reason
    assert "modulo" not in str(caught.value)


@pytest.mark.parametrize("shape", QUOTED_LEAK_SHAPES)
def test_gate_independent_detector_refuses_when_the_structural_parse_misses(
    shape: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate must not share a blind spot with the layer's detector.

    With the shared structural detector neutralised, the second, independent
    proximity check still fails closed on every quoted/JSON shape.
    """
    monkeypatch.setattr(redaction_module, "_weak_seed_occurrences", lambda _text, _value: [])
    with pytest.raises(RedactionSelfCheckError) as caught:
        assert_redaction_proven({"member.txt": shape}, collect_seed_values("modulo"), member_allowed=_allow)
    assert caught.value.member == "member.txt"
    assert "proximity" in caught.value.reason


@pytest.mark.parametrize(
    "text",
    [
        "modulo support bundle\nformat: modulo-support-bundle/v1",
        "modulo version: 0.3.323",
        "FATAL: password authentication failed for user modulo",
        "auth failed for user modulo",
        "docs: modulo is a governance platform",
    ],
)
def test_widened_detector_does_not_flag_boilerplate_or_prose(text: str) -> None:
    """Widening the separator must not turn ordinary prose into a credential context."""
    result = redact_text(text, collect_seed_values("modulo"))
    assert result.text == text
    assert result.counts.seed_exact == 0
    assert_redaction_proven({"version.txt": text}, collect_seed_values("modulo"), member_allowed=_allow)


# ---------------------------------------------------------------------------
# FAR-2305 review: `AUTH <username> <password>` (the documented Redis 6 ACL
# form) puts the credential SECOND, so a FIRST-token-only parse and a byte
# window both missed it — the plaintext shipped in the log tail.
# ---------------------------------------------------------------------------

#: Every multi-token command shape a real shell trace / backtrace can echo: the
#: password second (plain, double- and single-quoted) and third with an extra
#: command token in between.
MULTI_TOKEN_AUTH_SHAPES = (
    "probe: redis-cli AUTH default modulo",
    'probe: redis-cli AUTH appuser "modulo"',
    "probe: redis-cli auth default 'modulo'",
    "probe: redis-cli AUTH alice something modulo",
)


@pytest.mark.parametrize("shape", MULTI_TOKEN_AUTH_SHAPES)
def test_multi_token_auth_shapes_are_scrubbed(shape: str) -> None:
    """The value is restored to the candidate set wherever it sits in the window."""
    result = redact_text(shape, collect_seed_values("modulo"))
    assert "modulo" not in result.text
    assert REDACTED in result.text
    assert result.counts.seed_exact >= 1


@pytest.mark.parametrize("shape", MULTI_TOKEN_AUTH_SHAPES)
def test_gate_refuses_multi_token_auth_shapes_when_the_scrub_layer_is_disabled(
    shape: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prove-the-fix: the gate, not only the scrub layer, sees past the username."""
    monkeypatch.setattr(redaction_module, "_scrub_seed", lambda text, _value, _replacement: (text, 0))
    with pytest.raises(RedactionSelfCheckError) as caught:
        assert_redaction_proven({"member.txt": shape}, collect_seed_values("modulo"), member_allowed=_allow)
    assert caught.value.member == "member.txt"
    assert "credential context" in caught.value.reason
    assert "modulo" not in str(caught.value)


@pytest.mark.parametrize("shape", MULTI_TOKEN_AUTH_SHAPES)
def test_gate_independent_token_window_refuses_multi_token_auth_shapes(
    shape: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The independent backstop is a TOKEN window, not a byte window.

    With the shared structural detector neutralised, the second check still
    refuses: an intervening username token must not push the value out of reach.
    """
    monkeypatch.setattr(redaction_module, "_weak_seed_occurrences", lambda _text, _value: [])
    with pytest.raises(RedactionSelfCheckError) as caught:
        assert_redaction_proven({"member.txt": shape}, collect_seed_values("modulo"), member_allowed=_allow)
    assert caught.value.member == "member.txt"
    assert "proximity" in caught.value.reason


@pytest.mark.parametrize(
    "text",
    [
        "FATAL: password authentication failed for user modulo",
        "auth failed for user modulo",
        "docs: modulo is a governance platform",
    ],
)
def test_prose_with_a_credential_keyword_and_the_weak_word_is_still_accepted(text: str) -> None:
    """The token window stops short of prose: five tokens away is not a command."""
    result = redact_text(text, collect_seed_values("modulo"))
    assert result.text == text
    assert result.counts.seed_exact == 0
    assert_redaction_proven({"version.txt": text}, collect_seed_values("modulo"), member_allowed=_allow)


def test_weak_seed_userinfo_is_still_matched() -> None:
    """The URL-userinfo position survived the separator rewrite."""
    result = redact_text("redis://:modulo@127.0.0.1:6379/0", collect_seed_values("modulo"))
    assert "modulo" not in result.text
    assert result.counts.seed_exact == 1


def test_percent_encoded_seed_form_is_still_matched() -> None:
    """Percent-encoded forms remain distinctive and are scrubbed verbatim."""
    from urllib.parse import quote

    password = "p@ssword"
    encoded = quote(password, safe="")
    result = redact_text(f"redis://:{encoded}@host/0", collect_seed_values(password))
    assert encoded not in result.text
    assert password not in result.text


def test_gate_ignores_an_empty_seed_value() -> None:
    """An empty seed key must not make the gate refuse every member."""
    assert_redaction_proven({"version.txt": "modulo version: 0.3.323"}, {"": REDACTED}, member_allowed=_allow)


@pytest.mark.parametrize(
    "shape",
    [
        "requirepass modulox",
        "requirepass xmodulo",
        '{"password": "modulox"}',
        '{"password": "xmodulo"}',
    ],
)
def test_seed_inside_a_longer_credential_value_is_not_a_leak(shape: str) -> None:
    """A credential value that merely contains the word is a different value."""
    result = redact_text(shape, collect_seed_values("modulo"))
    assert result.text == shape
    assert result.counts.seed_exact == 0
    assert_redaction_proven({"member.txt": shape}, collect_seed_values("modulo"), member_allowed=_allow)
