"""Branch-coverage tests for SCIM route helpers (scim.py).

Tests pure helper functions that don't need a database session.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from farna.api.routes import scim as scim_routes
from farna.api.routes.scim import (
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
    result = _user_to_scim(account, "https://control.farna.dev")
    assert result["schemas"] == ["urn:ietf:params:scim:schemas:core:2.0:User"]
    assert result["userName"] == "alice@example.com"
    assert result["name"]["givenName"] == "Alice"
    assert result["name"]["familyName"] == "Smith"
    assert result["name"]["formatted"] == "Alice Smith"
    assert result["active"] is True


def test_user_to_scim_single_name():
    """Display name with only one word → familyName is empty."""
    account = _make_account(display_name="Cher")
    result = _user_to_scim(account, "https://control.farna.dev")
    assert result["name"]["givenName"] == "Cher"
    assert not result["name"]["familyName"]


def test_user_to_scim_none_display_name():
    """None display_name → empty given/family."""
    account = _make_account(display_name=None)
    result = _user_to_scim(account, "https://control.farna.dev")
    assert not result["name"]["givenName"]
    assert not result["name"]["familyName"]
    assert result["name"]["formatted"] is None


def test_user_to_scim_none_timestamps():
    """None timestamps → empty strings."""
    account = _make_account(created_at=None, updated_at=None)
    result = _user_to_scim(account, "https://control.farna.dev")
    assert not result["meta"]["created"]
    assert not result["meta"]["lastModified"]


def test_user_to_scim_multi_word_name():
    """Display name with multiple parts → familyName is everything after first."""
    account = _make_account(display_name="Mary Jane Watson")
    result = _user_to_scim(account, "https://control.farna.dev")
    assert result["name"]["givenName"] == "Mary"
    assert result["name"]["familyName"] == "Jane Watson"


def test_user_to_scim_emails():
    """Emails list is properly constructed."""
    account = _make_account(email="bob@example.com")
    result = _user_to_scim(account, "https://control.farna.dev")
    assert len(result["emails"]) == 1
    assert result["emails"][0]["value"] == "bob@example.com"
    assert result["emails"][0]["type"] == "work"
    assert result["emails"][0]["primary"] is True


def test_user_to_scim_location():
    """Location URL is properly constructed."""
    account = _make_account()
    result = _user_to_scim(account, "https://control.farna.dev")
    assert f"/scim/v2/Users/{account.id}" in result["meta"]["location"]


def test_user_to_scim_external_id():
    """externalId matches the account id."""
    account = _make_account()
    result = _user_to_scim(account, "https://control.farna.dev")
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
    result = _group_to_scim(group, members, "https://control.farna.dev")
    assert result["schemas"] == ["urn:ietf:params:scim:schemas:core:2.0:Group"]
    assert result["displayName"] == "Engineering"
    assert result["members"] == members


def test_group_to_scim_empty_members():
    """Empty members list."""
    group = _make_group()
    result = _group_to_scim(group, [], "https://control.farna.dev")
    assert not result["members"]


def test_group_to_scim_none_timestamps():
    """None timestamps → empty strings."""
    group = _make_group(created_at=None, updated_at=None)
    result = _group_to_scim(group, [], "https://control.farna.dev")
    assert not result["meta"]["created"]
    assert not result["meta"]["lastModified"]


def test_group_to_scim_location():
    """Location URL is properly constructed."""
    group = _make_group()
    result = _group_to_scim(group, [], "https://control.farna.dev")
    assert f"/scim/v2/Groups/{group.id}" in result["meta"]["location"]


def test_group_to_scim_external_id():
    """externalId matches the group id."""
    group = _make_group()
    result = _group_to_scim(group, [], "https://control.farna.dev")
    assert result["externalId"] == str(group.id)


# ---------------------------------------------------------------------------
# _get_base_url
# ---------------------------------------------------------------------------


def test_get_base_url_strips_trailing_slash():
    """Trailing slash is stripped."""
    settings = MagicMock()
    settings.modulo_public_url = "https://control.farna.dev/"
    assert _get_base_url(settings) == "https://control.farna.dev"


def test_get_base_url_no_trailing_slash():
    """No trailing slash → returned as-is."""
    settings = MagicMock()
    settings.modulo_public_url = "https://control.farna.dev"
    assert _get_base_url(settings) == "https://control.farna.dev"


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


# ---------------------------------------------------------------------------
# Internal user/group branch helpers
# ---------------------------------------------------------------------------


def test_require_human_scim_user_rejects_missing_account() -> None:
    user_id = uuid.uuid4()
    with pytest.raises(HTTPException) as exc_info:
        scim_routes._require_human_scim_user(None, user_id)
    assert exc_info.value.status_code == 404


def test_require_human_scim_user_rejects_service_account() -> None:
    account = MagicMock()
    account.id = uuid.uuid4()
    with (
        patch("farna.api.routes.scim.is_service_account", return_value=True),
        pytest.raises(HTTPException) as exc_info,
    ):
        scim_routes._require_human_scim_user(account, account.id)
    assert exc_info.value.status_code == 404


def test_require_human_scim_user_accepts_human_account() -> None:
    account = MagicMock()
    account.id = uuid.uuid4()
    with patch("farna.api.routes.scim.is_service_account", return_value=False):
        assert scim_routes._require_human_scim_user(account, account.id) is account


def test_resolve_scim_admin_caller_returns_id_and_none() -> None:
    session = AsyncMock()
    row = MagicMock()
    row.__getitem__.return_value = uuid.uuid4()
    result = MagicMock()
    result.first.return_value = row
    session.execute = AsyncMock(return_value=result)
    org_id = uuid.uuid4()
    caller = asyncio.run(scim_routes._resolve_scim_admin_caller(session, org_id))
    assert caller == row[0]

    result.first.return_value = None
    caller = asyncio.run(scim_routes._resolve_scim_admin_caller(session, org_id))
    assert caller is None


def test_iter_member_uuids_handles_dict_and_invalid_values() -> None:
    member_id = uuid.uuid4()
    values = {"value": str(member_id)}
    assert scim_routes._iter_member_uuids(values) == [member_id]
    assert not scim_routes._iter_member_uuids({"value": "bad"})
    assert not scim_routes._iter_member_uuids([{"other": "value"}, {"value": "bad"}])
    assert not scim_routes._iter_member_uuids(None)


def test_apply_user_patch_helpers_cover_all_operations() -> None:
    account = MagicMock()
    account.email = "old@example.com"
    account.display_name = "Old"
    replace = scim_routes.ScimPatchOperation(
        op="replace",
        value={"userName": "new@example.com", "active": True, "name": {"givenName": "New", "familyName": "Name"}},
    )
    assert not scim_routes._apply_user_replace_op(account, replace)
    assert account.email == "new@example.com"
    assert account.display_name == "New Name"

    replace_active = scim_routes.ScimPatchOperation(op="replace", path="active", value=False)
    assert scim_routes._apply_user_replace_op(account, replace_active)
    assert not account.active
    assert scim_routes._apply_user_remove_op(account, scim_routes.ScimPatchOperation(op="remove", path="active"))
    assert scim_routes._apply_user_add_op(
        account,
        scim_routes.ScimPatchOperation(op="add", value={"userName": "again@example.com", "active": False}),
    )
    assert account.email == "again@example.com"


def test_apply_user_patch_ops_rejects_unknown_operation() -> None:
    account = MagicMock()
    request = scim_routes.ScimPatchOperation(op="replace")
    with pytest.raises(HTTPException) as exc_info:
        scim_routes._apply_user_patch_ops(account, [scim_routes.ScimPatchOperation(op="invalid")])
    assert exc_info.value.status_code == 400
    assert request.op == "replace"


def test_group_member_remove_helpers_cover_paths() -> None:
    session = AsyncMock()
    group = MagicMock(id=uuid.uuid4())
    member_id = uuid.uuid4()
    with patch("farna.api.routes.scim.scim_remove_group_member", new_callable=AsyncMock) as remove:
        asyncio.run(scim_routes._remove_member_by_path(session, group, f'members[value eq "{member_id}"]'))
        asyncio.run(scim_routes._remove_member_by_path(session, group, 'members[value eq "bad"]'))
        asyncio.run(scim_routes._remove_member_by_path(session, group, "other"))
        asyncio.run(scim_routes._remove_members_from_value(session, group, {"value": str(member_id)}))
        asyncio.run(scim_routes._remove_members_from_value(session, group, [{"value": str(member_id)}]))
        asyncio.run(scim_routes._remove_members_from_value(session, group, [{"value": "bad"}]))
    assert remove.await_count == 3


def test_apply_remove_group_op_covers_empty_and_value() -> None:
    session = AsyncMock()
    group = MagicMock(id=uuid.uuid4())
    with patch("farna.api.routes.scim._remove_members_from_value", new_callable=AsyncMock) as remove:
        asyncio.run(scim_routes._apply_remove_group_op(session, group, MagicMock(path=None, value=None)))
        asyncio.run(scim_routes._apply_remove_group_op(session, group, MagicMock(path=None, value={"value": "x"})))
    remove.assert_awaited_once()
