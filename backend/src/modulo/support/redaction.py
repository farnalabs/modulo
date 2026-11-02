"""Allowlist-by-construction redaction + the fail-closed post-assembly gate.

Three substitution layers run over every bundle member, in order:

1. **Seeded exact-match** — the deployment's REAL generated credential values
   (bundled postgres/redis passwords, the state HMAC key, and the app's
   environment credentials) are replaced verbatim, including their
   percent-encoded URL forms. The seed map's keys are only ever used as scrub
   patterns; no seeded value is ever written to the bundle.
2. **Structural ``KEY=value``** — lines whose key the canonical sensitive-field
   classifier marks credential-shaped have their value replaced. This reuses
   :func:`modulo.core.secret_patterns.is_sensitive_env_key`, the same
   classifier ``modulo env`` applies, so the two cannot drift.
3. **Secret-shaped values** — the canonical gitleaks-style patterns from
   :mod:`modulo.core.secret_patterns` (GitHub PATs, ``sk-`` keys, bearer
   tokens, private keys, URL userinfo, ...), reused rather than forked.

The gate is separate and deliberately paranoid: after every member is
assembled, :func:`assert_redaction_proven` re-scans the REDACTED text for a
surviving seeded value, a surviving secret-shaped value, or a surviving
sensitive ``KEY=value`` pair. Any hit raises :class:`RedactionSelfCheckError`
naming the member; the caller writes no zip and exits non-zero. A support tool
that silently ships a token is worse than no tool.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Final
from urllib.parse import quote

from modulo.core.secret_patterns import (
    SENSITIVE_VALUE_MASK,
    is_sensitive_env_key,
    mask_secret_values_in_text,
)

#: The single marker used by the seeded and structural layers.
REDACTED: Final[str] = "<redacted>"

#: Fields whose VALUES are always credentials (wholesale redaction), mirroring
#: ``modulo env``. Substring tokens cover the rest.
SENSITIVE_FIELD_NAMES: Final[frozenset[str]] = frozenset(
    {
        "secret_key",
        "fernet_key",
        "fernet_key_old",
        "modulo_oidc_providers",
        "modulo_users",
        "alert_webhook_url",
        "alert_teams_webhook_url",
    }
)
SENSITIVE_FIELD_TOKENS: Final[tuple[str, ...]] = (
    "password",
    "secret",
    "token",
    "api_key",
    "private_key",
    "webhook",
    "users",
    "oidc",
)


@dataclass(frozen=True)
class RedactionCounts:
    """Substitutions per redaction layer (the shape ``report.json`` records)."""

    seed_exact: int = 0
    structural: int = 0
    patterns: int = 0

    @property
    def total(self) -> int:
        return self.seed_exact + self.structural + self.patterns

    def as_dict(self) -> dict[str, int]:
        return {
            "seed_exact": self.seed_exact,
            "structural": self.structural,
            "patterns": self.patterns,
        }

    def applied_layers(self) -> list[str]:
        """Layer names that actually substituted something, in pipeline order."""
        names = ("seed_exact", "structural", "patterns")
        counts = self.as_dict()
        return [name for name in names if counts[name] > 0]


@dataclass(frozen=True)
class RedactionResult:
    """Redacted text plus what each layer did to it."""

    text: str
    counts: RedactionCounts


class RedactionSelfCheckError(RuntimeError):
    """The post-assembly gate refused to emit a bundle.

    The message names the offending MEMBER and the layer that should have
    caught the content — never the secret value itself.
    """

    def __init__(self, member: str, reason: str) -> None:
        self.member = member
        self.reason = reason
        super().__init__(f"redaction self-check failed for {member!r}: {reason}")


def is_sensitive_key(key: str) -> bool:
    """Canonical sensitive-field classification (the ``modulo env`` classifier)."""
    if key.lower() in SENSITIVE_FIELD_NAMES:
        return True
    if is_sensitive_env_key(key.upper()):
        return True
    lowered = key.lower()
    return any(token in lowered for token in SENSITIVE_FIELD_TOKENS)


def redact_kv_lines(text: str) -> tuple[str, int]:
    """Mask the value of every sensitive ``KEY=value`` line; return (text, count)."""
    lines: list[str] = []
    substitutions = 0
    for line in text.splitlines():
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            key = stripped.partition("=")[0].strip()
            if is_sensitive_key(key):
                indent = line[: len(line) - len(line.lstrip())]
                lines.append(f"{indent}{key}={REDACTED}")
                substitutions += 1
                continue
        lines.append(line)
    if substitutions:
        return "\n".join(lines), substitutions
    return text, 0


def redact_text(text: str, secret_values: Mapping[str, str]) -> RedactionResult:
    """Scrub *text* through all three layers, counting each layer's work.

    The seeded map values must be the redaction markers themselves; only the
    map KEYS (the real secret values) are used as patterns.
    """
    output = text
    seed_count = 0
    for secret, replacement in secret_values.items():
        if not secret:
            continue
        occurrences = output.count(secret)
        if occurrences:
            output = output.replace(secret, replacement)
            seed_count += occurrences

    output, structural_count = redact_kv_lines(output)

    masks_before = output.count(SENSITIVE_VALUE_MASK)
    output = mask_secret_values_in_text(output)
    pattern_count = max(0, output.count(SENSITIVE_VALUE_MASK) - masks_before)

    return RedactionResult(
        text=output,
        counts=RedactionCounts(
            seed_exact=seed_count,
            structural=structural_count,
            patterns=pattern_count,
        ),
    )


def _without_markers(text: str) -> str:
    """Drop the redaction markers before scanning for a surviving seed value.

    A short seed value (or a percent-encoded form) can legitimately be a
    substring of ``<redacted>`` / the mask; scanning the marker text itself
    would raise a false positive that blocks every bundle.
    """
    return text.replace(REDACTED, "").replace(SENSITIVE_VALUE_MASK, "")


def _is_secrets_file_member(name: str) -> bool:
    normalised = name.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return normalised == "secrets.json"


def assert_redaction_proven(
    members: Mapping[str, str],
    secret_values: Mapping[str, str],
    *,
    member_allowed: Callable[[str], bool],
) -> None:
    """Fail-closed gate: raise unless every member is provably clean.

    Checks, per member:

    * the member name is on the caller's v1 allowlist (a new member cannot
      slip into the bundle without a golden-list update);
    * no seeded secret value survives (the seed-exact layer ran);
    * no secret-shaped value survives (the pattern layer ran);
    * no sensitive ``KEY=value`` pair survives (the structural layer ran).

    The raw ``secrets.json`` file is never a permitted member.
    """
    for name, text in members.items():
        if not member_allowed(name):
            raise RedactionSelfCheckError(name, "member is not on the support-bundle v1 allowlist")
        if _is_secrets_file_member(name):
            raise RedactionSelfCheckError(name, "the raw secrets file is never a bundle member")

        scrubbed = _without_markers(text)
        for secret in secret_values:
            if secret and secret in scrubbed:
                raise RedactionSelfCheckError(name, "a seeded secret value survived the seed-exact layer")

        if mask_secret_values_in_text(text) != text:
            raise RedactionSelfCheckError(name, "secret-shaped content survived the pattern layer")

        structurally_redacted, _substitutions = redact_kv_lines(text)
        if structurally_redacted != text:
            raise RedactionSelfCheckError(name, "a sensitive KEY=value pair survived the structural layer")


def collect_seed_values(*values: str | None) -> dict[str, str]:
    """Build a seed map from real credential values (raw + percent-encoded)."""
    mapping: dict[str, str] = {}
    for value in values:
        if not value:
            continue
        mapping[value] = REDACTED
        quoted = quote(value, safe="")
        if quoted and quoted != value:
            mapping.setdefault(quoted, REDACTED)
    return mapping


def allowed_member_predicate(allowed: Collection[str], log_prefix: str, log_suffix: str) -> Callable[[str], bool]:
    """Return the v1 membership predicate for a fixed allowlist + log pattern."""
    fixed = frozenset(allowed)

    def _allowed(name: str) -> bool:
        if name in fixed:
            return True
        if name.startswith(log_prefix) and name.endswith(log_suffix):
            component = name[len(log_prefix) : -len(log_suffix)]
            return bool(component) and "/" not in component and "\\" not in component
        return False

    return _allowed
