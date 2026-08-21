"""Unit tests for account_deps — account-level routing dependency."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from modulo.api.account_deps import AccountContext


class TestAccountContext:
    def test_init(self) -> None:
        account_id = uuid.uuid4()
        user_id = uuid.uuid4()
        account = SimpleNamespace(id=account_id, name="test", status="active")
        ctx = AccountContext(
            account_id=account_id,
            user_id=user_id,
            role="owner",
            account=account,
        )
        assert ctx.account_id == account_id
        assert ctx.user_id == user_id
        assert ctx.role == "owner"
        assert ctx.account == account
