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

Seed-exact matching has two tiers, because a deployment credential can be a
common English word. A self-hoster following the documented quickstart runs
Postgres with ``POSTGRES_PASSWORD=modulo`` — and every bundle carries the word
``modulo`` in its own boilerplate, so a bare substring match would either
mangle every member or fail the gate on a value that was never a leak.
*distinctive* values (long, or containing any non-letter) are scrubbed and
detected as bare substrings; *weak* values (short and alphabetic) are scrubbed
and detected only inside a structural credential context — a URL userinfo
position, or a value introduced by a credential-bearing key such as
``password``/``requirepass``/``token``. The trade-off is explicit: a weak
credential mentioned in free text with no credential context cannot be
distinguished from the same English word in boilerplate, so it is not treated
as a leak.

The credential context is parsed structurally (:func:`_credential_value_spans`),
not matched by a fixed separator: between the key and the value any run of
whitespace, quotes, brackets/braces/parens and the ``:``/``=`` assignment
punctuation is tolerated, so ``requirepass "x"``, ``password: 'x'``,
``{"password": "x"}``, ``{'password': 'x'}`` and ``{"POSTGRES_PASSWORD":"x"}``
are all credential contexts. A key must be a whole token, so the ``auth``
inside ``authentication`` never counts.

The value is not necessarily the FIRST token after the key. ``redis-cli
AUTH <username> <password>`` — the documented Redis 6 ACL form — puts the
password second, and a shell trace can carry further command tokens before it.
So up to :data:`MAX_CREDENTIAL_VALUE_TOKENS` tokens after a keyword are
candidate value positions and every one of them is scrubbed when it holds a
weak seed. The bound is small and explicit: it covers the documented command
shapes without turning free prose into a credential command.

That structural detector is shared with the gate, which means a bug in it would
be invisible to the gate (one detector = a detector miss is a gate miss too).
The gate therefore adds a SECOND, INDEPENDENT check
(:func:`_weak_seed_near_credential_keyword`): any occurrence of the exact weak
seed value as one of the (bounded) tokens after a credential keyword is a leak,
regardless of how many intervening tokens such as a username sit in between,
whatever the separator looks like and whether or not the structural parse
understood it. The gate fails closed on either check.
"""

from __future__ import annotations

import re
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

#: A seed value at least this long, or containing any non-letter character, is
#: distinctive enough to scrub as a bare substring: it cannot plausibly be
#: boilerplate, and the documented quickstart password (``modulo``) is not.
MIN_DISTINCTIVE_SEED_LENGTH: Final[int] = 12

#: Keys that introduce a credential VALUE. A weak seed value is only scrubbed
#: (and only detected) where one of these introduces a value, or sits within
#: :data:`MAX_CREDENTIAL_VALUE_TOKENS` tokens of one.
_CREDENTIAL_CONTEXT_KEYS: Final[str] = (
    r"(?:password|passwd|pwd|passphrase|secret|token|api[_-]?key|private[_-]?key|"
    r"access[_-]?key|hmac|requirepass|fernet[_-]?key|auth)"
)

#: A credential key as a WHOLE token: ``POSTGRES_PASSWORD`` matches, the ``auth``
#: inside ``authentication`` does not, and prose stays prose.
_CREDENTIAL_KEYWORD_RE: Final[re.Pattern[str]] = re.compile(
    rf"(?i)(?<![A-Za-z0-9]){_CREDENTIAL_CONTEXT_KEYS}(?![A-Za-z0-9])"
)

#: What may sit between a credential key and its value without changing what
#: the value IS: whitespace, quotes, brackets/braces/parens, and the ``:``/``=``
#: assignment punctuation used by env files, YAML/TOML, JSON and shell. A
#: quoted JSON *key* puts its closing quote here (``{"password": "x"}``) —
#: the separator shape a ``[:=]``-only pattern could not span.
_KEY_VALUE_SEPARATOR: Final[str] = r"""[\s"'`(\[{:,=]*"""

#: The value token a credential key introduces: a quoted string, a bracketed
#: run, or a bare run. One group, so the span is the whole token (its own
#: quoting included) and no shape can leave every alternative unmatched.
_VALUE_TOKEN: Final[str] = (
    r'(?P<value>"[^"\n]*"'
    r"|'[^'\n]*'"
    r"|\([^)\n]*\)"
    r"|\[[^\]\n]*\]"
    r'|[^\s"\'`()\[\]{},;=]+)'
)

#: ``KEY`` + tolerated separator + value token, as one structural match.
_SEPARATOR_RE: Final[re.Pattern[str]] = re.compile(_KEY_VALUE_SEPARATOR)
_VALUE_TOKEN_RE: Final[re.Pattern[str]] = re.compile(_VALUE_TOKEN)

#: How many tokens after a credential keyword can be the credential VALUE.
#: ``redis-cli AUTH <username> <password>`` (Redis 6 ACL form) puts the
#: password SECOND and a shell trace can carry a command token before the
#: username, so the FIRST-token-only parse leaked every one of those shapes.
#: The bound is deliberately small: three tokens covers the documented command
#: forms while staying far short of free prose, where the word can sit many
#: tokens away (``password authentication failed for user modulo`` is five).
MAX_CREDENTIAL_VALUE_TOKENS: Final[int] = 3


def is_distinctive_seed(value: str) -> bool:
    """True when *value* can be matched as a bare substring without false positives."""
    return len(value) >= MIN_DISTINCTIVE_SEED_LENGTH or not value.isalpha()


def _is_word_char(char: str) -> bool:
    return char.isalnum() or char == "_"


def _bounded_occurrences(text: str, value: str, start: int, end: int) -> list[tuple[int, int]]:
    """Spans where *value* appears in ``text[start:end]`` as a whole token."""
    spans: list[tuple[int, int]] = []
    position = text.find(value, start, end)
    while position != -1:
        after = position + len(value)
        preceded_by_word = position > 0 and _is_word_char(text[position - 1])
        followed_by_word = after < len(text) and _is_word_char(text[after])
        if not preceded_by_word and not followed_by_word:
            spans.append((position, after))
        position = text.find(value, position + 1, end)
    return spans


def _value_token_spans_after(text: str, start: int) -> list[tuple[int, int]]:
    """Bounded spans of the value tokens a credential keyword introduces.

    The credential VALUE is not always the first token after its keyword: the
    documented Redis 6 ACL form is ``AUTH <username> <password>``, so the
    password is the second token, and a shell trace can carry a command token
    before the username. Up to :data:`MAX_CREDENTIAL_VALUE_TOKENS` tokens are
    therefore returned as candidate value positions.

    Only the first token may follow a newline (a YAML value on the next line);
    the rest of the window must stay on that token's line, so a multi-token
    window can never drift into unrelated prose on a later line.
    """
    spans: list[tuple[int, int]] = []
    position = start
    while len(spans) < MAX_CREDENTIAL_VALUE_TOKENS:
        # The separator pattern is zero-or-more, so it always matches; the
        # ``None`` arm exists only to satisfy the type checker.
        separator = _SEPARATOR_RE.match(text, position)
        separator_end = separator.end() if separator is not None else position
        if spans and "\n" in text[position:separator_end]:
            break
        token = _VALUE_TOKEN_RE.match(text, separator_end)
        if token is None:
            break
        spans.append(token.span())
        position = token.end()
    return spans


def _credential_value_spans(text: str) -> list[tuple[int, int]]:
    """Spans of *text* that occupy a candidate credential VALUE position.

    The structural parse shared by the redaction layer and the gate: find a
    credential key, then take up to :data:`MAX_CREDENTIAL_VALUE_TOKENS` value
    tokens that follow it. A span covers only a value token (its own quoting
    included), never the key, the separator, or the intervening username.
    """
    spans: list[tuple[int, int]] = []
    for keyword in _CREDENTIAL_KEYWORD_RE.finditer(text):
        spans.extend(_value_token_spans_after(text, keyword.end()))
    return spans


def _weak_seed_occurrences(text: str, value: str) -> list[tuple[int, int]]:
    """Spans where a WEAK *value* is a leak: a credential value or URL userinfo."""
    escaped = re.escape(value)
    occurrences = [(match.start(0) + 1, match.end(0) - 1) for match in re.finditer(rf":{escaped}@", text)]
    for start, end in _credential_value_spans(text):
        occurrences.extend(_bounded_occurrences(text, value, start, end))
    return sorted(occurrences)


def _seed_leak_occurrences(text: str, value: str) -> list[tuple[int, int]]:
    """Spans where *value* counts as a leak: bare when distinctive, contextual when weak."""
    if is_distinctive_seed(value):
        return [(match.start(), match.end()) for match in re.finditer(re.escape(value), text)]
    return _weak_seed_occurrences(text, value)


def _weak_seed_near_credential_keyword(text: str, value: str) -> bool:
    """Independent gate backstop: a weak *value* in a credential token window.

    Deliberately does NOT use :func:`_credential_value_spans` (the structural
    parse shared with the scrub layer). It re-finds the credential keyword and
    checks the exact seed value against each of the bounded value-token
    positions that follow it. So a detector miss in the layer cannot blind the
    gate: an unfamiliar separator, or any number of intervening tokens such as
    a username, cannot hide a value that sits within
    :data:`MAX_CREDENTIAL_VALUE_TOKENS` tokens of a credential keyword.
    """
    for keyword in _CREDENTIAL_KEYWORD_RE.finditer(text):
        for start, end in _value_token_spans_after(text, keyword.end()):
            if _bounded_occurrences(text, value, start, end):
                return True
    return False


def _scrub_seed(text: str, value: str, replacement: str) -> tuple[str, int]:
    """Replace every leak occurrence of *value* in *text*; return (text, count).

    The SAME structural detector (:func:`_seed_leak_occurrences`) decides what a
    leak is here and in :func:`assert_redaction_proven`, so a value the layer
    scrubbed is never reported as surviving and a value the layer left alone is
    never refused by the shared check. The gate adds one INDEPENDENT check on
    top, so a miss in this detector is not a miss in the gate.
    """
    occurrences = _seed_leak_occurrences(text, value)
    if not occurrences:
        return text, 0

    pieces: list[str] = []
    cursor = 0
    substituted = 0
    for start, end in occurrences:
        if start < cursor:  # pragma: no cover - defensive: overlapping spans
            continue
        pieces.append(text[cursor:start])
        pieces.append(replacement)
        cursor = end
        substituted += 1
    pieces.append(text[cursor:])
    return "".join(pieces), substituted


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
        output, occurrences = _scrub_seed(output, secret, replacement)
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
    * no seeded secret value survives (the seed-exact layer ran) — matched by
      the same structural detector the layer uses, so a short common-word
      credential is only a leak in a credential context, never in boilerplate;
    * no weak seeded value survives NEAR a credential keyword — a second,
      independent check that does not depend on the structural parse, so a
      detector miss in the layer (and in the check above) cannot blind the gate;
    * no secret-shaped value survives (the pattern layer ran);
    * no sensitive ``KEY=value`` pair survives (the structural layer ran).

    The raw ``secrets.json`` file is never a permitted member.

    Callers pass only *collected* content here. Bundle-generated inventory
    (``MANIFEST.txt`` / ``report.json``) is assembled by the harness from fixed
    constants, so it is gated with an empty seed map — the pattern and
    structural layers still apply to it.
    """
    for name, text in members.items():
        if not member_allowed(name):
            raise RedactionSelfCheckError(name, "member is not on the support-bundle v1 allowlist")
        if _is_secrets_file_member(name):
            raise RedactionSelfCheckError(name, "the raw secrets file is never a bundle member")

        scrubbed = _without_markers(text)
        for secret in secret_values:
            if not secret:
                continue
            if _seed_leak_occurrences(scrubbed, secret):
                layer = "seed-exact layer" if is_distinctive_seed(secret) else "seed-exact layer (credential context)"
                raise RedactionSelfCheckError(name, f"a seeded secret value survived the {layer}")
            # SECOND, INDEPENDENT check. The detector above is shared with the
            # redaction layer, so a bug in it would be invisible here — one
            # detector means a detector miss is a gate miss. This check depends
            # on neither the separator shape nor the structural tokeniser: any
            # occurrence of the exact weak seed value in the bounded token
            # window after a credential keyword (MAX_CREDENTIAL_VALUE_TOKENS)
            # is a leak, however many intervening tokens such as a username sit
            # between the keyword and the value.
            if not is_distinctive_seed(secret) and _weak_seed_near_credential_keyword(scrubbed, secret):
                raise RedactionSelfCheckError(
                    name,
                    "a seeded secret value survived near a credential keyword (independent proximity check)",
                )

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
