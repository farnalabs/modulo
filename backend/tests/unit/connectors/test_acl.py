"""Unit tests for ConnectorACL."""

import inspect

import pytest

from modulo.connectors.base import ConnectorACL, ConnectorPermissionError, unrestricted_allowed_operations


def test_acl_org_visibility():
    acl = ConnectorACL(visibility="org")
    # empty allowed_ops means no restriction — must not raise
    assert acl.check("read") is None
    assert acl.check("write") is None


def test_acl_team_visibility():
    acl = ConnectorACL(visibility="team", allowed_operations=["read"])
    # the allowlist still applies on a team connector
    assert acl.check("read") is None


def test_acl_blocks_unlisted_operation():
    acl = ConnectorACL(visibility="org", allowed_operations=["read"])
    with pytest.raises(ConnectorPermissionError, match="not in allowed_operations"):
        acl.check("write")


def test_acl_operation_matching_is_case_sensitive():
    acl = ConnectorACL(visibility="org", allowed_operations=["read"])
    with pytest.raises(ConnectorPermissionError, match="not in allowed_operations"):
        acl.check("READ")


def test_acl_empty_allowlist_is_unrestricted():
    # FAR-1564: an *explicit* empty allowlist is the unset value every
    # REST/MCP/UI-created connector stores. It means "nothing configured to
    # restrict" — identical to None — never deny-all.
    acl = ConnectorACL(visibility="org", allowed_operations=[])
    assert acl.allowed_operations is not None
    assert not acl.allowed_operations
    assert acl.check("read") is None
    assert acl.check("write") is None
    assert acl.check("trigger_run") is None


@pytest.mark.parametrize("visibility", ["org", "team"])
def test_acl_empty_allowlist_is_unrestricted_for_every_visibility(visibility):
    # An unrestricted operation scope is unrestricted regardless of the
    # connector's visibility. Made revert-sensitive by the signature
    # assertion: visibility USED to narrow check() for a team-scoped caller
    # (the FAR-516 run-gate), so re-threading that axis fails here even though
    # the behavioural assertions below would still pass without it.
    acl = ConnectorACL(visibility=visibility, allowed_operations=[])
    assert "request_visibility" not in inspect.signature(acl.check).parameters
    assert acl.check("read") is None
    assert acl.check("write") is None


def test_acl_team_connector_allows_org_request():
    # Control: an unset allowlist is unrestricted (FAR-1564), so a team
    # connector serves a plain read. That a caller's scope cannot even be
    # expressed to check() is pinned by
    # test_acl_org_connector_is_shared_and_carries_no_request_scope_axis.
    acl = ConnectorACL(visibility="team")
    assert acl.check("read") is None


def test_acl_team_connector_still_enforces_allowlist():
    acl = ConnectorACL(visibility="team", allowed_operations=["read"])
    with pytest.raises(ConnectorPermissionError, match="not in allowed_operations"):
        acl.check("write")


def test_acl_allows_any_when_none_ops():
    # None means no restriction on operations
    acl = ConnectorACL(visibility="org", allowed_operations=None)
    assert acl.allowed_operations is None
    assert acl.check("read") is None
    assert acl.check("write") is None
    assert acl.check("git_push") is None


def test_acl_allowlist_is_normalised_to_frozenset():
    acl = ConnectorACL(visibility="org", allowed_operations=["read", "read", "write"])
    assert acl.allowed_operations == frozenset({"read", "write"})


def test_acl_org_connector_is_shared_and_carries_no_request_scope_axis():
    # FAR-1618 (reverts the FAR-516 run-gate): an org-visibility connector is
    # shared across the organisation and binds to ANY pipeline, including a
    # team-owned one, so check() takes no caller-scope parameter at all. The
    # signature assertion is the structural guard — re-threading
    # ``request_visibility`` through the ACL fails here before any behaviour
    # can silently regress.
    acl = ConnectorACL(visibility="org")
    assert "request_visibility" not in inspect.signature(acl.check).parameters
    assert acl.check("read") is None
    assert acl.check("write") is None


def test_invalid_visibility_raises():
    with pytest.raises(ValueError, match="visibility must be 'org' or 'team'"):
        ConnectorACL(visibility="public")


# ---------------------------------------------------------------------------
# Shared unrestricted predicate + malformed fail-closed (FAR-1564)
# ---------------------------------------------------------------------------


def test_unrestricted_predicate_only_accepts_none_and_empty_list():
    assert unrestricted_allowed_operations(None)
    assert unrestricted_allowed_operations([])
    assert not unrestricted_allowed_operations(["read"])
    assert not unrestricted_allowed_operations({"read": 1})
    assert not unrestricted_allowed_operations("read")
    assert not unrestricted_allowed_operations(0)


def test_acl_malformed_dict_allowlist_fails_closed():
    # A malformed non-list must never be read as UNRESTRICTED: previously
    # ``frozenset({"read": 1})`` silently became the allowlist ``{"read"}``,
    # so a malformed value CERTIFIED an operation the stored value never
    # declared — while the graph validator and the guardrail conformance
    # reader read the same value restrictively.
    acl = ConnectorACL(visibility="org", allowed_operations={"read": 1})  # type: ignore[arg-type]
    assert acl.allowed_operations is not None
    assert not acl.allowed_operations
    with pytest.raises(ConnectorPermissionError, match="not in allowed_operations"):
        acl.check("read")


def test_acl_malformed_int_allowlist_fails_closed():
    # ``frozenset(7)`` used to raise TypeError at construction; now a
    # malformed value restricts to the empty allowlist, so EVERY operation is
    # denied (fail closed) rather than crashing the ACL build.
    acl = ConnectorACL(visibility="org", allowed_operations=7)  # type: ignore[arg-type]
    with pytest.raises(ConnectorPermissionError, match="not in allowed_operations"):
        acl.check("read")
    with pytest.raises(ConnectorPermissionError, match="not in allowed_operations"):
        acl.check("write")


# ---------------------------------------------------------------------------
# One canonical vocabulary across certification + enforcement (FAR-1594)
# ---------------------------------------------------------------------------


def test_acl_legacy_type_qualified_allowlist_grants_bare_capability():
    # FAR-1594 defect (a): a stored ["github.read"] on a GITHUB surface must
    # GRANT "read" — the exact answer the guardrail conformance reader
    # certifies for the same stored value. Before the shared canonicalisation
    # the ACL matched raw membership, so certification and enforcement gave
    # OPPOSITE answers. The surface type is supplied (FAR-1616) so the
    # same-type qualifier can be verified.
    acl = ConnectorACL(visibility="org", allowed_operations=["github.read"], connector_type_id="github")
    assert acl.allowed_operations == frozenset({"read"})
    assert acl.check("read") is None


def test_acl_check_accepts_a_legacy_spelling_of_a_granted_operation():
    # The check side canonicalises with the SAME helper as the stored side, so
    # a legacy-qualified request matches a bare grant (mirrors a qualified
    # conformance claim matching a bare manifest entry).
    acl = ConnectorACL(visibility="org", allowed_operations=["read"])
    assert acl.check("github.read") is None


def test_acl_non_capability_entry_grants_nothing():
    # An entry outside the capability vocabulary certifies nothing here either
    # — the same DROP the conformance manifest reader applies.
    acl = ConnectorACL(visibility="org", allowed_operations=["not-a-capability"])
    assert not acl.allowed_operations
    with pytest.raises(ConnectorPermissionError, match="not in allowed_operations"):
        acl.check("read")


# ---------------------------------------------------------------------------
# Type-qualified allowlist entries bind to the surface's OWN type (FAR-1616)
# ---------------------------------------------------------------------------


def test_acl_mis_typed_legacy_allowlist_entry_grants_nothing():
    # FAR-1616: ["github.write"] on a FILESYSTEM connector must NOT grant bare
    # "write". FAR-1594's canonicalisation dropped the qualifier unconditionally,
    # so the mis-typed entry silently granted on the wrong surface; the entry is
    # now REJECTED (fail closed) and every operation is denied.
    acl = ConnectorACL(visibility="org", allowed_operations=["github.write"], connector_type_id="filesystem")
    assert acl.allowed_operations is not None
    assert not acl.allowed_operations
    with pytest.raises(ConnectorPermissionError, match="not in allowed_operations"):
        acl.check("write")
    with pytest.raises(ConnectorPermissionError, match="not in allowed_operations"):
        acl.check("read")


def test_acl_unverifiable_legacy_qualifier_grants_nothing():
    # Without the surface's type the qualifier cannot be verified, so a
    # qualified entry fails CLOSED — never a grant. Construction sites that
    # know the type must supply it (the connector hub does).
    acl = ConnectorACL(visibility="org", allowed_operations=["github.write"])
    assert acl.allowed_operations is not None
    assert not acl.allowed_operations
    with pytest.raises(ConnectorPermissionError, match="not in allowed_operations"):
        acl.check("write")


def test_acl_same_type_colon_spelling_still_grants():
    # Both accepted separators keep granting on a same-type surface.
    acl = ConnectorACL(visibility="org", allowed_operations=["github:write"], connector_type_id="github")
    assert acl.allowed_operations == frozenset({"write"})
    assert acl.check("write") is None


def test_acl_restricted_allowlist_rejects_only_the_mis_typed_entry():
    # A mixed allowlist keeps its bare + same-type grants; only the mis-typed
    # qualified entry is dropped.
    acl = ConnectorACL(
        visibility="org",
        allowed_operations=["read", "github.write", "filesystem.write"],
        connector_type_id="github",
    )
    assert acl.allowed_operations == frozenset({"read", "write"})
    assert acl.check("read") is None
    assert acl.check("write") is None


def test_acl_unrestricted_semantics_survive_the_type_check():
    # FAR-1564: None/[] mean UNRESTRICTED regardless of the supplied type —
    # the type check only applies to a NON-EMPTY allowlist's entries.
    for ops in (None, []):
        acl = ConnectorACL(visibility="org", allowed_operations=ops, connector_type_id="filesystem")
        assert acl.check("write") is None
        assert acl.check("read") is None
