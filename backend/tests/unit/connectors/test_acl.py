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
    with pytest.raises(ValueError):
        ConnectorACL(visibility="public")
