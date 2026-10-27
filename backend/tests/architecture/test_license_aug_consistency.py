"""The Additional Use Grant must mean the same thing in LICENSE and in the docs.

``LICENSE``'s Additional Use Grant (AUG) is the only provision that permits
production use. Before FAR-2206 it named only "the Licensed Work", which left
the production use of a patched fork ambiguous: the base grant covers derivative
works, the AUG did not mention them, and ``docs/continuity-and-escrow.md`` and
the public security page both promise a customer can fork, patch and run Modulo.
The AUG now names modified versions and derivative works explicitly, and the
carve-out applies to them too.

This module keeps the licence and every artefact that quotes or paraphrases it
from drifting apart again. A doc describing the licence slightly wrong has
happened twice, and a prose rule did not prevent it - so the agreement is
enforced here.

The comparison is of SUBSTANCE, not wording. Each concept below (the permitted
acts, the carve-out, the fact that the carve-out reaches derivatives, and the
fact that it is limited to offering the service to third parties) is expressed by
any of several phrasings, so an artefact may reword freely. What it may not do is
disagree: drop a permitted act the licence grants, or claim a permission the
licence does not give.

Deliberately NOT covered: byte-identical formatting. The AUG is wrapped to the
parameter block's column width in ``LICENSE`` and reflowed in prose, so a literal
text match would be permanently red and would be disabled within a week.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

# tests/architecture/ -> tests/ -> backend/ -> repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_LICENCE = _REPO_ROOT / "LICENSE"

# ---------------------------------------------------------------------------
# Concept lexicon - what the AUG grants and restricts, independent of wording
# ---------------------------------------------------------------------------

_PRODUCTION_USE = r"(?<!non-)\bproduction\s+(?:and\s+commercial\s+)?use\b"
_DERIVATIVE_WORKS = (
    r"\bderivative works?\b",
    r"\bmodified version\b",
    r"\bmodified or derivative\b",
)
_HOSTED_PHRASES = (
    r"\bpaid hosted\b",
    r"\bhosted or managed service\b",
    r"\bas a hosted service\b",
)
# The carve-out is limited to offering such a service TO THIRD PARTIES. Dropping
# that limit overstates the restriction: read literally it would also bar
# offering the service to the customer's own group. Checked inside the carve-out
# window only, so an unrelated mention of "third parties" elsewhere in a
# document cannot satisfy it.
_THIRD_PARTY_RECIPIENT = r"\bthird\s+part(?:y|ies)\b"
# Words that make a hosted-service mention a RESTRICTION rather than a
# permission. Without one, "you may offer a paid hosted service" would read as
# agreement with the carve-out.
_RESTRICTION_MARKERS = (
    r"\bexcept\b",
    r"\bmay not\b",
    r"\bmust not\b",
    r"\brestricts?\b",
    r"\bnot permitted\b",
    r"\bprohibited\b",
    r"\bprovided that\b",
)

# How far around a hosted-service phrase to look for the restriction marker and
# for the derivative and recipient wording. The carve-out is one sentence in
# every artefact, so a couple of hundred characters is enough for any sane
# rewording.
_WINDOW = 250


def _matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def _windows_around(text: str, phrases: tuple[str, ...]) -> Iterator[str]:
    for phrase in phrases:
        for match in re.finditer(phrase, text, re.IGNORECASE):
            start = max(0, match.start() - _WINDOW)
            yield text[start : match.end() + _WINDOW]


def _expresses_hosted_carve_out(text: str) -> bool:
    return any(_matches_any(window, _RESTRICTION_MARKERS) for window in _windows_around(text, _HOSTED_PHRASES))


def _carve_out_reaches_derivatives(text: str) -> bool:
    return any(
        _matches_any(window, _RESTRICTION_MARKERS) and _matches_any(window, _DERIVATIVE_WORKS)
        for window in _windows_around(text, _HOSTED_PHRASES)
    )


def _carve_out_names_third_parties(text: str) -> bool:
    return any(
        re.search(_THIRD_PARTY_RECIPIENT, window, re.IGNORECASE) for window in _windows_around(text, _HOSTED_PHRASES)
    )


_CONCEPT_CHECKS: dict[str, Callable[[str], bool]] = {
    "production_use": lambda text: _matches_any(text, (_PRODUCTION_USE,)),
    "derivative_works": lambda text: _matches_any(text, _DERIVATIVE_WORKS),
    "hosted_service_carve_out": _expresses_hosted_carve_out,
    "derivative_in_carve_out": _carve_out_reaches_derivatives,
    "third_party_recipient": _carve_out_names_third_parties,
}

_ALL_CONCEPTS = frozenset(_CONCEPT_CHECKS)


def _expressed_concepts(text: str) -> set[str]:
    return {name for name, check in _CONCEPT_CHECKS.items() if check(text)}


# ---------------------------------------------------------------------------
# Artefacts that quote or paraphrase the AUG, and the substance each must carry
# ---------------------------------------------------------------------------

_AUG_ARTEFACTS: dict[str, frozenset[str]] = {
    "README.md": _ALL_CONCEPTS,
    "backend/LICENSE": _ALL_CONCEPTS,
    "docs/continuity-and-escrow.md": _ALL_CONCEPTS,
    # The answer bank states the RESTRICTION only, so it is held to the
    # carve-out concepts rather than the full grant.
    "docs/security/security-questionnaire-answer-bank.md": frozenset(
        {"hosted_service_carve_out", "derivative_in_carve_out", "third_party_recipient"}
    ),
}

# A doc "mentions the AUG" if it names the parameter or uses the distinctive
# hosted/managed-service restriction phrasing. Both signals are precise: a plain
# "non-production use" or a "self-hosted service" does not match.
_AUG_MENTION = re.compile(
    r"additional use grant"
    r"|\bpaid hosted\b"
    r"|\bhosted or managed service\b"
    r"|\bas a hosted service\b",
    re.IGNORECASE,
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _licence_additional_use_grant() -> str:
    """The AUG parameter block, with its line wrapping normalised away."""
    lines = _read(_LICENCE).splitlines()
    for index, line in enumerate(lines):
        if line.startswith("Additional Use Grant:"):
            break
    else:
        return ""

    block = [line.split(":", 1)[1]]
    for continuation in lines[index + 1 :]:
        if not continuation.strip():
            break
        block.append(continuation)
    return " ".join(" ".join(block).split())


def _scanned_doc_paths() -> list[Path]:
    paths = {_REPO_ROOT / "LICENSE"}
    paths.update(_REPO_ROOT.glob("*.md"))
    paths.update((_REPO_ROOT / "docs").rglob("*.md"))
    return sorted(path for path in paths if path.is_file())


# ---------------------------------------------------------------------------
# The licence itself
# ---------------------------------------------------------------------------


def test_licence_has_an_additional_use_grant_parameter_block() -> None:
    assert _licence_additional_use_grant()


def test_licence_grant_names_derivative_production_use_and_its_carve_out() -> None:
    """A narrowing of the licence itself is a failure, not just a doc drift."""
    aug = _licence_additional_use_grant()
    missing = sorted(_ALL_CONCEPTS - _expressed_concepts(aug))
    assert not missing, f"LICENSE's Additional Use Grant no longer expresses {missing}: {aug!r}"


# ---------------------------------------------------------------------------
# The artefacts that quote it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("relative_path", sorted(_AUG_ARTEFACTS))
def test_registered_artefact_exists(relative_path: str) -> None:
    assert (_REPO_ROOT / relative_path).is_file()


@pytest.mark.parametrize("relative_path", sorted(_AUG_ARTEFACTS))
def test_artefact_expresses_the_substance_the_licence_grants(relative_path: str) -> None:
    """Every concept the artefact is responsible for must be present."""
    required = _AUG_ARTEFACTS[relative_path]
    expressed = _expressed_concepts(_read(_REPO_ROOT / relative_path))
    missing = sorted(required - expressed)
    assert not missing, (
        f"{relative_path} does not express {missing} about the Additional Use Grant; "
        "either update it to match LICENSE or drop the concept from the registry in "
        f"{Path(__file__).name}"
    )


@pytest.mark.parametrize("relative_path", sorted(_AUG_ARTEFACTS))
def test_artefact_does_not_claim_more_than_the_licence_grants(relative_path: str) -> None:
    """An artefact may not promise a permission the licence does not give."""
    granted = _expressed_concepts(_licence_additional_use_grant())
    expressed = _expressed_concepts(_read(_REPO_ROOT / relative_path))
    overclaimed = sorted(expressed - granted)
    assert not overclaimed, (
        f"{relative_path} expresses {overclaimed}, which LICENSE's Additional Use Grant does not grant"
    )


def test_every_doc_quoting_the_additional_use_grant_is_registered() -> None:
    """Structural anti-drift: a new doc that quotes the AUG must be checked."""
    registered = {_REPO_ROOT / path for path in _AUG_ARTEFACTS} | {_LICENCE}
    unregistered = [
        str(path.relative_to(_REPO_ROOT))
        for path in _scanned_doc_paths()
        if path not in registered and _AUG_MENTION.search(_read(path))
    ]
    assert not unregistered, (
        f"these docs quote the Additional Use Grant but are not in _AUG_ARTEFACTS: "
        f"{unregistered} - register each one so it is checked against LICENSE"
    )
