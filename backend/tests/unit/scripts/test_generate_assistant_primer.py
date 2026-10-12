"""Unit tests for the primer token-budget trim ladder in generate_assistant_primer.py.

Covers `_truncate_primer` (section ladder + fallback) and the extracted
`_trim_section` helper (Key Concepts definitions -> bare terms).
"""

from __future__ import annotations

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

for parent in Path(__file__).resolve().parents:
    script_path = parent / "scripts" / "generate_assistant_primer.py"
    if script_path.exists():
        break
else:
    raise RuntimeError("Could not find repo root (scripts/generate_assistant_primer.py)")

_ap_loader = SourceFileLoader("generate_assistant_primer", str(script_path))
ap = module_from_spec(spec_from_loader("generate_assistant_primer", _ap_loader))
_ap_loader.exec_module(ap)


def _overview(words: int = 40) -> str:
    return " ".join(f"overviewword{i}" for i in range(words))


def _key_concepts(count: int = 4, def_words: int = 8) -> str:
    lines = [f"- **Term{i}** — " + " ".join(f"defword{i}x{j}" for j in range(def_words)) for i in range(count)]
    return "Key Concepts\n\n" + "\n".join(lines)


def _navigation(count: int = 6) -> str:
    lines = [f"- **Group{i}** — Page One, Page Two, Page Three" for i in range(count)]
    return "Pages & Navigation\n\n" + "\n".join(lines)


def _active_context(count: int = 7) -> str:
    lines = [f"- **Field{i}** — value {i} with some words here" for i in range(count)]
    return "Active Context\n\n" + "\n".join(lines)


def _primer(*sections: str) -> str:
    return "\n\n## ".join(sections)


# ---------------------------------------------------------------------------
# _truncate_primer — ladder
# ---------------------------------------------------------------------------


def test_returns_input_unchanged_when_within_budget():
    primer = _primer(_overview(10), _key_concepts(2))
    assert ap._truncate_primer(primer, 100000) == primer


def test_single_section_over_budget_returns_input_unchanged():
    primer = _overview(200)
    assert ap._estimate_tokens(primer) > 5
    assert ap._truncate_primer(primer, 5) == primer


def test_navigation_dropped_when_truncating():
    primer = _primer(_overview(40), _key_concepts(4), _navigation(30), _active_context(7))
    assert ap._estimate_tokens(primer) > 400
    result = ap._truncate_primer(primer, 400)
    assert "Pages & Navigation" not in result
    assert "Key Concepts" in result
    assert "Active Context" in result


def test_overview_always_survives():
    primer = _primer(_overview(40), _key_concepts(11), _navigation(30), _active_context(7))
    result = ap._truncate_primer(primer, 1)
    assert result == _overview(40)


def test_key_concepts_definitions_reduced_to_terms_when_tight():
    full = _primer(_overview(40), _key_concepts(4), _active_context(7))
    candidate = _primer(_overview(40), _key_concepts(4))
    reduced = ap._trim_section(candidate, 0)
    # Budget strictly between the reduced and the unreduced candidate forces
    # the definition-trimming rung of the ladder.
    assert ap._estimate_tokens(reduced) < ap._estimate_tokens(candidate)
    budget = (ap._estimate_tokens(candidate) + ap._estimate_tokens(reduced)) // 2
    assert ap._estimate_tokens(full) > budget

    result = ap._truncate_primer(full, budget)
    assert "## Key Concepts" in result
    assert "- **Term0**" not in result
    assert "- Term0" in result
    assert ap._estimate_tokens(result) <= budget


def test_active_context_dropped_when_it_does_not_fit():
    full = _primer(_overview(40), _key_concepts(4), _active_context(7))
    without_context = _primer(_overview(40), _key_concepts(4))
    budget = ap._estimate_tokens(without_context)
    assert ap._estimate_tokens(full) > budget

    result = ap._truncate_primer(full, budget)
    assert "Active Context" not in result
    assert "## Key Concepts" in result


def test_truncation_is_deterministic_for_same_input_and_budget():
    primer = _primer(_overview(40), _key_concepts(11), _navigation(30), _active_context(7))
    first = ap._truncate_primer(primer, 200)
    second = ap._truncate_primer(primer, 200)
    assert first == second


# ---------------------------------------------------------------------------
# _trim_section — extracted helper
# ---------------------------------------------------------------------------


def test_trim_section_returns_input_when_within_budget():
    section = "## Key Concepts\n\n- **Term0** — A definition."
    assert ap._trim_section(section, 100000) == section


def test_trim_section_reduces_definitions_to_terms_and_drops_blanks():
    section = "## Key Concepts\n\n- **Term0** — Long definition here.\n- **Term1** — Another one."
    assert ap._trim_section(section, 1) == "## Key Concepts\n- Term0\n- Term1"


def test_trim_section_preserves_text_outside_concepts_block():
    section = _primer(_overview(10), "Key Concepts\n\n- **Term0** — Def.")
    result = ap._trim_section(section, 1)
    assert result.startswith(_overview(10))
    assert "- Term0" in result


def test_trim_section_drops_bullet_lines_that_do_not_match_the_term_pattern():
    section = "## Key Concepts\n\n- **multi word** — def\n- plain line"
    assert ap._trim_section(section, 1) == "## Key Concepts"


def test_trim_section_without_concepts_block_is_preserved_verbatim():
    section = "## Active Context\n\n- **User:** Alice"
    assert ap._trim_section(section, 1) == section


# ---------------------------------------------------------------------------
# generate_primer — end-to-end budget enforcement
# ---------------------------------------------------------------------------


async def test_generate_primer_respects_token_budget() -> None:
    primer = await ap.generate_primer()
    assert ap._estimate_tokens(primer) <= ap._MAX_PRIMER_TOKENS
    assert primer
