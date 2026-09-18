"""modulo-break-glass: break-glass admin recovery CLI (deliverable B).

Usage::

  modulo-break-glass activate <org-id|org-slug> --reason TEXT [--ttl-minutes N] [--dry-run] [--yes]
  modulo-break-glass deactivate <org-id|org-slug> --reason TEXT [--force] [--account-id UUID]
  modulo-break-glass status [org-id|org-slug] [--all] [--json]
  modulo-break-glass force-last-admin <org-id|org-slug> --reason TEXT
  modulo-break-glass smoke

Connects as ``modulo_breakglass`` via ``MODULO_BREAK_GLASS_DATABASE_URL`` — a
dedicated LAZY engine (importing this module connects to nothing). The
caller-bound SECURITY DEFINER ``deactivate_break_glass`` (0036) is the sole
write path for deactivation / force-last-admin. Operator authentication is a
hmac.compare_digest check of ``--secret`` / ``MODULO_BREAK_GLASS_OPERATOR_SECRET``
against the configured primary/standby secrets, with the actor derived from
which secret matched.

Exit codes (0-9): 0 success, 1 unexpected, 2 usage (missing --reason etc),
3 org-not-found (incl. deactivate M2040 target-does-not-exist), 4 activation-txn
failure, 5 preconditions (incl. force refusals + the status-sweep live-row exit),
6 deactivate-refused, 7 smoke failure, 8 deactivate atomicity failure,
9 credential-print failure.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
import sys
import uuid
from datetime import datetime, timedelta
from typing import Any, cast

import click
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.auth.passwords import hash_password
from modulo.core.audit_logger import append_audit_event
from modulo.db.models.account import Account
from modulo.db.models.audit_event import AuditEvent
from modulo.db.models.org_membership import OrgMembership
from modulo.db.models.organisation import Organisation
from modulo.db.rls import set_rls_org
from modulo.settings import Settings, get_settings

_log = logging.getLogger(__name__)

EXIT_SUCCESS = 0
EXIT_UNEXPECTED = 1
EXIT_USAGE = 2
EXIT_ORG_NOT_FOUND = 3
EXIT_ACTIVATION_TXN_FAILURE = 4
EXIT_PRECONDITIONS = 5
EXIT_DEACTIVATE_REFUSED = 6
EXIT_SMOKE_FAILURE = 7
EXIT_DEACTIVATE_ATOMICITY_FAILURE = 8
EXIT_CREDENTIAL_PRINT_FAILURE = 9

# The CLI calls the SECURITY DEFINER with a sentinel caller id; authorization is
# carried entirely by the session_user operator branch (modulo_breakglass only).
_CALLER_SENTINEL = uuid.UUID(int=0)

_OPERATOR_PRIMARY_ACTOR = "operator"
_OPERATOR_STANDBY_ACTOR = "operator-standby"

# Email collisions are regenerated up to this many times before failing.
_ACTIVATION_EMAIL_TRIES = 5


class BreakGlassError(Exception):
    exit_code: int = EXIT_UNEXPECTED


class BreakGlassUsageError(BreakGlassError):
    exit_code = EXIT_USAGE


class OrgNotFoundError(BreakGlassError):
    exit_code = EXIT_ORG_NOT_FOUND


class ActivationTxnError(BreakGlassError):
    exit_code = EXIT_ACTIVATION_TXN_FAILURE


class PreconditionError(BreakGlassError):
    exit_code = EXIT_PRECONDITIONS


class DeactivateRefusedError(BreakGlassError):
    exit_code = EXIT_DEACTIVATE_REFUSED


class SmokeFailureError(BreakGlassError):
    exit_code = EXIT_SMOKE_FAILURE


class DeactivateAtomicityError(BreakGlassError):
    exit_code = EXIT_DEACTIVATE_ATOMICITY_FAILURE


class CredentialPrintError(BreakGlassError):
    exit_code = EXIT_CREDENTIAL_PRINT_FAILURE


# ── Dedicated lazy engine (keyed on the break-glass URL, never the app URL) ──


_bg_engine: AsyncEngine | None = None
_bg_engine_url: str | None = None
_bg_factory: async_sessionmaker[AsyncSession] | None = None
_bg_factory_url: str | None = None


def get_break_glass_engine(settings: Settings) -> AsyncEngine:
    """Return the process-global break-glass engine, creating it if necessary.

    Keyed on ``MODULO_BREAK_GLASS_DATABASE_URL`` (not the app ``database_url``)
    and LAZY — importing this module never connects to anything. Mirrors
    ``api.dependencies.get_or_create_engine``'s module-level cache.
    """
    global _bg_engine, _bg_engine_url
    url = settings.modulo_break_glass_database_url
    if _bg_engine is None or _bg_engine_url != url:
        _bg_engine = create_async_engine(
            url,
            pool_pre_ping=True,
            connect_args={"timeout": 10, "ssl": False},
        )
        _bg_engine_url = url
    return _bg_engine


def get_break_glass_session_factory(settings: Settings) -> async_sessionmaker[AsyncSession]:
    """Return a session factory bound to the break-glass engine (cached)."""
    global _bg_factory, _bg_factory_url
    url = settings.modulo_break_glass_database_url
    if _bg_factory is None or _bg_factory_url != url:
        _bg_factory = async_sessionmaker(
            get_break_glass_engine(settings),
            expire_on_commit=False,
            autobegin=False,
        )
        _bg_factory_url = url
    return _bg_factory


# ── Operator authentication (hmac.compare_digest against primary/standby) ────


def authenticate_operator(secret: str | None, settings: Settings) -> str:
    """Return the actor derived from the matching secret, else exit-5 error."""
    provided = (secret or "").strip()
    if not provided:
        raise PreconditionError("operator secret required — pass --secret or set MODULO_BREAK_GLASS_OPERATOR_SECRET")
    primary = settings.modulo_break_glass_secret
    standby = settings.modulo_break_glass_standby_secret
    if primary and hmac.compare_digest(provided, primary):
        return _OPERATOR_PRIMARY_ACTOR
    if standby and hmac.compare_digest(provided, standby):
        return _OPERATOR_STANDBY_ACTOR
    raise PreconditionError(
        "operator secret does not match MODULO_BREAK_GLASS_SECRET or MODULO_BREAK_GLASS_STANDBY_SECRET"
    )


# ── Shared async helpers ─────────────────────────────────────────────────────


def _sqlstate_from_exc(exc: BaseException) -> str | None:
    """Extract a PostgreSQL SQLSTATE (M2010/M2020/M2040) from a wrapped DBAPI error."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if hasattr(current, "sqlstate"):
            state = current.sqlstate
            if isinstance(state, str) and state:
                return state
        current = getattr(current, "orig", None) or getattr(current, "__cause__", None)
    return None


async def _resolve_org(session: AsyncSession, ref: str) -> Organisation | None:
    try:
        org_id = uuid.UUID(str(ref))
    except (ValueError, AttributeError, TypeError):
        result = await session.execute(select(Organisation).where(Organisation.slug == ref))
    else:
        result = await session.execute(select(Organisation).where(Organisation.id == org_id))
    return result.scalar_one_or_none()


async def _db_now(session: AsyncSession) -> datetime:
    """DB clock is authoritative for expiry computation (plan §2)."""
    result = await session.execute(text("SELECT current_timestamp"))
    return cast("datetime", result.scalar_one())


def _is_live(row: Account, now: datetime) -> bool:
    return (
        row.is_break_glass
        and row.break_glass_deactivated_at is None
        and row.break_glass_expires_at is not None
        and row.break_glass_expires_at > now
    )


def _row_state(row: Account, now: datetime) -> str:
    if row.break_glass_deactivated_at is not None:
        return "deactivated"
    if row.break_glass_expires_at is None or row.break_glass_expires_at <= now:
        return "expired"
    return "live"


def _is_consumed(row: Account) -> bool:
    # Cross-cutting consumed flag: live AND the hash lacks the bcrypt $2 prefix
    # (the login hook's CAS re-randomizes it to gen_random_uuid on first use).
    return row.password_hash is not None and not row.password_hash.startswith("$2")


async def _bg_accounts_for_org(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    only_undeactivated: bool,
) -> list[Account]:
    stmt = (
        select(Account)
        .join(OrgMembership, OrgMembership.account_id == Account.id)
        .where(OrgMembership.organisation_id == org_id, Account.is_break_glass.is_(True))
    )
    if only_undeactivated:
        stmt = stmt.where(Account.break_glass_deactivated_at.is_(None))
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _live_non_bg_admins(session: AsyncSession, org_id: uuid.UUID) -> list[Account]:
    stmt = (
        select(Account)
        .join(OrgMembership, OrgMembership.account_id == Account.id)
        .where(
            OrgMembership.organisation_id == org_id,
            OrgMembership.deactivated_at.is_(None),
            OrgMembership.role == "admin",
            Account.active.is_(True),
            Account.is_break_glass.is_(False),
        )
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


# ── Core async operations (injected-session + now pattern) ──────────────────


async def activate(
    session: AsyncSession,
    *,
    now: datetime,
    org_id: uuid.UUID,
    ttl_minutes: int,
    actor: str,
    reason: str,
) -> str:
    """Synthesize a break-glass account + admin membership + audit in ONE txn.

    All-or-nothing: any failure rolls back the whole txn and raises. The
    delivered credential is returned AFTER commit so callers print it only once
    the durable activation row (and audit) exist.
    """
    credential = secrets.token_urlsafe(32)
    async with session.begin():
        await set_rls_org(session, org_id)

        email: str | None = None
        for _ in range(_ACTIVATION_EMAIL_TRIES):
            candidate = f"break-glass-{secrets.token_hex(8)}@modulo.run"
            existing = (
                await session.execute(select(Account.id).where(Account.email == candidate))
            ).scalar_one_or_none()
            if existing is None:
                email = candidate
                break
        if email is None:
            raise ActivationTxnError(
                f"could not synthesize a unique break-glass email after {_ACTIVATION_EMAIL_TRIES} attempts"
            )

        account = Account(
            email=email,
            display_name="Break-glass recovery",
            password_hash=hash_password(credential),
            auth_provider="local",
            active=True,
            is_break_glass=True,
            break_glass_expires_at=now + timedelta(minutes=ttl_minutes),
        )
        session.add(account)
        await session.flush()

        membership = OrgMembership(
            account_id=account.id,
            organisation_id=org_id,
            role="admin",
        )
        session.add(membership)
        await session.flush()

        await append_audit_event(
            session,
            org_id=org_id,
            event_type="break_glass_activated",
            actor_user_id=None,
            payload_json={"operator": actor, "reason": reason},
        )
        await session.flush()

        _log.info(
            "break_glass_activated org_id=%s account_id=%s operator=%s",
            org_id,
            account.id,
            actor,
        )
    return credential


async def deactivate(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    account_id: uuid.UUID | None,
    actor: str,
    reason: str,
    force: bool,
    now: datetime,
) -> dict[str, Any]:
    """Tombstone break-glass account(s) via the caller-bound SECURITY DEFINER.

    Without ``account_id``, ALL undeactivated break-glass rows for the org are
    targeted. Refuses (exit 6) when a LIVE activation exists unless ``force``.
    The audit write is pinned to the SAME transaction as the function call.
    """
    rows: list[Account] = []
    sentinel = _CALLER_SENTINEL
    try:
        async with session.begin():
            rows = await _bg_accounts_for_org(session, org_id, only_undeactivated=True)
            if account_id is not None:
                rows = [row for row in rows if row.id == account_id]
            if not rows:
                suffix = f" / account {account_id}" if account_id is not None else ""
                raise OrgNotFoundError(f"no break-glass target for org {org_id}{suffix}")
            if not force and any(_is_live(row, now) for row in rows):
                raise DeactivateRefusedError(
                    "a live break-glass activation exists for this org; pass --force to proceed "
                    "or --account-id to target one specific row"
                )
            for row in rows:
                await session.execute(
                    text("SELECT public.deactivate_break_glass(:caller, :target, false)"),
                    {"caller": sentinel, "target": row.id},
                )
            await append_audit_event(
                session,
                org_id=org_id,
                event_type="break_glass_deactivated",
                actor_user_id=None,
                payload_json={"operator": actor, "reason": reason},
            )
            await session.flush()
    except SQLAlchemyError as exc:
        sqlstate = _sqlstate_from_exc(exc)
        if sqlstate == "M2040":
            raise OrgNotFoundError("deactivation target does not exist") from exc
        if sqlstate in ("M2010", "M2020"):
            raise DeactivateRefusedError(f"deactivation refused by database ({sqlstate}): {exc}") from exc
        raise DeactivateAtomicityError(f"deactivation transaction failed: {exc}") from exc
    return {"deactivated": len(rows), "account_ids": [str(row.id) for row in rows]}


async def force_last_admin(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor: str,
    reason: str,
    now: datetime,
) -> dict[str, Any]:
    """Remove the org's last live NON-break-glass admin (operator-only path).

    Refuses (exit 5) when the org has zero or multiple live non-break-glass
    admins, when it is a break-glass-only org, or when the only live admins are
    break-glass accounts. The ``last_admin_forcibly_removed`` audit is written
    in the same transaction as the SECURITY DEFINER call.
    """
    removed_account_id: str | None = None
    sentinel = _CALLER_SENTINEL
    try:
        async with session.begin():
            admins = await _live_non_bg_admins(session, org_id)
            if not admins:
                bg_live = [
                    row
                    for row in await _bg_accounts_for_org(session, org_id, only_undeactivated=True)
                    if _is_live(row, now)
                ]
                if bg_live:
                    raise PreconditionError(
                        "org has no live non-break-glass admin and its only live admins are break-glass "
                        "accounts — refusing to remove a live break-glass account"
                    )
                raise PreconditionError("org has no live non-break-glass admins to remove")
            if len(admins) > 1:
                raise PreconditionError(
                    f"org has {len(admins)} live non-break-glass admins — force-last-admin requires exactly one"
                )
            target = admins[0]
            removed_account_id = str(target.id)
            await session.execute(
                text("SELECT public.deactivate_break_glass(:caller, :target, true)"),
                {"caller": sentinel, "target": target.id},
            )
            await append_audit_event(
                session,
                org_id=org_id,
                event_type="last_admin_forcibly_removed",
                actor_user_id=None,
                payload_json={"operator": actor, "reason": reason},
            )
            await session.flush()
    except SQLAlchemyError as exc:
        sqlstate = _sqlstate_from_exc(exc)
        if sqlstate == "M2040":
            raise OrgNotFoundError("force-last-admin target does not exist") from exc
        if sqlstate in ("M2010", "M2020"):
            raise PreconditionError(f"force-last-admin refused by database ({sqlstate}): {exc}") from exc
        raise DeactivateAtomicityError(f"force-last-admin transaction failed: {exc}") from exc
    if removed_account_id is None:
        raise DeactivateAtomicityError("force-last-admin transaction produced no removed account id")
    return {"removed_account_id": removed_account_id}


async def _latest_activation_actor_reason(
    session: AsyncSession,
    org_ids: set[str],
) -> dict[str, tuple[str, str]]:
    """Per-org (operator, reason) from the most recent break_glass_activated audit."""
    if not org_ids:
        return {}
    parsed = [uuid.UUID(value) for value in org_ids]
    result = await session.execute(
        select(AuditEvent)
        .where(
            AuditEvent.organisation_id.in_(parsed),
            AuditEvent.event_type == "break_glass_activated",
        )
        .order_by(AuditEvent.created_at.desc())
    )
    latest: dict[str, tuple[str, str]] = {}
    for event in result.scalars():
        key = str(event.organisation_id)
        if key not in latest:
            payload = event.payload_json or {}
            latest[key] = (
                str(payload.get("operator", "")),
                str(payload.get("reason", "")),
            )
    return latest


async def status_rows(
    session: AsyncSession,
    *,
    org_id: uuid.UUID | None,
    all_rows: bool,
    now: datetime,
) -> list[dict[str, Any]]:
    """List break-glass rows with org identity + state (+ actor/reason best-effort)."""
    stmt = (
        select(Account, Organisation)
        .join(OrgMembership, OrgMembership.account_id == Account.id)
        .join(Organisation, Organisation.id == OrgMembership.organisation_id)
        .where(Account.is_break_glass.is_(True))
    )
    if org_id is not None:
        stmt = stmt.where(OrgMembership.organisation_id == org_id)
    if not all_rows:
        stmt = stmt.where(
            Account.break_glass_deactivated_at.is_(None),
            Account.break_glass_expires_at.is_not(None),
            Account.break_glass_expires_at > now,
        )
    result = await session.execute(stmt)
    pairs = list(result.all())
    if not pairs:
        return []
    actor_reason = await _latest_activation_actor_reason(session, {str(org.id) for _, org in pairs})
    rows: list[dict[str, Any]] = []
    for account, org in pairs:
        actor, reason = actor_reason.get(str(org.id), ("", ""))
        rows.append(
            {
                "org_id": str(org.id),
                "org_slug": org.slug,
                "org_name": org.name,
                "account_id": str(account.id),
                "email": account.email,
                "state": _row_state(account, now),
                "consumed": _is_consumed(account),
                "activated_at": account.created_at,
                "expires_at": account.break_glass_expires_at,
                "break_glass_deactivated_at": account.break_glass_deactivated_at,
                "last_login_at": account.last_login,
                "actor": actor,
                "reason": reason,
            }
        )
    return rows


async def smoke(session: AsyncSession) -> dict[str, Any]:
    """Connectivity probe + basic posture assertions (non-zero exit on failure)."""
    try:
        one = (await session.execute(text("SELECT 1"))).scalar_one()
        if one != 1:
            raise SmokeFailureError("connectivity probe SELECT 1 did not return 1")
        role = (await session.execute(text("SELECT session_user"))).scalar_one()
        if role != "modulo_breakglass":
            raise SmokeFailureError(f"session_user is {role!r}, expected 'modulo_breakglass'")
        function = (
            await session.execute(text("SELECT to_regprocedure('public.deactivate_break_glass(uuid, uuid, boolean)')"))
        ).scalar_one()
        if not function:
            raise SmokeFailureError("deactivate_break_glass SECURITY DEFINER not found")
    except SmokeFailureError:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise SmokeFailureError(f"smoke probe failed: {exc}") from exc
    return {"connectivity": "ok", "session_user": role, "deactivate_function": str(function)}


# ── Click glue ───────────────────────────────────────────────────────────────


def _settings_from_ctx(ctx: click.Context) -> Settings:
    settings = ctx.obj.get("settings")
    if settings is None:
        settings = get_settings()
        ctx.obj["settings"] = settings
    return cast("Settings", settings)


def _factory_from_ctx(ctx: click.Context, settings: Settings) -> Any:
    factory = ctx.obj.get("session_factory")
    if factory is None:
        factory = get_break_glass_session_factory(settings)
        ctx.obj["session_factory"] = factory
    return factory


def _require_reason(ctx: click.Context, reason: str) -> None:
    if not reason or not reason.strip():
        click.echo("error: --reason is required and must be non-empty", err=True)
        ctx.exit(EXIT_USAGE)


def _confirm_interactive(org: Organisation) -> bool:
    click.echo(f"Target organisation: {org.name} (id={org.id}, slug={org.slug})")
    if not sys.stdin.isatty():
        return False
    try:
        return bool(click.confirm("Proceed with break-glass activation?"))
    except click.Abort:
        return False


def _print_dry_run(org: Organisation, ttl_minutes: int, reason: str, actor: str) -> None:
    click.echo(f"[dry-run] would activate break-glass for org {org.name} (id={org.id}, slug={org.slug})")
    click.echo(f"[dry-run] ttl_minutes={ttl_minutes}, reason={reason!r}, operator={actor}")
    click.echo(
        "[dry-run] would create a fresh break-glass-<16-hex>@modulo.run org admin, "
        "write the break_glass_activated audit, and print a single-use credential once"
    )


def _deliver_credential(credential: str, *, yes: bool) -> None:
    try:
        if yes:
            sys.stdout.write(credential + "\n")
            sys.stdout.flush()
        else:
            click.echo(credential)
    except Exception as exc:
        raise CredentialPrintError(f"credential delivery to stdout failed after commit: {exc}") from exc


def _render_status(rows: list[dict[str, Any]], *, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(rows, indent=2, default=str))
        return
    if not rows:
        click.echo("no break-glass rows")
        return
    for row in rows:
        click.echo(
            f"{row['org_slug']:24s} {row['state']:10s} account={row['account_id']} "
            f"expires={row['expires_at']} reason={row['reason'] or ''} actor={row['actor'] or ''}"
        )


@click.group()
@click.option(
    "--secret",
    envvar="MODULO_BREAK_GLASS_OPERATOR_SECRET",
    default=None,
    help="Operator secret (or MODULO_BREAK_GLASS_OPERATOR_SECRET)",
)
@click.pass_context
def cli(ctx: click.Context, secret: str | None) -> None:
    """Break-glass admin recovery — emergency org access via modulo_breakglass."""
    ctx.ensure_object(dict)
    settings = _settings_from_ctx(ctx)
    if "actor" not in ctx.obj:
        try:
            ctx.obj["actor"] = authenticate_operator(secret, settings)
        except BreakGlassError as exc:
            click.echo(f"error: {exc}", err=True)
            ctx.exit(exc.exit_code)


@cli.command("activate")
@click.argument("org_ref", type=str)
@click.option("--reason", required=True, type=str, help="Ticket/reason reference (required)")
@click.option(
    "--ttl-minutes",
    type=int,
    default=None,
    help="TTL in minutes (default = MODULO_BREAK_GLASS_TTL_MINUTES)",
)
@click.option("--dry-run", is_flag=True, default=False, help="Print what would happen without executing")
@click.option("--yes", is_flag=True, default=False, help="Non-interactive: skip confirmation, print to stdout")
@click.pass_context
def activate_cmd(
    ctx: click.Context,
    org_ref: str,
    reason: str,
    ttl_minutes: int | None,
    dry_run: bool,
    yes: bool,
) -> None:
    """Synthesize a break-glass admin credential for an org."""
    _require_reason(ctx, reason)
    settings = _settings_from_ctx(ctx)
    if ttl_minutes is None:
        ttl_minutes = settings.modulo_break_glass_ttl_minutes
    if ttl_minutes < 1 or ttl_minutes > settings.modulo_break_glass_max_ttl_minutes:
        click.echo(
            f"error: --ttl-minutes must be within [1, {settings.modulo_break_glass_max_ttl_minutes}]; "
            f"got {ttl_minutes}",
            err=True,
        )
        ctx.exit(EXIT_PRECONDITIONS)
    actor = ctx.obj["actor"]
    asyncio.run(_async_activate(ctx, settings, org_ref, reason, ttl_minutes, dry_run, yes, actor))


async def _async_activate(
    ctx: click.Context,
    settings: Settings,
    org_ref: str,
    reason: str,
    ttl_minutes: int,
    dry_run: bool,
    yes: bool,
    actor: str,
) -> None:
    factory = _factory_from_ctx(ctx, settings)
    credential: str | None = None
    try:
        # Read phase (autobegin=False sessions require an explicit txn).
        async with factory() as session, session.begin():
            org = await _resolve_org(session, org_ref)
            if org is None:
                raise OrgNotFoundError(f"organisation {org_ref!r} not found")
            now = await _db_now(session)
            live = await _bg_accounts_for_org(session, org.id, only_undeactivated=True)
        if not yes and not _confirm_interactive(org):
            raise PreconditionError("activation aborted by operator")
        if dry_run:
            _print_dry_run(org, ttl_minutes, reason, actor)
            return
        if any(_is_live(row, now) for row in live):
            click.echo(
                f"warning: a live break-glass activation already exists for org {org.slug} — "
                "this creates a fresh account"
            )
        credential = await activate(
            session,
            now=now,
            org_id=org.id,
            ttl_minutes=ttl_minutes,
            actor=actor,
            reason=reason,
        )
    except asyncio.CancelledError:
        raise
    except BreakGlassError as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(exc.exit_code)
    except Exception as exc:
        click.echo(f"error: activation failed: {exc}", err=True)
        ctx.exit(EXIT_ACTIVATION_TXN_FAILURE)
    if credential is None:
        raise ActivationTxnError("activation succeeded but returned no credential")
    try:
        _deliver_credential(credential, yes=yes)
    except CredentialPrintError as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(EXIT_CREDENTIAL_PRINT_FAILURE)


@cli.command("deactivate")
@click.argument("org_ref", type=str)
@click.option("--reason", required=True, type=str, help="Ticket/reason reference (required)")
@click.option("--force", is_flag=True, default=False, help="Proceed when a live activation exists")
@click.option("--account-id", type=str, default=None, help="Deactivate one specific live account")
@click.pass_context
def deactivate_cmd(
    ctx: click.Context,
    org_ref: str,
    reason: str,
    force: bool,
    account_id: str | None,
) -> None:
    """Tombstone break-glass account(s) for an org (atomic SECURITY DEFINER)."""
    _require_reason(ctx, reason)
    settings = _settings_from_ctx(ctx)
    parsed_account_id: uuid.UUID | None = None
    if account_id:
        try:
            parsed_account_id = uuid.UUID(account_id)
        except (ValueError, AttributeError):
            click.echo(f"error: invalid --account-id {account_id!r}", err=True)
            ctx.exit(EXIT_USAGE)
    actor = ctx.obj["actor"]
    asyncio.run(_async_deactivate(ctx, settings, org_ref, reason, parsed_account_id, force, actor))


async def _async_deactivate(
    ctx: click.Context,
    settings: Settings,
    org_ref: str,
    reason: str,
    account_id: uuid.UUID | None,
    force: bool,
    actor: str,
) -> None:
    factory = _factory_from_ctx(ctx, settings)
    try:
        # Read phase (autobegin=False sessions require an explicit txn).
        async with factory() as session, session.begin():
            org = await _resolve_org(session, org_ref)
            if org is None:
                raise OrgNotFoundError(f"organisation {org_ref!r} not found")
            now = await _db_now(session)
        result = await deactivate(
            session,
            org_id=org.id,
            account_id=account_id,
            actor=actor,
            reason=reason,
            force=force,
            now=now,
        )
        click.echo(
            f"deactivated {result['deactivated']} break-glass account(s) for org {org.slug}: "
            f"{', '.join(result['account_ids'])}"
        )
    except asyncio.CancelledError:
        raise
    except BreakGlassError as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(exc.exit_code)
    except SQLAlchemyError as exc:
        click.echo(f"error: deactivation transaction failed: {exc}", err=True)
        ctx.exit(EXIT_DEACTIVATE_ATOMICITY_FAILURE)
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(EXIT_UNEXPECTED)


@cli.command("status")
@click.argument("org_ref", type=str, required=False)
@click.option(
    "--all",
    "all_rows",
    is_flag=True,
    default=False,
    help="List all break-glass rows (incl. expired/deactivated) across orgs",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable JSON output")
@click.pass_context
def status_cmd(
    ctx: click.Context,
    org_ref: str | None,
    all_rows: bool,
    as_json: bool,
) -> None:
    """List break-glass rows.

    ``status --all --json`` is the daily-sweep form: it prints the rows and
    exits 5 (preconditions) when any live row exists so the sweep can alert.
    """
    settings = _settings_from_ctx(ctx)
    actor = ctx.obj["actor"]
    asyncio.run(_async_status(ctx, settings, org_ref, all_rows, as_json, actor))


async def _async_status(
    ctx: click.Context,
    settings: Settings,
    org_ref: str | None,
    all_rows: bool,
    as_json: bool,
    _actor: str,
) -> None:
    factory = _factory_from_ctx(ctx, settings)
    try:
        # Read phase (autobegin=False sessions require an explicit txn).
        async with factory() as session, session.begin():
            if org_ref:
                org = await _resolve_org(session, org_ref)
                if org is None:
                    raise OrgNotFoundError(f"organisation {org_ref!r} not found")
                org_id = org.id
            else:
                if not all_rows:
                    raise BreakGlassUsageError("provide an org (id|slug) or use --all")
                org_id = None
            now = await _db_now(session)
            rows = await status_rows(session, org_id=org_id, all_rows=all_rows, now=now)
        _render_status(rows, as_json=as_json)
        if all_rows and any(row["state"] == "live" for row in rows):
            live_count = sum(1 for row in rows if row["state"] == "live")
            raise PreconditionError(f"{live_count} live break-glass row(s) exist")
    except asyncio.CancelledError:
        raise
    except BreakGlassError as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(exc.exit_code)
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(EXIT_UNEXPECTED)


@cli.command("force-last-admin")
@click.argument("org_ref", type=str)
@click.option("--reason", required=True, type=str, help="Ticket/reason reference (required)")
@click.pass_context
def force_last_admin_cmd(ctx: click.Context, org_ref: str, reason: str) -> None:
    """Remove the org's last live non-break-glass admin (operator-only)."""
    _require_reason(ctx, reason)
    settings = _settings_from_ctx(ctx)
    actor = ctx.obj["actor"]
    asyncio.run(_async_force_last_admin(ctx, settings, org_ref, reason, actor))


async def _async_force_last_admin(
    ctx: click.Context,
    settings: Settings,
    org_ref: str,
    reason: str,
    actor: str,
) -> None:
    factory = _factory_from_ctx(ctx, settings)
    try:
        # Read phase (autobegin=False sessions require an explicit txn).
        async with factory() as session, session.begin():
            org = await _resolve_org(session, org_ref)
            if org is None:
                raise OrgNotFoundError(f"organisation {org_ref!r} not found")
            now = await _db_now(session)
        result = await force_last_admin(
            session,
            org_id=org.id,
            actor=actor,
            reason=reason,
            now=now,
        )
        click.echo(f"force-last-admin: removed account {result['removed_account_id']} from org {org.slug}")
    except asyncio.CancelledError:
        raise
    except BreakGlassError as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(exc.exit_code)
    except Exception as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(EXIT_UNEXPECTED)


@cli.command()
@click.pass_context
def smoke_cmd(ctx: click.Context) -> None:
    """Connectivity + basic posture probe against the break-glass role."""
    settings = _settings_from_ctx(ctx)
    actor = ctx.obj["actor"]
    asyncio.run(_async_smoke(ctx, settings, actor))


async def _async_smoke(
    ctx: click.Context,
    settings: Settings,
    _actor: str,
) -> None:
    factory = _factory_from_ctx(ctx, settings)
    try:
        async with factory() as session, session.begin():
            result = await smoke(session)
        click.echo(json.dumps(result))
    except asyncio.CancelledError:
        raise
    except BreakGlassError as exc:
        click.echo(f"error: {exc}", err=True)
        ctx.exit(exc.exit_code)
    except Exception as exc:
        click.echo(f"error: smoke failed: {exc}", err=True)
        ctx.exit(EXIT_SMOKE_FAILURE)


if __name__ == "__main__":
    cli()
