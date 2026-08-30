"""Regression tests: SSO callbacks must run DB work inside session.begin().

The DI session factory uses ``autobegin=False``. ``oidc_process_callback`` and
``saml_process_response`` execute SQL (JIT provisioning, group mappings, token
issuance) on the injected session, so the route handlers must wrap them in an
explicit transaction. These tests call the real handler against an autobegin-aware
fake session; the callback is mocked to perform a ``session.execute()`` (standing in
for the JIT-provisioning queries) so the pre-fix handler fails loudly.
"""

from unittest.mock import MagicMock, patch

from fastapi import Request

from modulo.api.routes.sso import oidc_callback
from modulo.settings import Settings

_VALID_32 = "a" * 32


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


class _AutobeginAwareSession:
    """Fake session whose execute() requires an explicit begin() first."""

    def __init__(self) -> None:
        self._in_tx = False

    def begin(self) -> "_BeginCtx":
        return _BeginCtx(self)

    async def execute(self, stmt: object, *args: object) -> MagicMock:
        assert self._in_tx, "execute() ran outside session.begin() (autobegin=False)"
        return MagicMock()


class _BeginCtx:
    """Async context manager returned by ``_AutobeginAwareSession.begin()``."""

    def __init__(self, session: _AutobeginAwareSession) -> None:
        self._session = session

    async def __aenter__(self) -> None:
        self._session._in_tx = True

    async def __aexit__(self, *_exc: object) -> bool:
        self._session._in_tx = False
        return False


def _make_callback_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/auth/oidc/google/callback",
            "query_string": b"code=abc&state=google:xyz",
            "headers": [],
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 123),
            "root_path": "",
        }
    )


async def test_oidc_callback_runs_db_work_inside_begin() -> None:
    """The OIDC callback's DB work must run inside session.begin().

    The mocked ``oidc_process_callback`` performs a ``session.execute()`` (standing
    in for ``jit_provision_user``'s queries). The fake session only allows executes
    inside a transaction, so the pre-fix handler (bare call, no begin) fails while
    the fixed handler (``async with session.begin()``) redirects successfully.
    """
    settings = _make_settings()
    request = _make_callback_request()
    fake = _AutobeginAwareSession()

    async def fake_process_callback(
        code: str,
        state: str,
        s: Settings,
        system_session: object,
        app_session: object,
        redirect_uri: str,
    ) -> dict:
        assert isinstance(app_session, _AutobeginAwareSession)
        await app_session.execute(MagicMock())
        return {"access_token": "at", "refresh_token": "rt"}

    with patch("modulo.api.routes.sso.oidc_process_callback", new=fake_process_callback):
        resp = await oidc_callback(
            provider="google",
            request=request,
            _=None,
            settings=settings,
            session=fake,
            system_session=fake,
        )

    assert resp.status_code == 307
    assert "access_token=at" in resp.headers["location"]
    assert "refresh_token=rt" in resp.headers["location"]
