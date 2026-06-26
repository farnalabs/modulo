"""Unit tests for ConnectorACL."""

import pytest

from modulo.connectors.base import ConnectorACL, ConnectorPermissionError


def test_acl_org_visibility():
    acl = ConnectorACL(visibility="org")
    acl.check("read")  # should pass — empty allowed_ops means no restriction


def test_acl_team_visibility():
    acl = ConnectorACL(visibility="team", allowed_operations=["read"])
    acl.check("read", request_visibility="team")  # should pass


def test_acl_blocks_unlisted_operation():
    acl = ConnectorACL(visibility="org", allowed_operations=["read"])
    with pytest.raises(ConnectorPermissionError, match="not in allowed_operations"):
        acl.check("write")


def test_acl_allows_any_when_none_ops():
    # None means no restriction on operations
    acl = ConnectorACL(visibility="org", allowed_operations=None)
    acl.check("read")
    acl.check("write")
    acl.check("git_push")


def test_acl_blocks_wrong_visibility():
    acl = ConnectorACL(visibility="org")
    with pytest.raises(ConnectorPermissionError, match="team-scoped"):
        acl.check("read", request_visibility="team")


def test_invalid_visibility_raises():
    with pytest.raises(ValueError):
        ConnectorACL(visibility="public")
