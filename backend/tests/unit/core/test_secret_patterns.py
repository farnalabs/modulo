"""Unit tests for modulo.core.secret_patterns — the canonical secret-VALUE
redaction patterns and the ReDoS-bounded masking helper.

The module is the single source of truth for gitleaks-style secret-VALUE
masking (AWS keys, GitHub tokens, private keys, connection strings, ...) and
carries two safety-critical properties this file locks in:

* non-secret content is NEVER truncated (the earlier 5000-char cap silently
  dropped long benign strings such as LLM answers, code blocks or log tails);
* the nested-quantifier patterns (private-key blocks, connection strings) are
  only ever fed ReDoS-bounded windows of :data:`SECRET_VALUE_REDACT_CHAR_CAP`
  chars, anchored at the secret's start so a secret straddling a fixed slice
  boundary is still masked in full.

Indirect callers (runs masking, error-code sanitisation, the API-layer
``sensitive_mask`` middleware) import these names directly, so regressions here
would silently leak secrets on every read surface.
"""

from modulo.core.secret_patterns import (
    CONNECTION_STRING_PATTERN,
    PRIVATE_KEY_PATTERN,
    SECRET_VALUE_PATTERNS,
    SECRET_VALUE_REDACT_CHAR_CAP,
    SENSITIVE_VALUE_MASK,
    _close_marker_end,
    mask_secret_values_in_text,
)

MASK = SENSITIVE_VALUE_MASK


class TestFlatPatternFamilies:
    """One test per SECRET_VALUE_PATTERNS family — each must fully mask a
    well-formed secret and leave a benign near-miss untouched."""

    def test_masks_aws_access_key_classic(self) -> None:
        text = "creds AKIAIOSFODNN7EXAMPLE leaked"
        masked = mask_secret_values_in_text(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in masked
        assert MASK in masked

    def test_masks_aws_session_key_asia(self) -> None:
        masked = mask_secret_values_in_text("token ASIAABCDEFGHIJKLMNOP")
        assert "ASIAABCDEFGHIJKLMNOP" not in masked
        assert MASK in masked

    def test_does_not_mask_short_aws_like_string(self) -> None:
        assert mask_secret_values_in_text("AKIA") == "AKIA"
        assert mask_secret_values_in_text("AKIASMALLLEN") == "AKIASMALLLEN"

    def test_masks_google_api_key(self) -> None:
        secret = "AIza" + ("A" * 35)
        masked = mask_secret_values_in_text(f"key {secret}")
        assert secret not in masked
        assert MASK in masked

    def test_masks_github_classic_tokens(self) -> None:
        for prefix in ("ghp_", "gho_", "ghu_", "ghs_", "ghr_"):
            secret = prefix + ("a" * 40)
            masked = mask_secret_values_in_text(f"token {secret}")
            assert secret not in masked
            assert MASK in masked

    def test_masks_github_fine_grained_pat(self) -> None:
        secret = "github_pat_" + ("A" * 60)
        masked = mask_secret_values_in_text(secret)
        assert secret not in masked
        assert MASK in masked

    def test_does_not_mask_short_github_pat(self) -> None:
        # The fine-grained pattern requires 50+ chars after the prefix.
        assert mask_secret_values_in_text("github_pat_11ABC") == "github_pat_11ABC"

    def test_masks_slack_tokens(self) -> None:
        for prefix in ("xoxb", "xoxa", "xoxp", "xoxr", "xoxs"):
            secret = f"{prefix}-1234567890-abcdefghij"
            masked = mask_secret_values_in_text(secret)
            assert secret not in masked
            assert MASK in masked

    def test_does_not_mask_short_slack_token(self) -> None:
        assert mask_secret_values_in_text("sent from xoxb-123") == "sent from xoxb-123"

    def test_masks_stripe_live_keys(self) -> None:
        for prefix in ("sk_live_", "rk_live_"):
            secret = prefix + ("a" * 24)
            masked = mask_secret_values_in_text(secret)
            assert secret not in masked
            assert MASK in masked

    def test_masks_openai_key(self) -> None:
        secret = "sk-" + ("A" * 24)
        masked = mask_secret_values_in_text(f"key {secret}")
        assert secret not in masked
        assert MASK in masked

    def test_does_not_mask_short_openai_key(self) -> None:
        assert mask_secret_values_in_text("sk-1234567890123456789") == "sk-1234567890123456789"

    def test_masks_anthropic_key(self) -> None:
        secret = "sk-ant-" + ("A" * 24)
        masked = mask_secret_values_in_text(secret)
        assert secret not in masked
        assert MASK in masked

    def test_masks_jwt(self) -> None:
        secret = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.signature"
        masked = mask_secret_values_in_text(secret)
        assert secret not in masked
        assert MASK in masked

    def test_masks_bearer_token(self) -> None:
        masked = mask_secret_values_in_text("Authorization: Bearer tok1234567890")
        assert "tok1234567890" not in masked
        assert MASK in masked


class TestPrivateKeys:
    def test_masks_multiline_private_key(self) -> None:
        key = "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAKj34GkxFhD\n-----END RSA PRIVATE KEY-----"
        masked = mask_secret_values_in_text(key)
        assert "BEGIN RSA PRIVATE KEY" not in masked
        assert "END RSA PRIVATE KEY" not in masked
        assert MASK in masked

    def test_masks_openssh_ec_and_pgp_flavours(self) -> None:
        for flavour in ("OPENSSH ", "EC ", "DSA ", "PGP ", ""):
            block = f"-----BEGIN {flavour}PRIVATE KEY-----\nMIIBOgIBAAJBAKj34GkxFhD\n-----END {flavour}PRIVATE KEY-----"
            masked = mask_secret_values_in_text(block)
            assert "PRIVATE KEY-----" not in masked
            assert MASK in masked

    def test_does_not_mask_plain_text_mentioning_key(self) -> None:
        text = "the private key is stored in a vault, not inline"
        assert mask_secret_values_in_text(text) == text


class TestConnectionStrings:
    def test_masks_credentials_in_connection_string(self) -> None:
        text = "postgres://admin:secret@db.host:5432/x"
        masked = mask_secret_values_in_text(text)
        assert "secret" not in masked
        assert masked == f"postgres://admin:{MASK}@db.host:5432/x"

    def test_masks_empty_username_connection_string(self) -> None:
        masked = mask_secret_values_in_text("redis://:secretpass@host:6379")
        assert "secretpass" not in masked
        assert MASK in masked

    def test_masks_password_containing_at_to_final_at(self) -> None:
        # The bounded window must span to the FINAL ``@`` (the host separator),
        # or the password tail after the first ``@`` leaks.
        text = "https://user:pa@ss@host:8080/path"
        masked = mask_secret_values_in_text(text)
        assert "pa@ss" not in masked
        assert masked == f"https://user:{MASK}@host:8080/path"

    def test_does_not_mask_user_without_password(self) -> None:
        assert mask_secret_values_in_text("http://user@host:8080/path") == "http://user@host:8080/path"

    def test_does_not_mask_email_address(self) -> None:
        assert mask_secret_values_in_text("contact admin@example.com now") == "contact admin@example.com now"

    def test_does_not_mask_plain_url(self) -> None:
        url = "ticket #123 https://github.com/org/repo/pull/45"
        assert mask_secret_values_in_text(url) == url


class TestPassthroughSafeGuards:
    def test_non_str_passthrough_unchanged(self) -> None:
        assert mask_secret_values_in_text(None) is None
        assert mask_secret_values_in_text(12345) == 12345
        assert mask_secret_values_in_text(b"AKIAIOSFODNN7EXAMPLE") == b"AKIAIOSFODNN7EXAMPLE"

    def test_empty_string_passthrough(self) -> None:
        assert not mask_secret_values_in_text("")

    def test_clean_text_passthrough(self) -> None:
        text = "run likely hung — no output produced within 60s"
        assert mask_secret_values_in_text(text) == text

    def test_is_idempotent(self) -> None:
        sample = "boom: Bearer tok1234567890, key sk-ABCDEFGHIJKLMNOPQRST"
        once = mask_secret_values_in_text(sample)
        assert mask_secret_values_in_text(once) == once

    def test_does_not_truncate_long_benign_string(self) -> None:
        long_plain = "plain " + "x" * 8000
        masked = mask_secret_values_in_text(long_plain)
        assert masked == long_plain
        assert len(masked) == len(long_plain)

    def test_masks_secret_in_long_string_without_truncating_tail(self) -> None:
        token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.sig"
        long_value = f"token {token} leaked " + ("y" * 8000)
        masked = mask_secret_values_in_text(long_value)
        assert token not in masked
        assert masked.endswith("y" * 8000)
        # Only the secret's slot is replaced by the mask — the rest is intact.
        assert len(masked) == len(long_value) - len(token) + len(MASK)


class TestReDoSBoundedMasking:
    def test_masks_connection_string_straddling_slice_boundary(self) -> None:
        conn = "postgresql://admin:super%40secret%2Fpass@db.host:5432/x"
        straddle = ("w" * 4995) + conn + ("e" * 50)
        masked = mask_secret_values_in_text(straddle)
        assert "super%40secret" not in masked
        assert MASK in masked

    def test_masks_private_key_straddling_slice_boundary(self) -> None:
        key = "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAKj34GkxFhD\n-----END RSA PRIVATE KEY-----"
        straddle = ("z" * 4990) + key + ("q" * 100)
        masked = mask_secret_values_in_text(straddle)
        assert "BEGIN RSA PRIVATE KEY" not in masked
        assert "END RSA PRIVATE KEY" not in masked

    def test_masks_private_key_body_longer_than_cap(self) -> None:
        # A private-key block whose body exceeds the ReDoS cap must still be
        # masked in full — its close delimiter falls outside any 5000-char
        # window, so the direct span masking must kick in.
        block = (
            "-----BEGIN RSA PRIVATE KEY-----\n" + ("MIIBOgIBAAJBAKj34GkxFhD" * 300) + "\n-----END RSA PRIVATE KEY-----"
        )
        assert len(block) > SECRET_VALUE_REDACT_CHAR_CAP
        text = ("a" * 100) + block + ("b" * 100)
        masked = mask_secret_values_in_text(text)
        assert "BEGIN RSA PRIVATE KEY" not in masked
        assert "END RSA PRIVATE KEY" not in masked

    def test_masks_password_with_at_in_over_cap_connection_string(self) -> None:
        conn = "postgres://u:pa@ss@host:5432/db"
        straddle = ("w" * 4995) + conn + ("e" * 100)
        masked = mask_secret_values_in_text(straddle)
        assert "pa@ss" not in masked
        assert "ss@host" not in masked
        assert MASK in masked


class TestWindowHelpers:
    def test_close_marker_end_connection_string_is_single_char(self) -> None:
        # Connection strings close on ``@`` (one character) — no extension.
        assert _close_marker_end("xx@yy", CONNECTION_STRING_PATTERN, 2, "@") == 3

    def test_close_marker_end_private_key_extends_past_full_marker(self) -> None:
        # Private keys close on the full ``-----END ... PRIVATE KEY-----`` tail,
        # not the short ``-----END `` anchor — otherwise the tail leaks.
        tail = "-----END RSA PRIVATE KEY-----"
        text = "z" + tail
        off = text.find("-----END ", 1)
        assert _close_marker_end(text, PRIVATE_KEY_PATTERN, off, "-----END ") == len(text)

    def test_close_marker_end_private_key_without_tail_uses_anchor(self) -> None:
        text = "z-----END "
        off = text.find("-----END ", 1)
        assert _close_marker_end(text, PRIVATE_KEY_PATTERN, off, "-----END ") == off + len("-----END ")

    def test_every_pattern_entry_has_compiled_regex(self) -> None:
        for pattern, replacement in SECRET_VALUE_PATTERNS:
            assert pattern is not None
            assert replacement is not None


class TestMaskValue:
    def test_mask_is_dom_safe(self) -> None:
        # The mask is the DOM-safe bullet character, never the plain text.
        assert MASK == "\u2022\u2022\u2022\u2022\u2022\u2022"

    def test_redact_cap_is_positive(self) -> None:
        assert SECRET_VALUE_REDACT_CHAR_CAP > 0
