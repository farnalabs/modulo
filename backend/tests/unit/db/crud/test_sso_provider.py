"""Unit tests for SSO provider CRUD (mocked session, no DB)."""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PROVIDER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_ACTOR_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_FERNET_KEY = "fernet-key-material"


@pytest.fixture
def mock_session() -> AsyncMock:
    return AsyncMock(spec=AsyncSession)


@pytest.fixture
def system_session() -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _make_provider(**overrides: object) -> MagicMock:
    provider = MagicMock()
    provider.id = _PROVIDER_ID
    provider.organisation_id = _ORG_ID
    provider.name = overrides.get("name", "Acme SSO")
    provider.provider_type = overrides.get("provider_type", "oidc")
    provider.enabled = overrides.get("enabled", True)
    provider.scopes = overrides.get("scopes")
    provider.group_mappings = overrides.get("group_mappings")
    return provider


def _patch_audit() -> object:
    return patch(
        "modulo.db.crud.sso_provider.append_audit_event",
        AsyncMock(),
    )


class TestSlugify:
    def test_slugify_normalises_and_trims(self) -> None:
        from modulo.db.crud.sso_provider import _slugify_provider_id

        assert _slugify_provider_id("Acme Corp SSO!!") == "acme-corp-sso"

    def test_slugify_caps_at_58_chars(self) -> None:
        from modulo.db.crud.sso_provider import _slugify_provider_id

        assert len(_slugify_provider_id("x" * 100)) == 58

    def test_slugify_all_symbols_falls_back_to_sso(self) -> None:
        from modulo.db.crud.sso_provider import _slugify_provider_id

        assert _slugify_provider_id("###") == "sso"


class TestUniqueProviderId:
    async def test_appends_counter_on_collision(self, mock_session: AsyncMock) -> None:
        existing = MagicMock()
        existing.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=["acme", "acme-2"])))
        mock_session.execute = AsyncMock(return_value=existing)

        result = await self._call(mock_session, "acme")
        assert result == "acme-3"

    async def test_first_choice_when_no_collision(self, mock_session: AsyncMock) -> None:
        existing = MagicMock()
        existing.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        mock_session.execute = AsyncMock(return_value=existing)

        result = await self._call(mock_session, "acme")
        assert result == "acme"

    async def test_skips_none_provider_ids(self, mock_session: AsyncMock) -> None:
        existing = MagicMock()
        existing.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[None, "base"])))
        mock_session.execute = AsyncMock(return_value=existing)

        assert await self._call(mock_session, "base") == "base-2"

    @staticmethod
    async def _call(session: AsyncMock, base: str) -> str:
        from modulo.db.crud.sso_provider import _unique_provider_id

        return await _unique_provider_id(session, base, _ORG_ID)


class TestReadPaths:
    async def test_list_providers_scopes_to_org(self, mock_session: AsyncMock) -> None:
        providers = [_make_provider()]
        result = MagicMock()
        scalars = MagicMock()
        scalars.all = MagicMock(return_value=providers)
        result.scalars = MagicMock(return_value=scalars)
        mock_session.execute = AsyncMock(return_value=result)
        from modulo.db.crud.sso_provider import list_providers

        assert await list_providers(mock_session, org_id=_ORG_ID) == providers

    async def test_get_provider_with_and_without_org_scope(self, mock_session: AsyncMock) -> None:
        provider = _make_provider()
        found = MagicMock(scalar_one_or_none=MagicMock(return_value=provider))
        missing = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        mock_session.execute = AsyncMock(side_effect=[found, missing])
        from modulo.db.crud.sso_provider import get_provider

        assert await get_provider(mock_session, _PROVIDER_ID, org_id=_ORG_ID) is provider
        assert await get_provider(mock_session, _PROVIDER_ID) is None

    async def test_get_provider_by_provider_id_slug(self, mock_session: AsyncMock) -> None:
        provider = _make_provider()
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=provider)))
        from modulo.db.crud.sso_provider import get_provider_by_provider_id

        assert await get_provider_by_provider_id(mock_session, "acme") is provider

    async def test_get_enabled_saml_provider(self, mock_session: AsyncMock) -> None:
        provider = _make_provider(provider_type="saml")
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=provider)))
        from modulo.db.crud.sso_provider import get_enabled_saml_provider

        assert await get_enabled_saml_provider(mock_session) is provider

    async def test_list_enabled_oidc_providers(self, mock_session: AsyncMock) -> None:
        providers = [_make_provider(provider_type="oidc")]
        result = MagicMock()
        scalars = MagicMock()
        scalars.all = MagicMock(return_value=providers)
        result.scalars = MagicMock(return_value=scalars)
        mock_session.execute = AsyncMock(return_value=result)
        from modulo.db.crud.sso_provider import list_enabled_oidc_providers

        assert await list_enabled_oidc_providers(mock_session) == providers


class TestCreateProvider:
    async def test_duplicate_name_raises_value_error(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=_make_provider()))
        )
        from modulo.db.crud.sso_provider import create_provider

        with pytest.raises(ValueError, match="already exists"):
            await create_provider(
                mock_session,
                provider_type="oidc",
                name="Acme SSO",
                fernet_key=_FERNET_KEY,
                org_id=_ORG_ID,
            )

    async def test_creates_provider_with_slug_and_audit(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
        provider = _make_provider()
        del provider
        with (
            patch("modulo.db.crud.sso_provider.encrypt_stored_secret", return_value=b"enc") as encrypt,
            _patch_audit() as audit,
        ):
            from modulo.db.crud.sso_provider import create_provider

            result = await create_provider(
                mock_session,
                provider_type="oidc",
                name="Acme SSO",
                client_id="cid",
                client_secret="sekrit",
                discovery_url="https://discovery",
                scopes=["openid", "email"],
                fernet_key=_FERNET_KEY,
                org_id=_ORG_ID,
                actor_user_id=_ACTOR_ID,
            )
        assert result.provider_id == "acme-sso"
        assert result.client_secret == b"enc"
        assert json.loads(result.scopes) == ["openid", "email"]
        assert result.organisation_id == _ORG_ID
        assert result.name == "Acme SSO"
        encrypt.assert_called_once_with("sekrit", _FERNET_KEY)
        audit.assert_awaited_once()
        mock_session.add.assert_called_once()

    async def test_scopes_none_stays_none(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
        with _patch_audit():
            from modulo.db.crud.sso_provider import create_provider

            result = await create_provider(
                mock_session,
                provider_type="oidc",
                name="Bare SSO",
                fernet_key=_FERNET_KEY,
                org_id=_ORG_ID,
            )
        assert result.scopes is None
        assert result.client_secret is None

    async def test_explicit_provider_id_overrides_slug(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
        with _patch_audit():
            from modulo.db.crud.sso_provider import create_provider

            result = await create_provider(
                mock_session,
                provider_type="oidc",
                name="Acme SSO",
                provider_id="Custom ID",
                fernet_key=_FERNET_KEY,
                org_id=_ORG_ID,
            )
        assert result.provider_id == "custom-id"

    def _system_scan_patches(self, modulo_system_database_url: str) -> object:
        return patch(
            "modulo.db.crud.sso_provider.get_settings",
            MagicMock(return_value=MagicMock(modulo_system_database_url=modulo_system_database_url)),
        )

    async def test_system_session_scan_when_url_configured(
        self, mock_session: AsyncMock, system_session: AsyncMock
    ) -> None:
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
        scalars_all = MagicMock(all=MagicMock(return_value=[]))
        system_session.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=scalars_all)))
        with self._system_scan_patches("postgresql://system"), _patch_audit():
            from modulo.db.crud.sso_provider import create_provider

            await create_provider(
                mock_session,
                provider_type="oidc",
                name="Acme SSO",
                fernet_key=_FERNET_KEY,
                org_id=_ORG_ID,
                system_session=system_session,
            )
        system_session.begin.assert_called_once()
        system_session.execute.assert_awaited_once()

    async def test_system_session_falls_back_when_url_unset(
        self, mock_session: AsyncMock, system_session: AsyncMock
    ) -> None:
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
        with self._system_scan_patches(""), _patch_audit():
            from modulo.db.crud.sso_provider import create_provider

            await create_provider(
                mock_session,
                provider_type="oidc",
                name="Acme SSO",
                fernet_key=_FERNET_KEY,
                org_id=_ORG_ID,
                system_session=system_session,
            )
        system_session.execute.assert_not_awaited()

    async def test_system_fallback_when_settings_raise(
        self, mock_session: AsyncMock, system_session: AsyncMock
    ) -> None:
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
        with (
            patch(
                "modulo.db.crud.sso_provider.get_settings",
                MagicMock(side_effect=RuntimeError("settings unavailable")),
            ),
            _patch_audit(),
        ):
            from modulo.db.crud.sso_provider import create_provider

            await create_provider(
                mock_session,
                provider_type="oidc",
                name="Acme SSO",
                fernet_key=_FERNET_KEY,
                org_id=_ORG_ID,
                system_session=system_session,
            )
        system_session.execute.assert_not_awaited()

    async def test_audit_failure_is_swallowed(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
        with _patch_audit() as audit:
            from modulo.db.crud.sso_provider import create_provider

            audit.side_effect = Exception("audit down")
            result = await create_provider(
                mock_session,
                provider_type="oidc",
                name="Acme SSO",
                fernet_key=_FERNET_KEY,
                org_id=_ORG_ID,
            )
        assert result.provider_id == "acme-sso"


class TestUpdateProvider:
    async def test_returns_none_when_missing(self, mock_session: AsyncMock) -> None:
        with patch("modulo.db.crud.sso_provider.get_provider", AsyncMock(return_value=None)):
            from modulo.db.crud.sso_provider import update_provider

            assert (
                await update_provider(mock_session, _PROVIDER_ID, org_id=_ORG_ID, fernet_key=_FERNET_KEY, name="x")
                is None
            )

    async def test_client_secret_encrypted_and_removed_from_updates(self, mock_session: AsyncMock) -> None:
        provider = _make_provider()
        no_conflict = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        mock_session.execute = AsyncMock(return_value=no_conflict)
        with (
            patch("modulo.db.crud.sso_provider.get_provider", AsyncMock(return_value=provider)),
            patch("modulo.db.crud.sso_provider.apply_updates") as apply_updates,
            patch("modulo.db.crud.sso_provider.encrypt_stored_secret", return_value=b"enc2") as encrypt,
            _patch_audit() as audit,
        ):
            from modulo.db.crud.sso_provider import update_provider

            result = await update_provider(
                mock_session,
                _PROVIDER_ID,
                org_id=_ORG_ID,
                fernet_key=_FERNET_KEY,
                client_secret="new-secret",
                name="Renamed",
            )
        encrypt.assert_called_once_with("new-secret", _FERNET_KEY)
        assert provider.client_secret == b"enc2"
        filtered = apply_updates.call_args.args[1]
        assert "client_secret" not in filtered
        audit.assert_awaited_once()
        assert result is provider

    async def test_non_string_client_secret_raises_type_error(self, mock_session: AsyncMock) -> None:
        provider = _make_provider()
        with patch("modulo.db.crud.sso_provider.get_provider", AsyncMock(return_value=provider)):
            from modulo.db.crud.sso_provider import update_provider

            with pytest.raises(TypeError, match="client_secret must be text"):
                await update_provider(
                    mock_session,
                    _PROVIDER_ID,
                    org_id=_ORG_ID,
                    fernet_key=_FERNET_KEY,
                    client_secret=12345,
                )

    async def test_name_conflict_raises(self, mock_session: AsyncMock) -> None:
        provider = _make_provider()
        conflicting = MagicMock()
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=conflicting)))
        with patch("modulo.db.crud.sso_provider.get_provider", AsyncMock(return_value=provider)):
            from modulo.db.crud.sso_provider import update_provider

            with pytest.raises(ValueError, match="already exists"):
                await update_provider(
                    mock_session,
                    _PROVIDER_ID,
                    org_id=_ORG_ID,
                    fernet_key=_FERNET_KEY,
                    name="Other SSO",
                )

    async def test_scopes_list_serialised(self, mock_session: AsyncMock) -> None:
        provider = _make_provider()
        with (
            patch("modulo.db.crud.sso_provider.get_provider", AsyncMock(return_value=provider)),
            patch("modulo.db.crud.sso_provider.apply_updates") as apply_updates,
            _patch_audit(),
        ):
            from modulo.db.crud.sso_provider import update_provider

            await update_provider(
                mock_session,
                _PROVIDER_ID,
                org_id=_ORG_ID,
                fernet_key=_FERNET_KEY,
                scopes=["openid"],
            )
        updates = apply_updates.call_args.args[1]
        assert json.loads(updates["scopes"]) == ["openid"]

    async def test_uneditable_fields_filtered_out(self, mock_session: AsyncMock) -> None:
        provider = _make_provider()
        with (
            patch("modulo.db.crud.sso_provider.get_provider", AsyncMock(return_value=provider)),
            patch("modulo.db.crud.sso_provider.apply_updates") as apply_updates,
            _patch_audit(),
        ):
            from modulo.db.crud.sso_provider import update_provider

            await update_provider(
                mock_session,
                _PROVIDER_ID,
                org_id=_ORG_ID,
                fernet_key=_FERNET_KEY,
                enabled=False,
                organisation_id=_PROVIDER_ID,
            )
        updates = apply_updates.call_args.args[1]
        assert updates["enabled"] is False
        assert "organisation_id" not in updates

    async def test_audit_failure_swallowed(self, mock_session: AsyncMock) -> None:
        provider = _make_provider()
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
        with (
            patch("modulo.db.crud.sso_provider.get_provider", AsyncMock(return_value=provider)),
            patch("modulo.db.crud.sso_provider.apply_updates"),
            _patch_audit() as audit,
        ):
            from modulo.db.crud.sso_provider import update_provider

            audit.side_effect = Exception("audit down")
            result = await update_provider(
                mock_session,
                _PROVIDER_ID,
                org_id=_ORG_ID,
                fernet_key=_FERNET_KEY,
                name="Renamed SSO",
            )
        assert result is provider


class TestDeleteAndToggle:
    async def test_delete_provider_success(self, mock_session: AsyncMock) -> None:
        provider = _make_provider()
        with (
            patch("modulo.db.crud.sso_provider.get_provider", AsyncMock(return_value=provider)),
            _patch_audit() as audit,
        ):
            from modulo.db.crud.sso_provider import delete_provider

            result = await delete_provider(mock_session, _PROVIDER_ID, org_id=_ORG_ID)
        assert result is True
        mock_session.delete.assert_awaited_once_with(provider)
        audit.assert_awaited_once()

    async def test_delete_provider_missing(self, mock_session: AsyncMock) -> None:
        with patch("modulo.db.crud.sso_provider.get_provider", AsyncMock(return_value=None)):
            from modulo.db.crud.sso_provider import delete_provider

            assert await delete_provider(mock_session, _PROVIDER_ID, org_id=_ORG_ID) is False

    async def test_toggle_provider_flips_enabled(self, mock_session: AsyncMock) -> None:
        provider = _make_provider(enabled=True)
        provider.enabled = True
        with (
            patch("modulo.db.crud.sso_provider.get_provider", AsyncMock(return_value=provider)),
            _patch_audit(),
        ):
            from modulo.db.crud.sso_provider import toggle_provider

            result = await toggle_provider(mock_session, _PROVIDER_ID, org_id=_ORG_ID)
        assert result.enabled is False

    async def test_toggle_provider_missing(self, mock_session: AsyncMock) -> None:
        with patch("modulo.db.crud.sso_provider.get_provider", AsyncMock(return_value=None)):
            from modulo.db.crud.sso_provider import toggle_provider

            assert await toggle_provider(mock_session, _PROVIDER_ID, org_id=_ORG_ID) is None


class TestSetGroupMappings:
    async def test_sets_mappings(self, mock_session: AsyncMock) -> None:
        provider = _make_provider()
        mappings = [{"group": "admins", "role": "admin"}]
        with patch("modulo.db.crud.sso_provider.get_provider", AsyncMock(return_value=provider)):
            from modulo.db.crud.sso_provider import set_group_mappings

            result = await set_group_mappings(mock_session, _PROVIDER_ID, mappings, org_id=_ORG_ID)
        assert result.group_mappings == mappings
        mock_session.flush.assert_awaited_once()

    async def test_returns_none_when_missing(self, mock_session: AsyncMock) -> None:
        with patch("modulo.db.crud.sso_provider.get_provider", AsyncMock(return_value=None)):
            from modulo.db.crud.sso_provider import set_group_mappings

            assert await set_group_mappings(mock_session, _PROVIDER_ID, [], org_id=_ORG_ID) is None
