"""Focused unit tests for isolated ``modulo.auth.sso`` helper functions.

These target low-level helpers (RLS scoping, provider lookups, group-mapping
application, JSON-object validation) to keep ``modulo.auth`` package coverage
above the 90% gate without touching the network-bound OIDC/SAML flows.
"""

import base64
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from defusedxml import ElementTree

from modulo.auth import sso as sso_mod
from modulo.auth.sso import (
    _lookup_provider_by_client_id,
    _lookup_provider_by_entity_id,
    _require_json_object,
    _set_default_rls_org,
    apply_group_mappings,
)


def _mock_session(scalar_value=None):
    """AsyncSession mock whose execute() returns a result with scalar_one_or_none()."""
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = scalar_value
    session.execute.return_value = result
    return session


# ---------------------------------------------------------------------------
# _set_default_rls_org
# ---------------------------------------------------------------------------


async def test_set_default_rls_org_no_org():
    session = _mock_session(scalar_value=None)
    with patch("modulo.auth.sso.set_rls_org") as set_rls:
        await _set_default_rls_org(session)
    session.execute.assert_awaited_once()
    set_rls.assert_not_awaited()


async def test_set_default_rls_org_with_org():
    org = SimpleNamespace(id=uuid.uuid4())
    session = _mock_session(scalar_value=org)
    with patch("modulo.auth.sso.set_rls_org") as set_rls:
        await _set_default_rls_org(session)
    set_rls.assert_awaited_once_with(session, org.id)


# ---------------------------------------------------------------------------
# provider lookups
# ---------------------------------------------------------------------------


async def test_lookup_provider_by_client_id():
    provider = SimpleNamespace(id=uuid.uuid4())
    session = _mock_session(scalar_value=provider)
    org_id = uuid.uuid4()
    result = await _lookup_provider_by_client_id(session, "client-id", org_id)
    assert result is provider
    session.execute.assert_awaited_once()


async def test_lookup_provider_by_entity_id():
    provider = SimpleNamespace(id=uuid.uuid4())
    session = _mock_session(scalar_value=provider)
    org_id = uuid.uuid4()
    result = await _lookup_provider_by_entity_id(session, "entity-id", org_id)
    assert result is provider
    session.execute.assert_awaited_once()


# ---------------------------------------------------------------------------
# _require_json_object
# ---------------------------------------------------------------------------


def test_require_json_object_valid():
    assert _require_json_object({"a": 1, "b": "x"}, "ctx") == {"a": 1, "b": "x"}


def test_require_json_object_not_dict():
    with pytest.raises(ValueError, match="must be a JSON object"):
        _require_json_object(["not", "dict"], "ctx")


def test_require_json_object_non_string_key():
    with pytest.raises(ValueError, match="non-string key"):
        _require_json_object({1: "v"}, "ctx")


# ---------------------------------------------------------------------------
# apply_group_mappings
# ---------------------------------------------------------------------------


def _account():
    return SimpleNamespace(id=uuid.uuid4(), email="user@example.com")


async def test_apply_group_mappings_skips_non_dict():
    session = AsyncMock()
    account = _account()
    org_id = uuid.uuid4()
    with (
        patch("modulo.auth.sso.get_membership_by_team_and_account") as get_mem,
        patch("modulo.auth.sso.add_team_member") as add_member,
        patch("modulo.auth.sso.update_member_role") as upd,
    ):
        await apply_group_mappings(session, account, org_id, ["team-a"], [{"not": "a dict"}])
    get_mem.assert_not_awaited()
    add_member.assert_not_awaited()
    upd.assert_not_awaited()


async def test_apply_group_mappings_idp_group_not_present():
    session = AsyncMock()
    account = _account()
    org_id = uuid.uuid4()
    with (
        patch("modulo.auth.sso.get_membership_by_team_and_account") as get_mem,
        patch("modulo.auth.sso.add_team_member") as add_member,
    ):
        await apply_group_mappings(
            session,
            account,
            org_id,
            ["team-a"],
            [{"idp_group": "team-b", "team_id": str(uuid.uuid4())}],
        )
    get_mem.assert_not_awaited()
    add_member.assert_not_awaited()


async def test_apply_group_mappings_invalid_team_id():
    session = AsyncMock()
    account = _account()
    org_id = uuid.uuid4()
    with (
        patch("modulo.auth.sso.get_membership_by_team_and_account") as get_mem,
        patch("modulo.auth.sso.add_team_member") as add_member,
    ):
        await apply_group_mappings(
            session,
            account,
            org_id,
            ["team-a"],
            [{"idp_group": "team-a", "team_id": "not-a-uuid"}],
        )
    get_mem.assert_not_awaited()
    add_member.assert_not_awaited()


async def test_apply_group_mappings_adds_new_member():
    session = AsyncMock()
    account = _account()
    org_id = uuid.uuid4()
    team_id = uuid.uuid4()
    with (
        patch("modulo.auth.sso.get_membership_by_team_and_account", new=AsyncMock(return_value=None)) as get_mem,
        patch("modulo.auth.sso.add_team_member", new=AsyncMock()) as add_member,
    ):
        await apply_group_mappings(
            session,
            account,
            org_id,
            ["team-a"],
            [{"idp_group": "team-a", "team_id": str(team_id), "team_role": "operator"}],
        )
    get_mem.assert_awaited_once()
    add_member.assert_awaited_once()
    _, kwargs = add_member.call_args
    assert kwargs["team_id"] == team_id
    assert kwargs["role"] == "operator"


async def test_apply_group_mappings_updates_existing_role():
    session = AsyncMock()
    account = _account()
    org_id = uuid.uuid4()
    team_id = uuid.uuid4()
    existing = SimpleNamespace(id=uuid.uuid4(), role="viewer")
    with (
        patch("modulo.auth.sso.get_membership_by_team_and_account", new=AsyncMock(return_value=existing)) as get_mem,
        patch("modulo.auth.sso.update_member_role", new=AsyncMock()) as upd,
        patch("modulo.auth.sso.add_team_member", new=AsyncMock()) as add_member,
    ):
        await apply_group_mappings(
            session,
            account,
            org_id,
            ["team-a"],
            [{"idp_group": "team-a", "team_id": str(team_id), "team_role": "operator"}],
        )
    get_mem.assert_awaited_once()
    add_member.assert_not_awaited()
    upd.assert_awaited_once()
    assert upd.call_args.args[1] == existing.id


# ---------------------------------------------------------------------------
# _decode_id_token_claims (no signature verification)
# ---------------------------------------------------------------------------


def _b64url(obj: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(obj).encode()).decode()
    return f"header.{payload}.signature"


def test_decode_id_token_claims_valid():
    claims = sso_mod._decode_id_token_claims(_b64url({"sub": "abc", "email": "x@y.z"}))
    assert claims["email"] == "x@y.z"


def test_decode_id_token_claims_wrong_parts():
    assert not sso_mod._decode_id_token_claims("not.a.jwt.token")


def test_decode_id_token_claims_bad_base64():
    # '!!' is not valid base64 urlsafe -> JSON decode fails -> {}
    assert not sso_mod._decode_id_token_claims("a.!!.c")


# ---------------------------------------------------------------------------
# _decode_saml_response
# ---------------------------------------------------------------------------


def test_decode_saml_response_valid():
    raw = "<saml>hi</saml>"
    encoded = base64.b64encode(raw.encode()).decode()
    assert sso_mod._decode_saml_response(encoded) == raw.encode()


def test_decode_saml_response_invalid():
    import pytest as _pytest

    with _pytest.raises(ValueError, match="Invalid base64 SAML response"):
        sso_mod._decode_saml_response("!!! not base64 !!!")


# ---------------------------------------------------------------------------
# _validate_saml_response_destination
# ---------------------------------------------------------------------------


def test_validate_saml_destination_invalid_base64():
    # _decode_saml_response raises -> ValueError swallowed -> returns None
    assert sso_mod._validate_saml_response_destination("!!!", "https://acs") is None


def test_validate_saml_destination_parse_error():
    with (
        patch("modulo.auth.sso._decode_saml_response", return_value=b"<bad"),
        patch("modulo.auth.sso.ElementTree.fromstring", side_effect=ElementTree.ParseError("boom")),
    ):
        assert sso_mod._validate_saml_response_destination("abc", "https://acs") is None


def test_validate_saml_destination_missing_attr():
    root = MagicMock()
    root.get.return_value = None
    with (
        patch("modulo.auth.sso._decode_saml_response", return_value=b"<xml/>"),
        patch("modulo.auth.sso.ElementTree.fromstring", return_value=root),
    ):
        assert sso_mod._validate_saml_response_destination("abc", "https://acs") is None


def test_validate_saml_destination_mismatch():
    import pytest as _pytest

    root = MagicMock()
    root.get.return_value = "https://evil"
    with (
        patch("modulo.auth.sso._decode_saml_response", return_value=b"<xml/>"),
        patch("modulo.auth.sso.ElementTree.fromstring", return_value=root),
        _pytest.raises(ValueError, match="Destination does not match"),
    ):
        sso_mod._validate_saml_response_destination("abc", "https://acs")
