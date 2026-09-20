"""Branch-coverage tests for SCIM route helpers (scim.py).

Tests pure helper functions that don't need a database session.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from modulo.api.routes.scim import (
    _get_base_url,
    _group_to_scim,
    _scim_error,
    _user_to_scim,
)

# ---------------------------------------------------------------------------
# _scim_error
# ---------------------------------------------------------------------------


def test_scim_error_returns_http_exception():
    """_scim_error returns an HTTPException with SCIM error body."""
    exc = _scim_error(409, "User already exists")
    assert isinstance(exc, HTTPException)
    assert exc.status_code == 409
    assert exc.detail["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]
    assert exc.detail["detail"] == "User already exists"
    assert exc.detail["status"] == "409"


def test_scim_error_404():
    exc = _scim_error(404, "Not found")
    assert exc.status_code == 404


# ---------------------------------------------------------------------------
# _user_to_scim
# ---------------------------------------------------------------------------


def _make_account(**overrides):
    """Create a mock Account with sensible defaults."""
    account = MagicMock()
    account.id = overrides.get("id", uuid.uuid4())
    account.email = overrides.get("email", "alice@example.com")
    account.display_name = overrides.get("display_name", "Alice Smith")
    account.active = overrides.get("active", True)
    account.created_at = overrides.get("created_at", datetime(2025, 1, 1, tzinfo=UTC))
    account.updated_at = overrides.get("updated_at", datetime(2025, 6, 1, tzinfo=UTC))
    return account


def test_user_to_scim_basic():
    """Basic conversion with display name containing first+last."""
    account = _make_account(display_name="Alice Smith")
    result = _user_to_scim(account, "https://app.modulo.run")
    assert result["schemas"] == ["urn:ietf:params:scim:schemas:core:2.0:User"]
    assert result["userName"] == "alice@example.com"
    assert result["name"]["givenName"] == "Alice"
    assert result["name"]["familyName"] == "Smith"
    assert result["name"]["formatted"] == "Alice Smith"
    assert result["active"] is True


def test_user_to_scim_single_name():
    """Display name with only one word → familyName is empty."""
    account = _make_account(display_name="Cher")
    result = _user_to_scim(account, "https://app.modulo.run")
    assert result["name"]["givenName"] == "Cher"
    assert result["name"]["familyName"] == ""


def test_user_to_scim_none_display_name():
    """None display_name → empty given/family."""
    account = _make_account(display_name=None)
    result = _user_to_scim(account, "https://app.modulo.run")
    assert result["name"]["givenName"] == ""
    assert result["name"]["familyName"] == ""
    assert result["name"]["formatted"] is None


def test_user_to_scim_none_timestamps():
    """None timestamps → empty strings."""
    account = _make_account(created_at=None, updated_at=None)
    result = _user_to_scim(account, "https://app.modulo.run")
    assert result["meta"]["created"] == ""
    assert result["meta"]["lastModified"] == ""


def test_user_to_scim_multi_word_name():
    """Display name with multiple parts → familyName is everything after first."""
    account = _make_account(display_name="Mary Jane Watson")
    result = _user_to_scim(account, "https://app.modulo.run")
    assert result["name"]["givenName"] == "Mary"
    assert result["name"]["familyName"] == "Jane Watson"


def test_user_to_scim_emails():
    """Emails list is properly constructed."""
    account = _make_account(email="bob@example.com")
    result = _user_to_scim(account, "https://app.modulo.run")
    assert len(result["emails"]) == 1
    assert result["emails"][0]["value"] == "bob@example.com"
    assert result["emails"][0]["type"] == "work"
    assert result["emails"][0]["primary"] is True


def test_user_to_scim_location():
    """Location URL is properly constructed."""
    account = _make_account()
    result = _user_to_scim(account, "https://app.modulo.run")
    assert f"/scim/v2/Users/{account.id}" in result["meta"]["location"]


def test_user_to_scim_external_id():
    """externalId matches the account id."""
    account = _make_account()
    result = _user_to_scim(account, "https://app.modulo.run")
    assert result["externalId"] == str(account.id)


# ---------------------------------------------------------------------------
# _group_to_scim
# ---------------------------------------------------------------------------


def _make_group(**overrides):
    """Create a mock Team with sensible defaults."""
    group = MagicMock()
    group.id = overrides.get("id", uuid.uuid4())
    group.name = overrides.get("name", "Engineering")
    group.created_at = overrides.get("created_at", datetime(2025, 1, 1, tzinfo=UTC))
    group.updated_at = overrides.get("updated_at", datetime(2025, 6, 1, tzinfo=UTC))
    return group


def test_group_to_scim_basic():
    """Basic group conversion."""
    group = _make_group(name="Engineering")
    members = [{"value": str(uuid.uuid4()), "type": "User"}]
    result = _group_to_scim(group, members, "https://app.modulo.run")
    assert result["schemas"] == ["urn:ietf:params:scim:schemas:core:2.0:Group"]
    assert result["displayName"] == "Engineering"
    assert result["members"] == members


def test_group_to_scim_empty_members():
    """Empty members list."""
    group = _make_group()
    result = _group_to_scim(group, [], "https://app.modulo.run")
    assert result["members"] == []


def test_group_to_scim_none_timestamps():
    """None timestamps → empty strings."""
    group = _make_group(created_at=None, updated_at=None)
    result = _group_to_scim(group, [], "https://app.modulo.run")
    assert result["meta"]["created"] == ""
    assert result["meta"]["lastModified"] == ""


def test_group_to_scim_location():
    """Location URL is properly constructed."""
    group = _make_group()
    result = _group_to_scim(group, [], "https://app.modulo.run")
    assert f"/scim/v2/Groups/{group.id}" in result["meta"]["location"]


def test_group_to_scim_external_id():
    """externalId matches the group id."""
    group = _make_group()
    result = _group_to_scim(group, [], "https://app.modulo.run")
    assert result["externalId"] == str(group.id)


# ---------------------------------------------------------------------------
# _get_base_url
# ---------------------------------------------------------------------------


def test_get_base_url_strips_trailing_slash():
    """Trailing slash is stripped."""
    settings = MagicMock()
    settings.modulo_public_url = "https://app.modulo.run/"
    assert _get_base_url(settings) == "https://app.modulo.run"


def test_get_base_url_no_trailing_slash():
    """No trailing slash → returned as-is."""
    settings = MagicMock()
    settings.modulo_public_url = "https://app.modulo.run"
    assert _get_base_url(settings) == "https://app.modulo.run"


def test_get_base_url_none_raises():
    """None URL → 500 HTTPException."""
    settings = MagicMock()
    settings.modulo_public_url = None
    with pytest.raises(HTTPException) as exc_info:
        _get_base_url(settings)
    assert exc_info.value.status_code == 500


def test_get_base_url_empty_string_raises():
    """Empty string URL → 500 HTTPException."""
    settings = MagicMock()
    settings.modulo_public_url = ""
    with pytest.raises(HTTPException) as exc_info:
        _get_base_url(settings)
    assert exc_info.value.status_code == 500
