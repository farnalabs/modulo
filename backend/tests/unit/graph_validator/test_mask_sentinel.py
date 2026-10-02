"""Unit tests for the FAR-1374 mask-sentinel detector.

``modulo.core.graph_validator.mask_sentinel`` is the single shared detection
rule behind the composite-template write gates, the expansion rejection, the
graph validator, and the housekeeping sweep. These tests pin the walker's
shape handling (scalar / dict / list / non-dict node), the dotted-index path
renderer, the stored-counterpart resolver, and the bounded detail formatter
directly, so no detection shape can regress silently.
"""

from modulo.core.graph_validator.mask_sentinel import (
    MaskSentinelFinding,
    _value_at_path,
    find_mask_sentinel_values,
    find_unresolved_mask_sentinels,
    format_mask_sentinel_detail,
    format_mask_sentinel_path,
)
from modulo.core.secret_patterns import SENSITIVE_VALUE_MASK


def test_format_path_renders_index_then_nested_key() -> None:
    """An int segment renders as ``[i]``; a later string appends ``.key``."""
    assert format_mask_sentinel_path(("outer", 0, "inner")) == "outer[0].inner"


def test_format_path_of_empty_path_is_empty_string() -> None:
    assert not format_mask_sentinel_path(())


def test_describe_scalar_field_value_uses_value_wording() -> None:
    """An empty path (a scalar field value) describes the value, not a key."""
    finding = MaskSentinelFinding(node_id="n1", field="env_vars", path=())
    assert finding.describe() == "sub-node 'n1' field 'env_vars' value"
    assert not finding.key


def test_describe_keyed_value_names_the_key() -> None:
    finding = MaskSentinelFinding(node_id="n1", field="env_vars", path=("GITHUB_TOKEN",))
    assert finding.describe() == "sub-node 'n1' field 'env_vars' key 'GITHUB_TOKEN'"
    assert finding.key == "GITHUB_TOKEN"


def test_walker_reports_sentinel_inside_list_index() -> None:
    """A list-valued field is walked and the finding carries the list index."""
    nodes = [{"id": "n1", "env_vars": ["safe", f"Bearer {SENSITIVE_VALUE_MASK}"]}]
    findings = find_mask_sentinel_values(nodes)
    assert [(f.field, f.path) for f in findings] == [("env_vars", (1,))]


def test_scalar_credential_field_is_reported_with_empty_path() -> None:
    """A non-container field value is never skipped - empty path, not fail-open."""
    nodes = [{"id": "n1", "env_vars": SENSITIVE_VALUE_MASK}]
    findings = find_mask_sentinel_values(nodes)
    assert [(f.field, f.path) for f in findings] == [("env_vars", ())]


def test_walker_ignores_nested_dict_without_sentinel() -> None:
    nodes = [{"id": "n1", "env_vars": {"OUTER": {"INNER": "real-value"}}}]
    assert not find_mask_sentinel_values(nodes)


def test_walker_ignores_a_non_container_field_value() -> None:
    """A non-str/dict/list field value (an int) carries no sentinel and no path."""
    nodes = [{"id": "n1", "env_vars": 12345}]
    assert not find_mask_sentinel_values(nodes)


def test_non_dict_node_is_skipped() -> None:
    nodes = ["not-a-dict", {"id": "n1", "env_vars": {"K": SENSITIVE_VALUE_MASK}}]
    assert [f.node_id for f in find_mask_sentinel_values(nodes)] == ["n1"]


def test_value_at_path_empty_path_returns_the_container() -> None:
    container = {"K": "v"}
    assert _value_at_path(container, ()) is container


def test_value_at_path_indexes_into_a_list() -> None:
    assert _value_at_path(["a", "b"], (1,)) == "b"


def test_value_at_path_missing_index_returns_none() -> None:
    assert _value_at_path(["a"], (5,)) is None


def test_unresolved_ignores_non_dict_and_idless_stored_nodes() -> None:
    """Only stored dict nodes WITH an id form the counterpart index."""
    incoming = [{"id": "n1", "env_vars": {"K": SENSITIVE_VALUE_MASK}}]
    stored = [
        "not-a-dict",
        {"env_vars": {"K": "real"}},
        {"id": "n2", "env_vars": {"K": "real"}},
    ]
    assert [f.node_id for f in find_unresolved_mask_sentinels(incoming, stored)] == ["n1"]


def test_unresolved_empty_when_stored_counterpart_present() -> None:
    incoming = [{"id": "n1", "env_vars": {"K": SENSITIVE_VALUE_MASK}}]
    stored = [{"id": "n1", "env_vars": {"K": "real"}}]
    assert not find_unresolved_mask_sentinels(incoming, stored)


def test_unresolved_resolves_a_list_index_counterpart() -> None:
    """An int path segment resolves through the stored list, not to None."""
    incoming = [{"id": "n1", "env_vars": ["x", SENSITIVE_VALUE_MASK]}]
    stored = [{"id": "n1", "env_vars": ["x", "real"]}]
    assert not find_unresolved_mask_sentinels(incoming, stored)


def test_unresolved_none_stored_marks_every_finding() -> None:
    incoming = [{"id": "n1", "env_vars": {"K": SENSITIVE_VALUE_MASK}}]
    assert [f.node_id for f in find_unresolved_mask_sentinels(incoming, None)] == ["n1"]


def test_format_detail_truncates_and_counts_the_remainder() -> None:
    findings = [MaskSentinelFinding(node_id=f"n{i}", field="env_vars", path=("K",)) for i in range(3)]
    assert format_mask_sentinel_detail(findings, limit=1) == ("sub-node 'n0' field 'env_vars' key 'K'; (+2 more)")


def test_format_detail_within_limit_has_no_remainder() -> None:
    findings = [MaskSentinelFinding(node_id="n0", field="env_vars", path=("K",))]
    assert format_mask_sentinel_detail(findings, limit=5) == "sub-node 'n0' field 'env_vars' key 'K'"
