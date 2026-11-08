"""Five-layer redaction for the support bundle (FAR-2305).

Layers, in the order they are applied to every member's raw text:

1. **Member allowlist** — enforced at assembly time (:mod:`modulo.support.bundle`).
   No content layer; it decides what may exist at all.
2. **Exact-value scrub** — every real generated secret value (the bundled
   postgres/redis passwords, the state HMAC key, plus every credential-shaped
   Settings value) is replaced verbatim, including its percent-encoded form.
3. **Structural ``KEY=value`` scrub** — through the CANONICAL sensitive-field
   classifier (:func:`modulo.core.secret_patterns.is_sensitive_field_name`),
   the same classifier ``modulo env`` and ``modulo doctor --report`` use.
4. **Shared value patterns** — :func:`modulo.core.secret_patterns.mask_secret_values_in_text`
   (GitHub PATs, API keys, bearer tokens, URL userinfo, private keys, JWTs).
5. **Fail-closed post-assembly gate** — re-scans each already-redacted member.
   Any residual seeded value, pattern hit, unredacted sensitive ``KEY=value``,
   or unclassified high-entropy token withholds the offending item (or, for a
   small invariant member, the whole member) and aborts the build only when
   the trip cannot be attributed to an item.

The gate deliberately includes an entropy heuristic no earlier layer
implements: a secret shape none of the pattern layers know about is WITHHELD
rather than shipped. That is the "fail closed" guarantee — an unknown shape
never reaches the archive silently.

The entropy heuristic exempts one benign shape (FAR-2333): a content-hashed
build artefact filename (``index-aB3dE5fG.js``). Its hash is mixed case plus
digits, i.e. byte-for-byte the entropy profile of a generated secret, and a
hashed Vite asset path in a browser error message made every real bundle
unbuildable. The exemption is deliberately NARROW — only the filename shape
is masked, never a whole URL — so a credential that lives in a URL path (a
Slack webhook token) is still refused/withheld.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlsplit

from modulo.core.secret_patterns import (
    SENSITIVE_VALUE_MASK,
    is_sensitive_field_name,
    mask_secret_values_in_text,
)

REDACTED = "<redacted>"

#: Values shorter than this are never used as exact-match patterns: a 1-3
#: character "secret" would scrub common words out of every member.
_MIN_SEED_VALUE_LENGTH = 4

# Substitution counts are reported per layer (the bundle's ``report.json``).
LAYER_EXACT = "layer2_exact"
LAYER_STRUCTURAL = "layer3_structural"
LAYER_PATTERNS = "layer4_patterns"


class RedactionGateError(RuntimeError):
    """Layer 5 refused the bundle: a member still looks like it holds a secret.

    Carries the offending member name and the concrete reasons, so the CLI can
    name the member, write NO zip and exit non-zero.
    """

    def __init__(self, member: str, reasons: list[str]) -> None:
        self.member = member
        self.reasons = tuple(reasons)
        super().__init__(f"redaction gate refused member {member!r}: " + "; ".join(reasons))


@dataclass
class RedactionResult:
    """One member's redacted text plus the per-layer substitution counts."""

    text: str
    counts: dict[str, int] = field(default_factory=dict)


def seed_values_from_settings(settings: object) -> dict[str, str]:
    """Collect credential-shaped Settings VALUES as exact-match patterns.

    Every field whose canonical name is sensitive and whose value is a
    non-empty string is seeded, plus the password component of any URL-shaped
    value (``database_url`` / ``redis_url`` carry credentials inline). The map
    keys are the raw secret values; they are only ever used as scrub patterns
    and never enter the archive.
    """
    seeds: dict[str, str] = {}
    fields = getattr(type(settings), "model_fields", {})
    for name in fields:
        if not is_sensitive_field_name(name):
            continue
        value: object = None
        try:
            value = getattr(settings, name)
        except Exception:  # pragma: no cover - defensive: a broken property
            value = None
        if isinstance(value, str) and value:
            _add_seed(seeds, value)
            password = _url_password(value)
            if password:
                _add_seed(seeds, password)
    return seeds


def seed_values_from_data_dir(data_dir: Path | None) -> dict[str, str]:
    """Exact-match patterns from the launcher's 0600 ``secrets.json``.

    Native installs only; an unreadable/missing file yields an empty map (the
    structural + pattern layers still apply).
    """
    if data_dir is None:
        return {}
    secrets_path = data_dir / "secrets.json"
    if not secrets_path.is_file():
        return {}
    try:
        from modulo.launcher.secrets_file import SecretsFileError, _parse
    except ImportError:  # pragma: no cover - launcher package always ships
        return {}
    try:
        parsed = _parse(secrets_path.read_bytes())
    except (OSError, ValueError, SecretsFileError):
        return {}
    seeds: dict[str, str] = {}
    for value in (parsed.postgres_password, parsed.redis_password, parsed.state_hmac_key_hex):
        if value:
            _add_seed(seeds, value)
    return seeds


def collect_secret_values(
    *,
    data_dir: Path | None,
    settings: object | None,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The complete exact-match seed map for one bundle build."""
    seeds: dict[str, str] = {}
    if settings is not None:
        seeds.update(seed_values_from_settings(settings))
    seeds.update(seed_values_from_data_dir(data_dir))
    if extra:
        for value in extra.values():
            _add_seed(seeds, value)
    return seeds


def _add_seed(seeds: dict[str, str], value: str) -> None:
    """Add *value* and its percent-encoded form; short values are skipped."""
    if len(value) < _MIN_SEED_VALUE_LENGTH:
        return
    seeds[value] = REDACTED
    # Both encoding styles, because a credential embedded in a URL may be
    # quoted with or without safe characters (e.g. a password containing ``/``).
    for encoded in (quote(value, safe=""), quote(value)):
        if encoded and encoded != value:
            seeds[encoded] = REDACTED


def _url_password(value: str) -> str | None:
    """Return the inline password of a URL-shaped value, if any."""
    if "://" not in value:
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.password:
        return parts.password
    # A DSN for asyncpg/SQLAlchemy may use a driver suffix; urlsplit still
    # parses the userinfo after the scheme separator.
    return None


class Redactor:
    """Applies layers 2-4 to a member and enforces the layer-5 gate."""

    def __init__(self, secret_values: Mapping[str, str]) -> None:
        # Longest first so a substring of a longer secret can never be
        # replaced first and leave the longer value's tail behind.
        self._seeds: list[str] = sorted((v for v in secret_values if v), key=len, reverse=True)

    def redact(self, text: str) -> RedactionResult:
        """Apply layers 2, 3 and 4 in order and return counts per layer."""
        counts: dict[str, int] = {}

        text, exact = _replace_seeds(text, self._seeds)
        counts[LAYER_EXACT] = exact

        text, structural = _redact_structured_lines(text)
        counts[LAYER_STRUCTURAL] = structural

        before = text.count(SENSITIVE_VALUE_MASK)
        text = mask_secret_values_in_text(text)
        counts[LAYER_PATTERNS] = max(0, text.count(SENSITIVE_VALUE_MASK) - before)

        return RedactionResult(text=text, counts=counts)

    def gate(
        self,
        member: str,
        text: str,
        *,
        allowed_tokens: frozenset[str] = frozenset(),
    ) -> None:
        """Layer 5: refuse the build when *member* still looks secret-bearing.

        *allowed_tokens* are tokens this build itself GENERATED (the bundle's
        own per-run reference). They are exempt from the entropy heuristic
        only — every seeded value, pattern and structural ``KEY=value`` check
        still applies to the text that carries them.
        """
        reasons = self.gate_reasons(text, allowed_tokens=allowed_tokens)
        if reasons:
            raise RedactionGateError(member, reasons)

    def gate_reasons(
        self,
        text: str,
        *,
        allowed_tokens: frozenset[str] = frozenset(),
    ) -> list[str]:
        """The layer-5 findings for *text* (empty when it is clean).

        Non-raising form of :meth:`gate`, so a caller can decide whether the
        trip is attributable to one item and withhold just that item
        (FAR-2333) instead of failing the whole build.
        """
        return _gate_reasons(text, self._seeds, allowed_tokens)


def _replace_seeds(text: str, seeds: list[str]) -> tuple[str, int]:
    count = 0
    output = text
    for secret in seeds:
        occurrences = output.count(secret)
        if occurrences:
            output = output.replace(secret, REDACTED)
            count += occurrences
    return output, count


_EXPORT_PREFIX_PATTERN = (
    r"^(?P<indent>\s*)(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_.\-]*)"
    r"\s*=\s*(?P<value>[^\n]*)$"
)
_EXPORT_PREFIX = re.compile(_EXPORT_PREFIX_PATTERN)
_REDACTION_MARKERS = frozenset({REDACTED, SENSITIVE_VALUE_MASK, "", '""', "''"})


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _redact_structured_lines(text: str) -> tuple[str, int]:
    """Layer 3: mask ``KEY=value`` lines whose KEY is credential-shaped."""
    count = 0
    lines: list[str] = []
    for line in text.splitlines():
        match = _EXPORT_PREFIX.match(line)
        if match is not None and is_sensitive_field_name(match.group("key")):
            value = _unquote(match.group("value").strip())
            if value not in _REDACTION_MARKERS:
                lines.append(f"{match.group('indent')}{match.group('key')}={REDACTED}")
                count += 1
                continue
        lines.append(line)
    return "\n".join(lines), count


# ---------------------------------------------------------------------------
# Layer 5 — fail-closed residual scan
# ---------------------------------------------------------------------------

# Candidate "secret-shaped" token: URL-safe base64 / hex run of >= 24 chars.
_ENTROPY_TOKEN = re.compile(r"[A-Za-z0-9+/_\-]{24,}")
_HEX_ONLY = re.compile(r"[0-9a-fA-F]+")
_MIN_ENTROPY_BITS = 3.5

#: File extensions of content-hashed build artefacts (Vite/webpack output).
_ASSET_EXTENSIONS: tuple[str, ...] = (
    "js",
    "mjs",
    "cjs",
    "css",
    "map",
    "json",
    "wasm",
    "woff",
    "woff2",
    "ttf",
    "otf",
    "eot",
    "png",
    "svg",
    "jpg",
    "jpeg",
    "gif",
    "webp",
    "ico",
    "avif",
)

#: The filename shape Vite/webpack give a content-hashed build artefact:
#: ``index-aB3dE5fG.js`` / ``index-aB3dE5fG.chunk.css``. The hash is mixed case
#: plus digits, so it has exactly the entropy profile of a generated secret.
#: Deliberately anchored to a PATH SEGMENT: only the filename is exempt, never
#: a whole URL, because a credential in a URL path must still be caught.
_HASHED_ASSET_NAME = re.compile(
    r"(?<![A-Za-z0-9_\-])"
    r"[A-Za-z0-9_][A-Za-z0-9_.\-]{0,160}-[A-Za-z0-9_\-]{8,}"
    r"(?:\.[A-Za-z0-9]{1,12})?"
    r"\.(?:" + "|".join(_ASSET_EXTENSIONS) + r")"
    r"(?![A-Za-z0-9_\-])"
)


def _gate_reasons(
    text: str,
    seeds: list[str],
    allowed_tokens: frozenset[str] = frozenset(),
) -> list[str]:
    reasons: list[str] = []
    _check_seeded_secrets(text, seeds, reasons)
    _check_shared_patterns(text, reasons)
    _check_structured_lines(text, reasons)
    _check_entropy_tokens(text, allowed_tokens, reasons)
    return reasons


def _check_seeded_secrets(text: str, seeds: list[str], reasons: list[str]) -> None:
    for secret in seeds:
        if secret and secret in text:
            reasons.append("seeded secret value still present")
            break


def _check_shared_patterns(text: str, reasons: list[str]) -> None:
    mask_probe = mask_secret_values_in_text(text)
    if mask_probe != text:
        reasons.append("shared secret-value pattern still matches")


def _check_structured_lines(text: str, reasons: list[str]) -> None:
    for line in text.splitlines():
        match = _EXPORT_PREFIX.match(line)
        if match is None or not is_sensitive_field_name(match.group("key")):
            continue
        value = _unquote(match.group("value").strip())
        if value not in _REDACTION_MARKERS:
            reasons.append(f"sensitive key {match.group('key')!r} has an unredacted value")
            break


def _check_entropy_tokens(text: str, allowed_tokens: frozenset[str], reasons: list[str]) -> None:
    for token in _ENTROPY_TOKEN.findall(_mask_hashed_asset_names(text)):
        if token in allowed_tokens:
            continue
        if _looks_like_unclassified_secret(token):
            reasons.append("unclassified high-entropy token present")
            break


def _mask_hashed_asset_names(text: str) -> str:
    """Blank out hashed build-artefact filenames before entropy tokenising.

    FAR-2333: a browser error message carrying
    ``https://staging.modulo.run/assets/index-<hash>.js`` tripped the entropy
    heuristic on ``run/assets/index-<hash>`` (``.`` is not in the token class,
    so the match runs from one dot to the next) and made every real support
    bundle unbuildable. Replacing just the hashed FILENAME with a space leaves
    the rest of the URL intact for the scan: a path with no uppercase/digit run
    is not secret-shaped, while a credential embedded in a URL path (a Slack
    webhook segment, ``hooks.slack.com/services/T…/B…/<token>``) has no
    ``<stem>-<hash>.<asset ext>`` filename and is still scanned.
    """
    return _HASHED_ASSET_NAME.sub(" ", text)


def _looks_like_unclassified_secret(token: str) -> bool:
    """True for a token that looks like a credential no pattern layer knows.

    Deliberately conservative on the two common benign shapes so a legitimate
    bundle does not abort: hex-only tokens (git SHAs, ``sha256`` digests, the
    HMAC key's hex form — the last is seeded anyway) and single-class tokens
    (paths, dotted identifiers, lowercase hostnames). It requires mixed case
    AND a digit, i.e. the shape a base64/URL-safe generated secret has.
    """
    if _HEX_ONLY.fullmatch(token):
        return False
    if not any(char.isupper() for char in token):
        return False
    if not any(char.islower() for char in token):
        return False
    if not any(char.isdigit() for char in token):
        return False
    return _shannon_entropy(token) >= _MIN_ENTROPY_BITS


def _shannon_entropy(value: str) -> float:
    counts = Counter(value)
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())
