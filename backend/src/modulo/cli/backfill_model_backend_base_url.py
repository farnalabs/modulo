"""``modulo backfill-model-backend-base-url`` — one-shot legacy endpoint backfill.

FAR-2046 moved a model backend's custom endpoint out of ``default_params.base_url``
(the pre-FAR-2046 model-parameter location) into the validated, encrypted
credential JSON. Every write path migrates or clears it and the UI pre-fills it,
but a row nobody edits keeps the legacy value forever. Two consequences:

1. A legacy ``http://<public-host>`` endpoint keeps working, sending the stored
   API key in cleartext, with no ``allow_insecure_http`` opt-in ever recorded.
2. On a provider outside the capability map the legacy value is ignored by the
   hub while the credential field stays empty, so reads and reality disagree
   until the first edit.

WHY THIS IS A CLI AND NOT A MIGRATION
-------------------------------------
Credentials are written through the pluggable ``SecretsBackend``
(``set_secret(str(backend.id), <json>)``). The default ``FernetSecretsBackend``
is org-scoped — it reads ``secrets.organisation_id`` from the session's RLS
context — and the backend may be an external store (Vault / AWS Secrets Manager)
that SQL cannot write at all. A plain Alembic migration cannot decrypt with the
org's key, write an external store, or establish per-org RLS context. So the
backfill runs in-process, iterating organisations exactly like
``cli/migrate_org.py`` does.

BEHAVIOUR
---------
Dry run by default; ``--apply`` writes. Each row carrying a legacy
``default_params.base_url`` is classified, and only the host is ever reported
(a URL path segment can itself carry a secret):

* ``migrate`` — valid ``https``. Merge it into the credential JSON (preserving
  any existing ``api_key``), then strip the legacy key. When the credential
  JSON already carries a ``base_url`` that one is preserved, so the effective
  endpoint never changes.
* ``strip-insecure`` — plain ``http``. Never auto-blessed: strip it from both
  places and let an operator re-enter it with the explicit opt-in.
* ``reject-invalid`` — any other validation failure. Stripped.
* ``drop-incapable`` — the provider's endpoint is not configurable through this
  field, so the hub never used the value. Stripped.
* ``skip`` — the key exists but holds no usable value. The dead key is still
  removed so a second run is a true no-op.

Exit code 0 when there was nothing to do or every row was migrated/skipped;
non-zero when any row was stripped or rejected, so a deploy gate notices.

IMPORT HYGIENE: module scope is stdlib + click only. The console-script import
graph loads this module before any command runs, so settings / DB / secrets
imports happen lazily inside the command.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import click

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from modulo.settings import Settings

Outcome = Literal["migrate", "strip-insecure", "reject-invalid", "drop-incapable", "skip"]

EXIT_OK = 0
EXIT_NEEDS_ATTENTION = 1

_OUTCOMES: tuple[Outcome, ...] = ("migrate", "strip-insecure", "reject-invalid", "drop-incapable", "skip")
_ATTENTION_OUTCOMES = frozenset({"strip-insecure", "reject-invalid", "drop-incapable"})
#: The row needs no credential write — only the legacy key is removed.
_STRIP_ONLY_OUTCOMES = frozenset({"strip-insecure", "reject-invalid", "drop-incapable", "skip"})


@dataclass(frozen=True)
class Classification:
    """The non-secret classification of one legacy endpoint."""

    outcome: Outcome
    host: str | None
    detail: str | None = None
    value: str | None = None


@dataclass(frozen=True)
class BackfillRow:
    """One row's report entry. Never carries the URL, only its host."""

    backend_id: uuid.UUID
    organisation_id: uuid.UUID
    organisation_slug: str
    provider: str
    outcome: Outcome
    host: str | None
    detail: str | None = None


@dataclass
class BackfillResult:
    """Everything the command needs to report and to pick an exit code."""

    rows: list[BackfillRow] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    applied: bool = False

    @property
    def considered(self) -> int:
        return len(self.rows)

    @property
    def counts(self) -> dict[Outcome, int]:
        counts: dict[Outcome, int] = dict.fromkeys(_OUTCOMES, 0)
        for row in self.rows:
            counts[row.outcome] += 1
        return counts

    @property
    def requires_attention(self) -> bool:
        """True when anything was stripped/rejected (or a row failed outright)."""
        return bool(self.failures) or any(row.outcome in _ATTENTION_OUTCOMES for row in self.rows)


@dataclass
class _PlannedRow:
    """A row plus the in-memory secret payload needed to apply it.

    ``payload`` is decrypted credential data; it is excluded from ``repr`` so it
    can never leak through a debug log or an exception message.
    """

    report: BackfillRow
    backend: Any = field(repr=False)
    payload: dict[str, Any] = field(default_factory=dict, repr=False)
    write_secret: bool = False
    strip_legacy: bool = False


def classify_legacy_endpoint(provider: str, raw: object) -> Classification:
    """Classify one legacy ``default_params.base_url`` value.

    Reuses the PRODUCTION rules — the shared validator the REST write path
    delegates to, and the hub's capability map — rather than a hand-copied
    subset that can drift (FAR-2067).
    """
    from modulo.core.model_backend_endpoints import (
        INSECURE_HTTP_NOT_ALLOWED,
        BaseUrlValidationError,
        legacy_base_url,
        redact_base_url,
        validate_base_url,
    )
    from modulo.core.model_backend_hub import supports_custom_base_url

    # ``legacy_base_url`` is the single definition of "a usable legacy value"
    # (non-blank string); a JSON null / blank / non-string is a dead key.
    usable = legacy_base_url({"base_url": raw}) if isinstance(raw, str) else None
    host = redact_base_url(usable)
    if usable is None:
        return Classification("skip", None, "no usable legacy value")

    if not supports_custom_base_url(provider):
        return Classification("drop-incapable", host, "provider does not support a custom endpoint")

    try:
        validate_base_url(usable)
    except BaseUrlValidationError as exc:
        if exc.code == INSECURE_HTTP_NOT_ALLOWED:
            return Classification("strip-insecure", host, "plain http is never auto-blessed")
        return Classification("reject-invalid", host, str(exc))
    return Classification("migrate", host, value=usable)


def _stored_secret_payload(raw: str | None) -> dict[str, Any]:
    """Parse a stored credential JSON object; malformed/absent -> ``{}``."""
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _read_secret_payload(secrets_backend: Any, backend_id: uuid.UUID) -> dict[str, Any]:
    """Read a backend's credential JSON, treating a missing/corrupt row as empty."""
    try:
        raw = await secrets_backend.get_secret(str(backend_id))
    except (KeyError, ValueError):
        return {}
    return _stored_secret_payload(raw)


async def _resolve_api_key(
    session: AsyncSession,
    backend: Any,
    payload: dict[str, Any],
    settings: Settings,
    organisation_id: uuid.UUID,
) -> str:
    """Return the row's API key, recovering a legacy ciphertext-only credential.

    FAR-2046 rows written before the credential-JSON move hold the key only in
    ``credentials_ciphertext``; writing a ``base_url``-only secret would narrow
    the row and lose the key. Recover it exactly as the update path does.

    Raises ``ValueError`` when nothing recoverable exists — the caller leaves
    the row untouched rather than destroying the credential.
    """
    from modulo.auth.secret_storage import decode_stored_secret_scoped

    existing = payload.get("api_key")
    if isinstance(existing, str) and existing:
        return existing
    ciphertext = getattr(backend, "credentials_ciphertext", None)
    if not ciphertext:
        raise ValueError("no stored API key")
    recovered = await decode_stored_secret_scoped(
        session,
        ciphertext,
        settings.fernet_key,
        org_id=organisation_id,
    )
    if not recovered:
        raise ValueError("recovered API key is empty")
    payload["api_key"] = recovered
    return recovered


async def _plan_row(
    session: AsyncSession,
    backend: Any,
    organisation_id: uuid.UUID,
    organisation_slug: str,
    settings: Settings,
    secrets_backend: Any,
) -> _PlannedRow:
    """Classify one row and, for ``migrate``, load the credential payload."""
    from modulo.core.model_backend_endpoints import stored_base_url

    default_params = backend.default_params if isinstance(backend.default_params, Mapping) else {}
    raw = default_params.get("base_url")
    classification = classify_legacy_endpoint(backend.provider, raw)
    report = BackfillRow(
        backend_id=backend.id,
        organisation_id=organisation_id,
        organisation_slug=organisation_slug,
        provider=backend.provider,
        outcome=classification.outcome,
        host=classification.host,
        detail=classification.detail,
    )
    if classification.outcome != "migrate":
        return _PlannedRow(
            report=report,
            backend=backend,
            strip_legacy=classification.outcome in _STRIP_ONLY_OUTCOMES,
        )

    payload = await _read_secret_payload(secrets_backend, backend.id)
    if stored_base_url(payload) is not None:
        # The credential endpoint already wins; never overwrite it. Removing the
        # dead legacy key leaves the effective endpoint unchanged.
        return _PlannedRow(
            report=BackfillRow(
                backend_id=report.backend_id,
                organisation_id=report.organisation_id,
                organisation_slug=report.organisation_slug,
                provider=report.provider,
                outcome="migrate",
                host=report.host,
                detail="credential endpoint already set; preserved",
            ),
            backend=backend,
            payload=payload,
            strip_legacy=True,
        )

    try:
        await _resolve_api_key(session, backend, payload, settings, organisation_id)
    except Exception:
        # No recoverable key: writing the endpoint would narrow the credentials.
        # Leave the row exactly as it is so the legacy value keeps working.
        return _PlannedRow(
            report=BackfillRow(
                backend_id=report.backend_id,
                organisation_id=report.organisation_id,
                organisation_slug=report.organisation_slug,
                provider=report.provider,
                outcome="reject-invalid",
                host=report.host,
                detail="credential unavailable; legacy endpoint left in place",
            ),
            backend=backend,
        )

    payload["base_url"] = classification.value
    return _PlannedRow(report=report, backend=backend, payload=payload, write_secret=True, strip_legacy=True)


async def _apply_row(
    secrets_backend: Any,
    planned: _PlannedRow,
) -> None:
    """Persist one planned row's credential and/or legacy-key removal."""
    from modulo.core.model_backend_endpoints import strip_legacy_base_url

    backend = planned.backend
    if planned.write_secret:
        await secrets_backend.set_secret(str(backend.id), json.dumps(planned.payload))
    if planned.strip_legacy:
        stripped = strip_legacy_base_url(backend.default_params)
        if stripped != dict(backend.default_params or {}):
            backend.default_params = stripped


async def _process_org(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    organisation: Any,
    *,
    apply: bool,
) -> tuple[list[BackfillRow], list[str]]:
    """Plan (and with ``apply`` write) one organisation under its own RLS scope.

    Returns the rows to report and any failure messages. The report is only
    returned after the transaction commits, so it always matches what was
    actually persisted.
    """
    from sqlalchemy import select

    from modulo.core.secrets_backend import create_secrets_backend
    from modulo.db.models.model_backend import ModelBackend
    from modulo.db.rls import set_rls_org

    planned_rows: list[_PlannedRow] = []
    async with session_factory() as session:
        try:
            async with session.begin():
                await set_rls_org(session, organisation.id)
                secrets_backend = create_secrets_backend(fernet_key=settings.fernet_key, session=session)
                backends = (
                    (
                        await session.execute(
                            select(ModelBackend)
                            .where(ModelBackend.organisation_id == organisation.id)
                            .order_by(ModelBackend.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                for backend in backends:
                    params = backend.default_params if isinstance(backend.default_params, Mapping) else {}
                    if "base_url" not in params:
                        continue
                    planned = await _plan_row(
                        session,
                        backend,
                        organisation.id,
                        organisation.slug,
                        settings,
                        secrets_backend,
                    )
                    planned_rows.append(planned)
                    if apply:
                        await _apply_row(secrets_backend, planned)
        except Exception as exc:  # reported per org, never swallowed
            return [], [f"org {organisation.slug} ({organisation.id}): {type(exc).__name__}: {exc}"]
    return [planned.report for planned in planned_rows], []


async def run_backfill(
    *,
    apply: bool,
    organisation_id: uuid.UUID | None = None,
    settings: Settings | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> BackfillResult:
    """Run the backfill across the requested organisation(s)."""
    from sqlalchemy import select

    from modulo.db.models.organisation import Organisation
    from modulo.settings import get_settings

    resolved_settings = settings or get_settings()
    if session_factory is None:
        from modulo.db.session import AsyncSessionLocal

        session_factory = AsyncSessionLocal

    result = BackfillResult(applied=apply)
    async with session_factory() as session, session.begin():
        stmt = select(Organisation).order_by(Organisation.id)
        if organisation_id is not None:
            stmt = stmt.where(Organisation.id == organisation_id)
        organisations = (await session.execute(stmt)).scalars().all()

    if organisation_id is not None and not organisations:
        raise click.ClickException(f"organisation {organisation_id} not found")

    for organisation in organisations:
        rows, failures = await _process_org(
            session_factory,
            resolved_settings,
            organisation,
            apply=apply,
        )
        result.rows.extend(rows)
        result.failures.extend(failures)
    return result


def _render_row(row: BackfillRow) -> str:
    host = row.host or "-"
    line = f"  {row.backend_id}  provider={row.provider:<14} {row.outcome:<14} host={host}  org={row.organisation_slug}"
    if row.detail:
        line = f"{line}  ({row.detail})"
    return line


def _render(result: BackfillResult) -> None:
    counts = result.counts
    mode = "APPLY" if result.applied else "DRY RUN"
    click.echo(f"Model-backend legacy base_url backfill — {mode}")
    if not result.rows and not result.failures:
        click.echo("No legacy default_params.base_url values found — nothing to do.")
        return
    for row in result.rows:
        click.echo(_render_row(row))
    for failure in result.failures:
        click.echo(f"  ERROR {failure}", err=True)
    summary = " ".join(f"{outcome}={counts[outcome]}" for outcome in _OUTCOMES)
    click.echo(f"Summary: {result.considered} row(s) considered — {summary}")
    if not result.applied:
        click.echo("Nothing was written (dry run). Re-run with --apply to write.")


@click.command("backfill-model-backend-base-url")
@click.option("--apply", is_flag=True, default=False, help="Write the changes (default is a dry run).")
@click.option(
    "--org-id",
    "org_id",
    default=None,
    metavar="UUID",
    help="Only process this organisation (default: every organisation).",
)
def backfill_model_backend_base_url(apply: bool, org_id: str | None) -> None:
    """Backfill legacy model-backend base_url values into the credential store.

    Dry run by default; pass --apply to write. Exits non-zero when any row was
    stripped or rejected so a deploy gate notices.
    """
    parsed_org_id: uuid.UUID | None = None
    if org_id is not None:
        try:
            parsed_org_id = uuid.UUID(org_id)
        except ValueError as exc:
            raise click.BadParameter(f"invalid organisation UUID: {org_id!r}") from exc
    try:
        result = asyncio.run(run_backfill(apply=apply, organisation_id=parsed_org_id))
    except click.ClickException:
        raise
    except Exception as exc:  # top-level CLI boundary
        raise click.ClickException(f"backfill failed: {type(exc).__name__}: {exc}") from exc
    _render(result)
    raise SystemExit(EXIT_NEEDS_ATTENTION if result.requires_attention else EXIT_OK)
