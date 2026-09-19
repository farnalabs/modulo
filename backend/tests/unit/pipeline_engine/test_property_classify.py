"""Hypothesis property-based tests for the pure run-classification cores.

Targets (all from ``modulo.core.pipeline_engine.classify``):

* ``_is_valid_pr_url`` -- URL validity predicate
* ``collect_pr_urls`` -- deduplicated PR-URL collection
* ``classify_run`` -- the FAR-189 decision table (pure over status + evidence)

Properties assert INVARIANTS, not specific outputs.  Chosen because the
module is explicitly documented as pure and unit-testable.
"""

from __future__ import annotations

import hypothesis.strategies as st
from hypothesis import given, settings

from modulo.core.pipeline_engine.classify import (
    _COUNTABLE_NO_DELIVERY_STATUSES,
    _DELIVERABLE_STATUSES,
    _EXCLUDED_STATUSES,
    RunClassificationValue,
    _is_valid_pr_url,
    classify_run,
    collect_pr_urls,
)

# Well-formed HTTPS URLs with guaranteed netloc.
valid_https_urls = st.from_regex(r"https://[a-z0-9][a-z0-9.-]+/[a-z0-9_./-]+", fullmatch=True)

TERMINAL = frozenset(
    {
        "cancelled",
        "budget_exceeded",
        "router_no_match",
        "failed",
        "eval_failed",
        "stalled",
        "compensation_failed",
        "complete",
        "cost_ceiling_exceeded",
    }
)


# ---------------------------------------------------------------------------
# 1. _is_valid_pr_url -- pure URL validation
# ---------------------------------------------------------------------------


class TestIsValidPrUrlProperties:
    """Properties for the PR-URL validity predicate."""

    @given(url=st.just(""))
    def test_empty_string_is_invalid(self, url: str) -> None:
        assert _is_valid_pr_url(url) is False

    @given(url=st.none())
    def test_none_is_invalid(self, url: object) -> None:
        assert _is_valid_pr_url(url) is False  # type: ignore[arg-type]

    @given(url=st.integers())
    def test_non_string_is_invalid(self, url: object) -> None:
        assert _is_valid_pr_url(url) is False  # type: ignore[arg-type]

    @given(url=st.sampled_from([" ", "  ", "\t", "\n", "\r\n", "   "]))
    def test_whitespace_only_is_invalid(self, url: str) -> None:
        assert _is_valid_pr_url(url) is False

    @given(url=st.from_regex(r"ftp://[a-z0-9.-]+", fullmatch=True))
    def test_non_http_scheme_is_invalid(self, url: str) -> None:
        assert _is_valid_pr_url(url) is False

    @given(url=st.just("https://"))
    def test_empty_netloc_is_invalid(self, url: str) -> None:
        assert _is_valid_pr_url(url) is False

    @given(url=st.text(min_size=1, max_size=200))
    @settings(max_examples=100)
    def test_result_is_always_bool(self, url: str) -> None:
        """The function must never raise for any string input."""
        result = _is_valid_pr_url(url)
        assert isinstance(result, bool)

    @given(url=valid_https_urls)
    @settings(max_examples=30)
    def test_idempotent_under_whitespace_stripping(self, url: str) -> None:
        """Validity is preserved across leading/trailing whitespace."""
        stripped = url.strip()
        if _is_valid_pr_url(stripped):
            assert _is_valid_pr_url(f"  {stripped}  ") is True

    @given(
        prefix=st.sampled_from(["prefix_", "garbage:", "not-a-url", "123"]),
        url=valid_https_urls,
    )
    @settings(max_examples=20)
    def test_prepending_garbage_makes_url_invalid(self, prefix: str, url: str) -> None:
        """Prepending non-URL text should invalidate a valid URL."""
        if _is_valid_pr_url(url):
            assert _is_valid_pr_url(prefix + url) is False


# ---------------------------------------------------------------------------
# 2. collect_pr_urls -- deduplicated collection
# ---------------------------------------------------------------------------


class TestCollectPrUrlsProperties:
    """Properties for the deduplicated PR-URL collector."""

    @given(urls=st.lists(valid_https_urls, min_size=1, max_size=10))
    @settings(max_examples=50)
    def test_no_duplicates_in_output(self, urls: list[str]) -> None:
        """Output must never contain duplicate URLs."""
        markers = {f"attempt_{i}": {"pr_url": u} for i, u in enumerate(urls)}
        result = collect_pr_urls(None, None, markers)
        assert len(result) == len(set(result))

    @given(url=valid_https_urls)
    @settings(max_examples=30)
    def test_single_valid_url_collected(self, url: str) -> None:
        """A single valid URL in markers should be collected."""
        markers = {"attempt_0": {"pr_url": url}}
        result = collect_pr_urls(None, None, markers)
        assert len(result) >= 1

    @given(n=st.integers(min_value=0, max_value=20))
    @settings(max_examples=20)
    def test_empty_sources_yield_empty_result(self, n: int) -> None:
        """No sources = no URLs."""
        result = collect_pr_urls(None, None, None)
        assert result == []

    @given(
        urls_a=st.lists(valid_https_urls, min_size=1, max_size=5),
        urls_b=st.lists(valid_https_urls, min_size=1, max_size=5),
    )
    @settings(max_examples=30)
    def test_cross_source_deduplication(self, urls_a: list[str], urls_b: list[str]) -> None:
        """URLs appearing in both sources appear only once in output."""
        markers = {}
        for i, u in enumerate(urls_a):
            markers[f"a_{i}"] = {"pr_url": u}
        for i, u in enumerate(urls_b):
            markers[f"b_{i}"] = {"pr_url": u}
        result = collect_pr_urls(None, None, markers)
        assert len(result) == len(set(result))

    @given(url=st.text(min_size=1, max_size=100))
    @settings(max_examples=50)
    def test_invalid_marker_pr_urls_not_collected(self, url: str) -> None:
        """Only valid HTTP(S) URLs with a netloc survive collection."""
        markers = {"attempt_0": {"pr_url": url}}
        result = collect_pr_urls(None, None, markers)
        for collected in result:
            assert _is_valid_pr_url(collected)


# ---------------------------------------------------------------------------
# 3. classify_run -- the FAR-189 decision table
# ---------------------------------------------------------------------------


class TestClassifyRunProperties:
    """Properties for the pure classification decision table."""

    @given(status=st.sampled_from(sorted(_EXCLUDED_STATUSES)))
    @settings(max_examples=20)
    def test_excluded_statuses_classify_as_excluded(self, status: str) -> None:
        """Every status in _EXCLUDED_STATUSES classifies as 'excluded'."""
        result = classify_run(status, None)
        assert result.value == RunClassificationValue.excluded

    @given(status=st.sampled_from(sorted(_COUNTABLE_NO_DELIVERY_STATUSES)))
    @settings(max_examples=20)
    def test_countable_statuses_classify_as_no_delivery(self, status: str) -> None:
        """Every countable status classifies as 'no_delivery'."""
        result = classify_run(status, None)
        assert result.value == RunClassificationValue.no_delivery

    @given(
        status=st.sampled_from(
            sorted(TERMINAL - _EXCLUDED_STATUSES - _COUNTABLE_NO_DELIVERY_STATUSES - _DELIVERABLE_STATUSES)
        )
    )
    @settings(max_examples=20)
    def test_unknown_terminal_classifies_as_excluded(self, status: str) -> None:
        """Terminal statuses outside known buckets hit fail-safe excluded."""
        result = classify_run(status, None)
        assert result.value == RunClassificationValue.excluded

    @given(status=st.text(min_size=1, max_size=50).filter(lambda s: s not in TERMINAL))
    @settings(max_examples=30)
    def test_non_terminal_status_classifies_as_excluded(self, status: str) -> None:
        """Non-terminal statuses are caught by the guard."""
        result = classify_run(status, None)
        assert result.value == RunClassificationValue.excluded

    @given(status=st.sampled_from(sorted(_EXCLUDED_STATUSES)))
    @settings(max_examples=10)
    def test_classification_is_deterministic(self, status: str) -> None:
        """Same inputs produce same value and reason."""
        first = classify_run(status, None)
        second = classify_run(status, None)
        assert first.value == second.value
        assert first.reason == second.reason

    @given(
        status=st.sampled_from(sorted(_COUNTABLE_NO_DELIVERY_STATUSES)),
        error_code=st.text(min_size=0, max_size=50),
    )
    @settings(max_examples=30)
    def test_countable_never_delivers(self, status: str, error_code: str) -> None:
        """A countable status NEVER classifies as 'delivered'."""
        result = classify_run(status, error_code or None)
        assert result.value != RunClassificationValue.delivered

    @given(status=st.sampled_from(sorted(_EXCLUDED_STATUSES)))
    @settings(max_examples=10)
    def test_excluded_never_delivers(self, status: str) -> None:
        """An excluded status NEVER classifies as 'delivered'."""
        result = classify_run(status, "any_error_code")
        assert result.value != RunClassificationValue.delivered

    @given(
        status=st.sampled_from(sorted(_COUNTABLE_NO_DELIVERY_STATUSES)),
        work_intact=st.booleans() | st.none(),
    )
    @settings(max_examples=15)
    def test_work_intact_preserved_on_countable(self, status: str, work_intact: bool | None) -> None:
        """work_intact metadata is carried through."""
        result = classify_run(status, None, work_intact=work_intact)
        assert result.work_intact == work_intact

    @given(
        status=st.sampled_from(sorted(TERMINAL)),
        error_code=st.text(min_size=0, max_size=100),
    )
    @settings(max_examples=50)
    def test_result_is_always_valid_enum(self, status: str, error_code: str) -> None:
        """The result value is always a valid RunClassificationValue."""
        result = classify_run(status, error_code or None)
        assert isinstance(result.value, RunClassificationValue)

    @given(
        error_code=st.sampled_from(["hitl.required", "human_review_needed", "harness.gate_creation_failed"]),
    )
    @settings(max_examples=10)
    def test_needs_human_codes_produce_needs_human_reason(self, error_code: str) -> None:
        """Error codes with 'hitl'/'human' or in _NEEDS_HUMAN_CODES."""
        from modulo.core.pipeline_engine.classify import REASON_NEEDS_HUMAN

        result = classify_run("failed", error_code)
        assert result.value == RunClassificationValue.no_delivery
        assert result.reason == REASON_NEEDS_HUMAN

    @given(
        error_code=st.sampled_from(["sandbox.timeout", "node.cancelled", "connector.auth_failed"]),
    )
    @settings(max_examples=10)
    def test_source_error_codes_produce_source_error_reason(self, error_code: str) -> None:
        """Source/infra error codes produce source_error reason."""
        from modulo.core.pipeline_engine.classify import REASON_SOURCE_ERROR

        result = classify_run("failed", error_code)
        assert result.value == RunClassificationValue.no_delivery
        assert result.reason == REASON_SOURCE_ERROR

    @given(status=st.sampled_from(sorted(TERMINAL)))
    @settings(max_examples=15)
    def test_delivered_pr_urls_is_always_tuple(self, status: str) -> None:
        """delivered_pr_urls is always a tuple."""
        result = classify_run(status, None)
        assert isinstance(result.delivered_pr_urls, tuple)

    @given(
        markers=st.just({"attempt_0": {"delivery_done": True}}),
    )
    @settings(max_examples=10)
    def test_delivery_done_marker_yields_delivered(self, markers: dict) -> None:
        """A 'complete' run with delivery_done marker -> delivered."""
        from modulo.core.pipeline_engine.classify import REASON_DELIVERED_EMAIL

        result = classify_run("complete", None, raw_output_markers=markers)
        assert result.value == RunClassificationValue.delivered
        assert result.reason == REASON_DELIVERED_EMAIL

    @given(
        status=st.sampled_from(sorted(_COUNTABLE_NO_DELIVERY_STATUSES - {"compensation_failed"})),
        markers=st.dictionaries(
            keys=st.text(min_size=1, max_size=20, alphabet="abcdef0123456789"),
            values=st.fixed_dictionaries({"parse_error": st.text(min_size=1, max_size=50)}),
            min_size=1,
            max_size=3,
        ),
    )
    @settings(max_examples=20)
    def test_parse_error_marker_yields_parse_error_reason(self, status: str, markers: dict) -> None:
        """A marker with parse_error -> parse_error reason."""
        from modulo.core.pipeline_engine.classify import REASON_PARSE_ERROR

        result = classify_run(status, None, raw_output_markers=markers)
        assert result.value == RunClassificationValue.no_delivery
        assert result.reason == REASON_PARSE_ERROR
