"""Step definitions for auth/change_password.feature."""

import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pytest_bdd import parsers, scenarios, then, when

try:
    scenarios("../features/auth/change_password.feature")
except (FileNotFoundError, OSError):
    pass

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


@pytest.fixture
def ctx() -> dict[str, Any]:
    return {}


def _store_response(request: Any, ctx: dict[str, Any], resp: Any) -> None:
    request.node._resp = resp
    request.node.response = resp
    ctx["response"] = resp


@then(parsers.parse('the error mentions "{text}"'))
def step_error_mentions(text: str, request: Any) -> None:
    body = request.node.response.json()
    detail = body.get("detail", str(body)).lower()
    assert text.lower() in detail, f"Expected error to mention {text!r}, got {body!r}"


@when(
    parsers.parse('I change my password from "{old_pw}" to "{new_pw}"'),
)
def step_change_password(
    client: Any,
    old_pw: str,
    new_pw: str,
    request: Any,
    ctx: dict[str, Any],
) -> None:
    from modulo.auth.passwords import hash_password

    with (
        patch("modulo.api.routes.me.get_account_by_id") as mock_get_user,
        patch("modulo.api.routes.me.list_families_for_account") as mock_list,
        patch("modulo.api.routes.me.blacklist_family") as mock_blacklist,
    ):
        mock_user = MagicMock()
        mock_user.password_hash = hash_password("correct-horse-battery")
        mock_get_user.return_value = mock_user

        mock_family = MagicMock()
        mock_family.family_id = uuid.uuid4()
        mock_list.return_value = [mock_family]
        mock_blacklist.return_value = True

        resp = client.put(
            "/api/v1/me/password",
            json={
                "current_password": old_pw,
                "new_password": new_pw,
            },
        )
        _store_response(request, ctx, resp)
        ctx["_mock_blacklist"] = mock_blacklist


@when("I attempt to change my password without a local password set")
def step_change_password_no_local(
    client: Any,
    request: Any,
    ctx: dict[str, Any],
) -> None:
    with (
        patch("modulo.api.routes.me.get_account_by_id") as mock_get_user,
        patch("modulo.api.routes.me.list_families_for_account") as mock_list,
        patch("modulo.api.routes.me.blacklist_family") as mock_blacklist,
    ):
        mock_user = MagicMock()
        mock_user.password_hash = None
        mock_get_user.return_value = mock_user

        mock_list.return_value = []
        mock_blacklist.return_value = True

        resp = client.put(
            "/api/v1/me/password",
            json={
                "current_password": "anything",
                "new_password": "new-strong-password-42",
            },
        )
        _store_response(request, ctx, resp)


@then(parsers.parse('the response says "{message}"'))
def step_response_message(request: Any, message: str) -> None:
    body = request.node.response.json()
    assert body.get("detail") == message, f"Expected detail {message!r}, got {body!r}"


@then("all token families for my user are blacklisted")
def step_all_families_blacklisted(ctx: dict[str, Any]) -> None:
    mock_blacklist = ctx.get("_mock_blacklist")
    assert mock_blacklist is not None, "No blacklist_family mock found — was the When step run?"
    mock_blacklist.assert_called()
