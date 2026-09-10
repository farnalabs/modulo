"""``modulo users``: local-account management for a single install (FAR-680).

Three DB-direct commands on the native-launcher surface:

* ``modulo users add <email> [--admin] [--password <pw> | prompt]``
* ``modulo users list``
* ``modulo users reset-password <email> [--password <pw> | prompt]``

Creation and password RESEEDING reuse the promoted MODULO_USERS seeder
(``modulo.db.seed.seed_modulo_user`` — the same creation path the boot
seeder runs) and the auth password machinery (``hash_password``); session
invalidation on reset reuses the token-family helpers the admin reset
route uses (``list_families_for_account`` / ``blacklist_family``).

DATABASE URL resolution (never prompts for stored credentials):

1. launcher-managed: ``--data-dir`` (or the default per-OS launcher root)
   with a present ``state.json`` — state.json + secrets.json compose the
   bundled-postgres URL (the same resolution the launcher boot uses).
2. otherwise the standard operator path: ``DATABASE_URL`` via Settings
   (env / pinned config.env).

MODULO_USERS guard: when ``MODULO_USERS`` is set, its entries are
re-applied on every boot — but ONLY for accounts whose stored hash is
missing or already non-bcrypt (plaintext); a bcrypt hash written by this
CLI is NOT reverted, and accounts not listed in MODULO_USERS are never
touched. The guard still refuses loudly by default (an operator should
know before any local-account write) with ``--force`` to proceed.

Bcrypt-hash rejection: ``users add`` refuses passwords starting ``$2``
BEFORE seeding — the seeder stores a ``$2``-prefixed password part
verbatim as a pre-computed bcrypt hash, which would create an account
that can never log in.

Break-glass refusal: ``reset-password`` refuses break-glass accounts
(``accounts.is_break_glass``) exactly like the admin reset route —
overwriting the hash breaks the break-glass CAS and orphans the
last-resort recovery credential.

Residual token window (stated in the reset-password help): the reset and
token-family blacklist kill refresh/rotation immediately, but a stolen
access JWT stays valid until it expires — same as the admin reset route.

Read-only guarantee: ``users list`` never creates side-effect files —
it needs state.json + secrets.json to READ the bundled credentials and
fails loudly (``LauncherConfigError``) when secrets.json is absent,
instead of ``load_or_create`` which would GENERATE fresh credentials.

IMPORT HYGIENE: module scope is click + stdlib only (the console-script
import graph loads this module before any command runs — settings/engine
imports happen lazily inside the commands). Passwords are never echoed
back in full anywhere in the output; exit codes are 0 success, 1 failure.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import datetime
from pathlib import Path
from typing import Any

import click
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.launcher.state import STATE_FILENAME

ListRow = tuple[str, str, str, str, str]


class UserCliError(Exception):
    """A user-facing CLI failure (exit code 1 via ``ClickException``)."""


# ---------------------------------------------------------------------------
# DB-URL resolution
# ---------------------------------------------------------------------------


def _launcher_inputs_readonly(data_dir: Path) -> tuple[Any, Any]:
    """Read (state.json, secrets.json) WITHOUT creating anything.

    ``load_or_create`` generates a whole fresh credential set when
    secrets.json is absent — a write side effect ``users list`` can never
    make (and, with a freshly-generated password that cannot match the
    running bundled Postgres, a composed URL that could never connect).
    """
    from modulo.launcher.config_source import LauncherConfigError
    from modulo.launcher.secrets_file import SECRETS_FILENAME, SecretsFileError, _parse
    from modulo.launcher.state import load_state

    state_path = data_dir / STATE_FILENAME
    secrets_path = data_dir / SECRETS_FILENAME
    if not state_path.exists():
        raise LauncherConfigError(f"no state.json at {state_path} — the data dir is not bootstrapped")
    if not secrets_path.exists():
        raise LauncherConfigError(f"no secrets.json at {secrets_path} — the data dir is not bootstrapped")
    try:
        secrets = _parse(secrets_path.read_bytes())
    except (OSError, SecretsFileError) as exc:
        raise LauncherConfigError(f"secrets file unreadable: {exc}") from exc
    try:
        return load_state(state_path, secrets.state_hmac_key), secrets
    except Exception as exc:  # StateIntegrityError / StateVersionError
        raise LauncherConfigError(f"launcher state unreadable: {exc}") from exc


def _launcher_url_for(data_dir: Path, *, readonly: bool = False) -> str:
    from modulo.launcher.config_source import compose_config, load_launcher_config_inputs

    try:
        if readonly:
            state, secrets = _launcher_inputs_readonly(data_dir)
        else:
            state, secrets = load_launcher_config_inputs(data_dir)
        url = compose_config(state, secrets)["DATABASE_URL"]
    except Exception as exc:  # LauncherConfigError / StateIntegrityError / HMAC failures
        raise click.ClickException(f"launcher data dir unreadable: {exc}") from exc
    if not url:
        raise click.ClickException(f"no DATABASE_URL composed from state.json in {data_dir}")
    return url


def _settings_url() -> str:
    from modulo.settings import get_settings

    try:
        url = get_settings().database_url
    except Exception as exc:
        raise click.ClickException(f"settings unavailable: {exc}") from exc
    if not url:
        raise click.ClickException("no DATABASE_URL configured (env / config.env)")
    return url


def _resolve_database_url(data_dir: Path | None, *, readonly: bool = False) -> str:
    """Resolve the DB URL: launcher state.json first, then DATABASE_URL/Settings.

    ``readonly=True`` (``users list``) reads the launcher secrets file
    load-only — a missing secrets.json is a loud refusal, never a
    credential-generation side effect.
    """
    if data_dir is not None:
        state_path = data_dir / STATE_FILENAME
        if not state_path.exists():
            raise click.ClickException(
                f"no state.json at {state_path} — pass a bootstrapped data dir, "
                "or omit --data-dir and rely on DATABASE_URL"
            )
        return _launcher_url_for(data_dir, readonly=readonly)
    try:
        from modulo.launcher.entry import default_data_dir

        bundled = default_data_dir()
    except Exception:
        return _settings_url()
    if (bundled / STATE_FILENAME).exists():
        return _launcher_url_for(bundled, readonly=readonly)
    return _settings_url()


# ---------------------------------------------------------------------------
# MODULO_USERS guard
# ---------------------------------------------------------------------------


def _guard_modulo_users(force: bool) -> None:
    """Refuse writes when MODULO_USERS is set, unless --force is given."""
    from modulo.settings import get_settings

    try:
        modulo_users = get_settings().modulo_users
    except Exception:
        modulo_users = ""
    if not modulo_users:
        return
    message = (
        "WARNING: MODULO_USERS is set — the boot seeder re-applies its entries on every boot, but ONLY for "
        "accounts whose stored hash is missing or already non-bcrypt (plaintext). A bcrypt hash written by "
        "this CLI is NOT reverted, and accounts not listed in MODULO_USERS are never touched."
    )
    if force:
        click.echo(f"{message} Proceeding (--force).", err=True)
        return
    raise click.ClickException(f"{message} Pass --force to proceed anyway.")


# ---------------------------------------------------------------------------
# Password handling
# ---------------------------------------------------------------------------


def _resolved_password(password_option: str | None) -> str:
    """``--password`` value, or a hidden double prompt."""
    if password_option is not None:
        return password_option
    return click.prompt("Password", hide_input=True, confirmation_prompt=True)


def _validate_password_strength(password: str) -> None:
    """Same entropy gate the change-password API route enforces."""
    from modulo.auth.passwords import validate_password_strength

    try:
        validate_password_strength(password)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None


# ---------------------------------------------------------------------------
# Async core (one command = one lazy engine, disposed before exit)
# ---------------------------------------------------------------------------


def _engine_and_factory(url: str) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(url)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _first_org(session: AsyncSession) -> Any:
    """The oldest organisation — the same convention the boot seeder uses."""
    from modulo.db.models.organisation import Organisation

    result = await session.execute(select(Organisation).order_by(Organisation.created_at).limit(1))
    org = result.scalar_one_or_none()
    if org is None:
        raise UserCliError("no organisation exists in this database — run 'modulo start' once to bootstrap")
    return org


async def _add_user(url: str, email: str, password: str, as_admin: bool) -> str:
    """Create the user via the promoted boot-seeder path; --admin fixes the role.

    The role REPORTED is the membership role actually stored after seeding —
    the seeder special-cases admin/admin@modulo.run to admin even without
    ``--admin``, so the flag alone must never decide the printed role.
    """
    from modulo.db.models.account import Account
    from modulo.db.models.org_membership import OrgMembership
    from modulo.db.seed import seed_modulo_user

    engine, maker = _engine_and_factory(url)
    try:
        async with maker() as session, session.begin():
            org = await _first_org(session)
            existing = (await session.execute(select(Account).where(Account.email == email))).scalar_one_or_none()
            if existing is not None:
                raise UserCliError(f"user {email} already exists — nothing changed")
            await seed_modulo_user(session, org, f"{email}:{password}")
            account = (await session.execute(select(Account).where(Account.email == email))).scalar_one()
            membership = (
                await session.execute(
                    select(OrgMembership).where(
                        OrgMembership.account_id == account.id,
                        OrgMembership.organisation_id == org.id,
                    )
                )
            ).scalar_one_or_none()
            if membership is None:
                raise UserCliError(f"membership for {email} missing after creation")
            if as_admin:
                membership.role = "admin"
            return f"Created user {email} ({membership.role} in organisation '{org.name}')"
    finally:
        await engine.dispose()


async def _reset_password(url: str, email: str, password: str) -> str:
    """Reset the hash with the auth machinery and sign out active sessions.

    Break-glass accounts are refused (the admin reset route refuses too):
    overwriting their password_hash breaks the break-glass CAS and silently
    orphans the last-resort recovery credential.
    """
    from modulo.auth.passwords import hash_password
    from modulo.db.crud.token_family import blacklist_family, list_families_for_account
    from modulo.db.models.account import Account

    engine, maker = _engine_and_factory(url)
    try:
        async with maker() as session, session.begin():
            account = (await session.execute(select(Account).where(Account.email == email))).scalar_one_or_none()
            if account is None:
                raise UserCliError(f"no user with email {email} in this database — try 'modulo users list'")
            # Strict boolean compare (the admin route compares the same way):
            # a break-glass account must never have its hash overwritten.
            if account.is_break_glass is True:
                raise UserCliError(
                    f"{email} is a break-glass account — password reset refused (the admin route refuses too): "
                    "overwriting its hash breaks the break-glass CAS and orphans the last-resort recovery credential."
                )
            account.password_hash = hash_password(password)
            families = await list_families_for_account(session, account.id)
            for family in families:
                await blacklist_family(session, family.family_id, account.id)
            signed_out = len(families)
    finally:
        await engine.dispose()
    return f"Password reset for {email}; {signed_out} active session(s) were signed out."


async def _list_rows(url: str) -> tuple[list[ListRow], str]:
    """Email / display-name / admin / last-login / org rows for the primary org.

    The role column is relative to the primary org ONLY, so every row names
    its org (or explicitly annotates that the account has no active
    membership in it) — on multi-org installs an unannotated "no" would be
    misleading.
    """
    from modulo.db.models.account import Account
    from modulo.db.models.org_membership import OrgMembership

    engine, maker = _engine_and_factory(url)
    try:
        async with maker() as session:
            org = await _first_org(session)
            pairs = (
                await session.execute(
                    select(Account, OrgMembership.role)
                    .outerjoin(
                        OrgMembership,
                        (OrgMembership.account_id == Account.id)
                        & (OrgMembership.organisation_id == org.id)
                        & (OrgMembership.deactivated_at.is_(None)),
                    )
                    .order_by(Account.email)
                )
            ).all()
            rows = [
                (
                    account.email,
                    account.display_name,
                    "yes" if role == "admin" else "no",
                    _format_last_login(account.last_login),
                    org.name if role is not None else f"no membership in {org.name}",
                )
                for account, role in pairs
            ]
            return rows, org.name
    finally:
        await engine.dispose()


def _format_last_login(value: Any) -> str:
    if not isinstance(value, datetime):
        return "never"
    return value.strftime("%Y-%m-%d %H:%M")


def _print_rows(rows: list[ListRow], org_name: str) -> None:
    if not rows:
        click.echo(f"no members found in organisation '{org_name}'")
        return
    headers: tuple[str, ...] = ("email", "display_name", "admin", "last_login", "org")
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    click.echo("  ".join(header.ljust(widths[i]) for i, header in enumerate(headers)))
    click.echo("  ".join("-" * width for width in widths))
    for row in rows:
        click.echo("  ".join(value.ljust(widths[i]) for i, value in enumerate(row)))


# ---------------------------------------------------------------------------
# Command plumbing
# ---------------------------------------------------------------------------


def _run_db_command(coroutine: Coroutine[Any, Any, str]) -> str:
    """Run one DB coroutine; render failure classes as exit code 1."""
    try:
        return asyncio.run(coroutine)
    except UserCliError as exc:
        raise click.ClickException(str(exc)) from None
    except SQLAlchemyError as exc:
        raise click.ClickException(f"database error: {exc}") from exc


@click.group("users")
def users() -> None:
    """Manage local users against the single-install app database."""


@users.command("add")
@click.argument("email")
@click.option("--admin", is_flag=True, default=False, help="Grant the admin role in the primary org.")
@click.option("--password", "password_option", default=None, help="Password (prompted, hidden, when omitted).")
@click.option("--force", is_flag=True, default=False, help="Proceed despite MODULO_USERS being set.")
@click.option("--data-dir", type=click.Path(file_okay=False, path_type=Path), default=None)
def add(email: str, admin: bool, force: bool, password_option: str | None, data_dir: Path | None) -> None:
    """Create a local user with the same creation path the boot seeder uses."""
    _guard_modulo_users(force)
    password = _resolved_password(password_option)
    _validate_password_strength(password)
    # The seeder stores a "$2"-prefixed password part VERBATIM as a
    # pre-computed bcrypt hash — an account that could never log in. Refuse
    # before seeding instead.
    if password.startswith("$2"):
        raise click.ClickException(
            "password must not start with '$2' — the seeder stores that prefix verbatim as a "
            "pre-computed bcrypt hash, which would create an account that can never log in"
        )
    url = _resolve_database_url(data_dir)
    click.echo(_run_db_command(_add_user(url, email, password, admin)))


@users.command("reset-password")
@click.argument("email")
@click.option("--password", "password_option", default=None, help="Password (prompted, hidden, when omitted).")
@click.option("--force", is_flag=True, default=False, help="Proceed despite MODULO_USERS being set.")
@click.option("--data-dir", type=click.Path(file_okay=False, path_type=Path), default=None)
def reset_password(email: str, force: bool, password_option: str | None, data_dir: Path | None) -> None:
    """Reset a user's password hash and sign out their active sessions.

    Residual access-token window: the reset and the token-family blacklist
    kill refresh/rotation immediately, but a stolen access JWT stays valid
    until it expires — same as the admin reset route.
    """
    _guard_modulo_users(force)
    password = _resolved_password(password_option)
    _validate_password_strength(password)
    url = _resolve_database_url(data_dir)
    click.echo(_run_db_command(_reset_password(url, email, password)))


@users.command("list")
@click.option("--data-dir", type=click.Path(file_okay=False, path_type=Path), default=None)
def list_cmd(data_dir: Path | None) -> None:
    """List local users (email, admin flag, last login) — read-only."""
    url = _resolve_database_url(data_dir, readonly=True)
    try:
        rows, org_name = asyncio.run(_list_rows(url))
    except UserCliError as exc:
        raise click.ClickException(str(exc)) from None
    except SQLAlchemyError as exc:
        raise click.ClickException(f"database error: {exc}") from exc
    _print_rows(rows, org_name)
