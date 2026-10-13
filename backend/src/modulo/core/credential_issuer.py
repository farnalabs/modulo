"""Run-scoped credential issuance seam (FAR-1690, slice A1).

Every sandbox dispatch mints a short-TTL, run-scoped credential through this
seam. The default provider is :class:`ModuloCredentialIssuer`, which wraps the
existing FAR-296 ``mint_run_api_key`` / ``revoke_run_api_key`` machinery. An
external broker (Vault / cloud STS) is an ADDITIVE provider behind the same
:class:`CredentialIssuer` protocol — it is deliberately NOT wired in v1 (a
self-hosted-only product must not require a broker to function).

Issuance provenance (binding guardrail)
---------------------------------------
Every issued credential carries a typed issuance provenance:

* ``modulo_minted`` — Modulo minted the credential itself (``attested``; the
  minting code path is the evidence).
* ``broker_issued`` — an external broker issued it (``declared``; Modulo only
  *reports* the broker's claim, it cannot observe the broker's mint).

``observed`` is NEVER a valid issuance provenance: Modulo does not observe an
external mint, it either performs it (attested) or is told about it (declared).
:class:`IssuedRunCredential` rejects an ``observed`` attestation kind at
construction so the forbidden value cannot enter the run metadata by accident.

The provenance is stamped where the run actually reads its credential — the
``MODULO_CREDENTIAL_PROVENANCE`` sandbox env var (kept unspoofable by the
reserved-env-var denylist) — in addition to riding on the returned credential
handle.

Dependency-light by design: stdlib + typing only at import time; the DB / auth
modules are imported lazily inside the provider methods so this module can be
imported from the dependency-light dispatch path without pulling them in.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

_log = logging.getLogger(__name__)

# --- Issuance provenance vocabulary (declare once, validate at the boundary) --

ISSUANCE_PROVENANCE_MODULO_MINTED = "modulo_minted"
ISSUANCE_PROVENANCE_BROKER_ISSUED = "broker_issued"

ISSUANCE_PROVENANCES: frozenset[str] = frozenset({ISSUANCE_PROVENANCE_MODULO_MINTED, ISSUANCE_PROVENANCE_BROKER_ISSUED})

# The evidence class of a provenance value. ``attested`` = Modulo performed the
# mint; ``declared`` = Modulo reports a third party's claim. ``observed`` is
# forbidden for issuance (see module docstring) and rejected at construction.
PROVENANCE_KIND_ATTESTED = "attested"
PROVENANCE_KIND_DECLARED = "declared"
_FORBIDDEN_PROVENANCE_KIND = "observed"

# The sandbox env var the run reads its issuance provenance from. Reserved via
# ``db.runner_binding_constraints.RESERVED_ENV_VAR_PREFIXES`` (``MODULO_``) and
# the exact-name denylist, so a node's ``env_vars``/binding cannot spoof it.
RUN_CREDENTIAL_PROVENANCE_ENV = "MODULO_CREDENTIAL_PROVENANCE"


@dataclass(frozen=True)
class IssuedRunCredential:
    """A credential issued for exactly one run.

    ``secret`` is the raw credential value. It is excluded from ``repr`` and
    from equality so it is never rendered in a log line, a checkpoint, or a
    test failure message; the caller injects it into the workspace env and
    never persists it.
    """

    run_id: uuid.UUID
    node_id: str
    secret: str = field(repr=False, compare=False)
    provenance: str = ISSUANCE_PROVENANCE_MODULO_MINTED
    provenance_kind: str = PROVENANCE_KIND_ATTESTED
    issuer: str = "modulo"
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.provenance not in ISSUANCE_PROVENANCES:
            raise ValueError(
                f"invalid issuance provenance {self.provenance!r}; valid values: {sorted(ISSUANCE_PROVENANCES)}"
            )
        if self.provenance_kind == _FORBIDDEN_PROVENANCE_KIND:
            raise ValueError("issuance provenance must never be 'observed'")


@runtime_checkable
class CredentialIssuer(Protocol):
    """The issuance seam: issue-for-run / revoke-for-run / describe.

    A provider is either Modulo-minted (the default) or an external broker.
    ``revoke_for_run`` is wired into the terminalisation path with the
    ``RunTeardownSeal`` slice (FAR-1690 A3); the seam declares it now so the
    revoke leg has one owner when it lands.
    """

    name: str
    provenance: str

    async def issue_for_run(
        self,
        *,
        session_factory: Callable[..., Any] | None,
        org_id: str | uuid.UUID,
        run_id: str | uuid.UUID,
        node_id: str,
        sandbox_timeout: int,
    ) -> IssuedRunCredential | None:
        """Issue a run-scoped credential, or ``None`` (fail-open — never raises)."""
        ...

    async def revoke_for_run(self, session: Any, *, run_id: uuid.UUID, org_id: uuid.UUID) -> int:
        """Revoke every credential linked to *run_id*; returns the count revoked."""
        ...

    def describe(self) -> Mapping[str, str]:
        """Provider metadata (name + provenance + evidence kind) for run telemetry."""
        ...


def _coerce_uuid(value: str | uuid.UUID | None) -> uuid.UUID | None:
    """Coerce a (possibly string) identifier to a UUID, or None when invalid."""
    if isinstance(value, uuid.UUID):
        return value
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


class ModuloCredentialIssuer:
    """Default provider: a Modulo-minted, run-scoped runner-role API key.

    Wraps the FAR-296 machinery: the account is resolved from the run row
    (falling back to the org's first active admin), the TTL is
    ``max(15 min, node_timeout + 5 min)`` capped by the org max, and the key is
    linked to the run via ``OrgApiKey.run_id`` so the existing finalisation
    revoke and compensating sweep already cover it.
    """

    name: str = "modulo"
    provenance: str = ISSUANCE_PROVENANCE_MODULO_MINTED

    async def issue_for_run(
        self,
        *,
        session_factory: Callable[..., Any] | None,
        org_id: str | uuid.UUID,
        run_id: str | uuid.UUID,
        node_id: str,
        sandbox_timeout: int,
    ) -> IssuedRunCredential | None:
        """Mint a run-scoped runner-role API key. Fail-open: ``None`` on any failure.

        The raw key value is returned to the caller only, which injects it into
        the sandbox envs — it is NEVER logged, checkpointed, or placed in
        LangGraph state.
        """
        if session_factory is None:
            return None
        org_uuid = _coerce_uuid(org_id)
        if org_uuid is None:
            return None
        run_uuid = _coerce_uuid(run_id)
        if run_uuid is None:
            return None
        try:
            from sqlalchemy import text as _sql_text

            from modulo.auth.api_key import mint_run_api_key
            from modulo.db.crud.run import get_run_api_key_ttl_seconds
            from modulo.db.rls import set_rls_execution_context, set_rls_org

            ttl_seconds = await get_run_api_key_ttl_seconds(session_factory, org_uuid, sandbox_timeout)
            async with session_factory() as session, session.begin():
                await set_rls_org(session, org_uuid)
                await set_rls_execution_context(session)
                account_id = await self._resolve_account_id(session, run_uuid, org_uuid, _sql_text)
                if account_id is None:
                    return None
                minted = await mint_run_api_key(
                    session,
                    org_id=org_uuid,
                    run_id=run_uuid,
                    node_id=node_id,
                    account_id=account_id,
                    ttl_seconds=ttl_seconds,
                )
                if minted is None:
                    return None
                _key_row, full_key = minted
                return IssuedRunCredential(
                    run_id=run_uuid,
                    node_id=node_id,
                    secret=full_key,
                    provenance=self.provenance,
                    provenance_kind=PROVENANCE_KIND_ATTESTED,
                    issuer=self.name,
                    expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception(
                "credential_issuer.modulo_issue_failed",
                extra={"run_id": str(run_id), "node_id": node_id, "org_id": str(org_id)},
            )
            return None

    @staticmethod
    async def _resolve_account_id(
        session: Any,
        run_uuid: uuid.UUID,
        org_uuid: uuid.UUID,
        sql_text: Callable[[str], Any],
    ) -> uuid.UUID | None:
        """Resolve the run's triggering account; fall back to the first active admin.

        The per-run key acts as the run's triggering user; a run with no
        ``account_id`` (webhook/cron child runs are legitimately NULL) falls
        back to the org's first active admin — still a REAL account row, never
        a fabricated identity. Returns None when neither exists.
        """
        row = (
            await session.execute(
                sql_text("SELECT account_id FROM runs WHERE id=:rid AND organisation_id=:oid"),
                {"rid": str(run_uuid), "oid": str(org_uuid)},
            )
        ).fetchone()
        account_id_raw = row[0] if row else None
        if account_id_raw is None:
            admin_row = (
                await session.execute(
                    sql_text(
                        "SELECT a.id FROM accounts a JOIN org_memberships om ON om.account_id = a.id "
                        "WHERE om.organisation_id=:oid AND om.role='admin' AND om.deactivated_at IS NULL "
                        "ORDER BY a.created_at LIMIT 1"
                    ),
                    {"oid": str(org_uuid)},
                )
            ).fetchone()
            account_id_raw = admin_row[0] if admin_row else None
        if account_id_raw is None:
            return None
        return uuid.UUID(str(account_id_raw))

    async def revoke_for_run(self, session: Any, *, run_id: uuid.UUID, org_id: uuid.UUID) -> int:
        """Revoke every Modulo-minted credential linked to *run_id* (FAR-296)."""
        from modulo.auth.api_key import revoke_run_api_key

        return await revoke_run_api_key(session, run_id=run_id, org_id=org_id)

    def describe(self) -> Mapping[str, str]:
        """Provider metadata for run telemetry (never carries a secret)."""
        return {
            "issuer": self.name,
            "provenance": self.provenance,
            "provenance_kind": PROVENANCE_KIND_ATTESTED,
        }


def default_credential_issuer() -> CredentialIssuer:
    """Return the default (Modulo-minted) credential issuer."""
    return ModuloCredentialIssuer()
