"""FAR-1385 slice 1 — human-path exclusion at the account-load boundaries.

Direct tests of the shared per-module guards that refuse a service principal on
a human-only surface. These are the paths that load an account by primary key,
where the NULL-email structural miss does not apply, so the explicit guard is
what makes the exclusion real.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from modulo.api.routes.me import _require_human_account
from modulo.api.routes.scim import _require_human_scim_user


def _account(kind: str | None) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), kind=kind, email="x@example.com" if kind != "service" else None)


class TestMeHumanGuard:
    def test_missing_account_is_404(self) -> None:
        with pytest.raises(HTTPException) as exc:
            _require_human_account(None)
        assert exc.value.status_code == 404

    def test_service_account_is_404(self) -> None:
        with pytest.raises(HTTPException) as exc:
            _require_human_account(_account("service"))
        assert exc.value.status_code == 404
        assert exc.value.detail == "Account not found"

    def test_human_account_passes(self) -> None:
        account = _account("human")
        assert _require_human_account(account) is account


class TestScimHumanGuard:
    def test_missing_user_is_404(self) -> None:
        with pytest.raises(HTTPException) as exc:
            _require_human_scim_user(None, uuid.uuid4())
        assert exc.value.status_code == 404

    def test_service_user_is_404(self) -> None:
        user_id = uuid.uuid4()
        with pytest.raises(HTTPException) as exc:
            _require_human_scim_user(_account("service"), user_id)
        assert exc.value.status_code == 404
        # SCIM error envelope — the client cannot tell service from unknown.
        assert str(user_id) in str(exc.value.detail)

    def test_human_user_passes(self) -> None:
        account = _account("human")
        assert _require_human_scim_user(account, account.id) is account
