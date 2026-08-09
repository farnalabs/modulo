"""Unit tests for ConnectorACL."""

import pytest

from modulo.connectors.base import ConnectorACL, ConnectorPermissionError


def test_acl_org_visibility():
    acl = ConnectorACL(visibility="org")
    # empty allowed_ops means no restriction — must not raise
    assert acl.check("read") is None
    assert acl.check("write") is None


def test_acl_team_visibility():
    acl = ConnectorACL(visibility="team", allowed_operations=["read"])
    # team-scoped access on a team connector must not raise
    assert acl.check("read", request_visibility="team") is None


def test_acl_blocks_unlisted_operation():
    acl = ConnectorACL(visibility="org", allowed_operations=["read"])
    with pytest.raises(ConnectorPermissionError, match="not in allowed_operations"):
        acl.check("write")


def test_acl_operation_matching_is_case_sensitive():
    acl = ConnectorACL(visibility="org", allowed_operations=["read"])
    with pytest.raises(ConnectorPermissionError, match="not in allowed_operations"):
        acl.check("READ")


def test_acl_empty_allowlist_denies_all_operations():
    # An *explicit* empty allowlist is distinct from None — it must deny
    # every operation, not fall back to the unrestricted default.
    acl = ConnectorACL(visibility="org", allowed_operations=[])
    assert acl.allowed_operations == frozenset()
    with pytest.raises(ConnectorPermissionError, match="No operations allowed"):
        acl.check("read")


def test_acl_team_connector_allows_org_request():
    # Team connectors serve org-scoped requests; only the reverse (team-scoped
    # access on an org-only connector) is forbidden.
    acl = ConnectorACL(visibility="team")
    assert acl.check("read", request_visibility="org") is None


def test_acl_team_connector_still_enforces_allowlist():
    acl = ConnectorACL(visibility="team", allowed_operations=["read"])
    with pytest.raises(ConnectorPermissionError, match="not in allowed_operations"):
        acl.check("write", request_visibility="team")


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


def test_acl_blocks_wrong_visibility():
    acl = ConnectorACL(visibility="org")
    with pytest.raises(ConnectorPermissionError, match="team-scoped"):
        acl.check("read", request_visibility="team")


def test_invalid_visibility_raises():
    with pytest.raises(ValueError, match="visibility must be 'org' or 'team'"):
        ConnectorACL(visibility="public")
