"""Unit tests for the bootstrap MODULO_USERS seed (modulo.api.main).

FAR-584: emails are case-insensitive now, so a MODULO_USERS entry like
``Admin:<pw>`` resolves to the admin bootstrap account where it previously
seeded a plain runner. These tests lock the loud-warning contract: a role
change caused by case normalisation must never be silent, and the password
part of an entry must never appear in any log record.
"""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.api.main import _BOOTSTRAP_ADMIN_EMAILS, _rehash_existing_user, _seed_modulo_user


def _make_mock_session() -> MagicMock:
    session = MagicMock()
    session.execute = AsyncMock()
    session.flush = AsyncMock()
    return session


def _bootstrap_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if str(record.msg).startswith("bootstrap.")]


class TestSeedModuloUserCaseNormalization:
    async def test_case_variant_admin_entry_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        session = _make_mock_session()
        session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

        with caplog.at_level(logging.WARNING, logger="modulo.api.main"):
            await _seed_modulo_user(session, MagicMock(id="org-1"), "Admin:$2b$12$fakehash")

        warnings = [
            record for record in _bootstrap_records(caplog) if record.msg == "bootstrap.role_case_normalization"
        ]
        assert warnings
        assert warnings[0].entry_email == "Admin"
        assert warnings[0].normalized_email == "admin"

    async def test_case_variant_admin_entry_seeds_admin_role(self, caplog: pytest.LogCaptureFixture) -> None:
        session = _make_mock_session()
        session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

        with caplog.at_level(logging.WARNING, logger="modulo.api.main"):
            await _seed_modulo_user(session, MagicMock(id="org-1"), "Admin:$2b$12$fakehash")

        added = [call.args[0] for call in session.add.call_args_list]
        roles = [membership.role for membership in added if type(membership).__name__ == "OrgMembership"]
        assert roles == ["admin"]

    async def test_exact_admin_entry_does_not_warn(self, caplog: pytest.LogCaptureFixture) -> None:
        session = _make_mock_session()
        session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

        with caplog.at_level(logging.WARNING, logger="modulo.api.main"):
            await _seed_modulo_user(session, MagicMock(id="org-1"), "admin:$2b$12$fakehash")

        assert not _bootstrap_records(caplog)

    async def test_case_variant_non_admin_entry_does_not_warn(self, caplog: pytest.LogCaptureFixture) -> None:
        session = _make_mock_session()
        session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

        with caplog.at_level(logging.WARNING, logger="modulo.api.main"):
            await _seed_modulo_user(session, MagicMock(id="org-1"), "Jane.Doe@Example.COM:$2b$12$fakehash")

        assert not _bootstrap_records(caplog)

    async def test_warning_never_contains_the_password(self, caplog: pytest.LogCaptureFixture) -> None:
        secret = "$2b$12$supersecretvalue"
        session = _make_mock_session()
        session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

        with caplog.at_level(logging.WARNING, logger="modulo.api.main"):
            await _seed_modulo_user(session, MagicMock(id="org-1"), f"Admin:{secret}")

        assert not any(secret in record.getMessage() for record in caplog.records)
        assert not any(secret in str(record.__dict__) for record in caplog.records)


class TestRehashExistingUserRolePromotion:
    async def test_role_promotion_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        session = _make_mock_session()
        membership = MagicMock(role="runner")
        session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=membership))

        with caplog.at_level(logging.WARNING, logger="modulo.api.main"):
            await _rehash_existing_user(session, MagicMock(id="org-1"), MagicMock(id="acct-1"), "admin", "$2b$12$x")

        assert membership.role == "admin"
        warnings = [record for record in _bootstrap_records(caplog) if record.msg == "bootstrap.role_changed_to_admin"]
        assert warnings
        assert warnings[0].previous_role == "runner"

    async def test_already_admin_does_not_warn(self, caplog: pytest.LogCaptureFixture) -> None:
        session = _make_mock_session()
        membership = MagicMock(role="admin")
        session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=membership))

        with caplog.at_level(logging.WARNING, logger="modulo.api.main"):
            await _rehash_existing_user(session, MagicMock(id="org-1"), MagicMock(id="acct-1"), "admin", "$2b$12$x")

        assert membership.role == "admin"
        assert not _bootstrap_records(caplog)


class TestBootstrapAdminEmailsConstant:
    def test_special_case_addresses_are_canonical(self) -> None:
        from modulo.util.emails import normalize_email

        for address in _BOOTSTRAP_ADMIN_EMAILS:
            assert normalize_email(address) == address
