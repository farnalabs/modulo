"""Centralised audit coverage for mutating REST routes (FAR-1472).

Modulo's audit trail is opt-in per call site: a route emits an event only if
someone remembered to call ``append_audit_event_isolated`` inline, so most
mutating endpoints write nothing at all. ``audited()`` is the shared dependency
factory that closes that gap — attach it to a route and the request gets ONE
chained audit event once the handler has finished::

    @router.post(
        "/parameter-schemas",
        status_code=status.HTTP_201_CREATED,
        dependencies=[
            Depends(
                audited(
                    "parameter_schema_created",
                    "parameter_schema",
                    principal_dep=get_current_tenant_user,
                )
            )
        ],
    )
    async def create_parameter_schema_endpoint(...) -> SchemaResponse: ...

Two policy rules (FAR-1517 / FAR-1538 — structurally enforced by
``tests/architecture/test_audit_antipatterns.py``)
---------------------------------------------------
* **The coarse ``audited()`` event must NOT reuse a rich event type the same
  route emits inline.** ``audited(...)`` writes a deliberately coarse event
  (method, path, outcome). A route that also calls its own
  ``append_audit_event(...)`` with the SAME ``event_type`` appends two events
  of one type per action. Give the coarse event the secondary
  ``api_access_<verb>`` namespace (``api_access_post``, ``api_access_put``,
  ``api_access_delete``, ...) and leave the rich domain event untouched.
* **A route that hard-deletes its own organisation must NOT use ``audited()``.**
  The append runs post-commit on a fresh session while
  ``audit_events.organisation_id`` FKs ``organisations.id`` — once the org row
  is gone the FK can never be satisfied, so the append fails open and records
  nothing. Write the record IN-transaction instead, with
  ``system_audit_logger.append_system_audit_event`` (the org-independent
  ledger), BEFORE the delete, so it commits only with it.

Why a dependency and not a route decorator
------------------------------------------
Both shapes work with FastAPI, but only the dependency fits this codebase:

* ``dependencies=[Depends(...)]`` on the route is already an established idiom
  here (``deny_break_glass_mint``, ``require_feature``), and it composes with
  ``@handle_db_errors`` instead of having to be ordered against it — a
  decorator's failure policy would otherwise be translated to an HTTP error by
  ``handle_db_errors`` before it could propagate.
* The router receives the *handler's* callable, so a decorator only sees the
  parameters FastAPI injects into that handler. A route-level dependency
  declares its own ``Request`` / principal / session and therefore works
  unchanged on handlers that name or declare their session differently.
* The coverage ratchet (``backend/scripts/audit_coverage.py``) can prove the
  annotation exists from the route decorator alone.

Fresh session, never the route's session
----------------------------------------
The append runs on its OWN session taken from the process-shared engine
(``modulo.db.session.get_shared_engine``), the same pattern
``core.hitl_email_alerts`` uses for a post-commit side effect. Consequences:

* it can neither roll back nor be blocked by the business transaction — they
  are different sessions with different transactions;
* it does not care whether the handler left the request session inside an open
  transaction (``append_audit_event_isolated`` calls ``session.begin()`` and
  would refuse to);
* FastAPI tears route dependencies down in reverse solve order, so by the time
  this runs ``get_db_session`` may already have closed the request session.

``audit_session`` is itself a FastAPI dependency precisely so tests can replace
it (``app.dependency_overrides[audit_session] = ...``).

Principal resolution
--------------------
The resolver is a REQUIRED argument (``principal_dep=``) rather than a default,
for a layering reason: the natural default, ``auth.dependencies.get_current_tenant_user``,
transitively reaches ``modulo.api.dependencies``, and the ``core-does-not-import-api``
contract (``backend/.importlinter``) forbids that chain from ``modulo.core``.
Passing the resolver from the route module keeps the import in the API layer,
where it already lives — and it also makes each route state the auth mode its
audit event is tied to::

    dependencies=[
        Depends(
            audited(
                "parameter_schema_created",
                "parameter_schema",
                principal_dep=get_current_tenant_user,
            )
        )
    ]

Match the route's own auth mode: ``get_current_tenant_user`` for
``require_permission`` routes, ``get_current_tenant_user_or_api_key`` for
``require_permission_any_credential`` routes. Because the resolver is a
sub-dependency it runs before the handler, so a mismatch fails at the auth
step instead of silently dropping audit events. On a route whose permission
check uses the same resolver, FastAPI's per-request dependency cache shares the
resolution — zero extra authentication work.

Failure policy
--------------
``fail_closed=False`` (default): the append is best-effort. The write goes
through ``append_audit_event_isolated``, which already logs-and-swallows its own
failures; anything that escapes it (session factory failure, cancellation is
re-raised) is logged under ``audit_coverage.<event_type>.append_failed`` and the
request carries on. Fail-open, with a log.

``fail_closed=True``: the same fresh-transaction append with the error left to
propagate — ``append_audit_event_isolated`` hard-codes fail-open, so a fail-closed
caller cannot use it. The raise escapes the dependency teardown (add
``scope="function"`` to the ``Depends`` to make it fail the response instead of
firing after the response has been sent).

When the *handler* itself fails, the attempt is still recorded (outcome
``"error"``) but the append is always best-effort: an audit failure must never
replace the error the caller is about to see.

Actor-less routes: ``audited_system()``
---------------------------------------
``audited()`` needs a request principal, which makes it unusable on the routes
whose identity is established INSIDE the handler or which never have a tenant
at all — sign-in/out and token refresh, the SAML ACS POST, the public error
ingest, and inbound webhooks. Those are exactly the security events worth
recording, so they use ``audited_system()`` instead::

    dependencies=[
        Depends(audited_system("login_attempted", "session", actor_source="pre_auth", fail_closed=True))
    ]

Same isolated-append path, with two differences that keep provenance honest:

* **no actor is ever fabricated.** The event is written with
  ``actor_user_id=NULL`` plus the ``payload["actor"] = SYSTEM_ACTOR`` marker
  and a ``payload["actor_source"]`` string (``pre_auth``,
  ``signature_verified``, ``unauthenticated``, ...) saying HOW the request was
  admitted — never WHO, because there is no who.
* **the organisation is published by the route, not resolved here.** An audit
  event is org-scoped (``audit_events.organisation_id`` is NOT NULL), so the
  handler calls ``bind_audit_org(request, org_id)`` at the point the tenant
  becomes known — and, for ingress that admits no tenant yet, with the
  unattributed sentinel org
  (``modulo.db.models.organisation.SYSTEM_ORG_ID``) so the attempt is still
  recorded; rebind the real org the moment it resolves. With no org published
  there is nowhere honest to record the event, so it is logged
  (``audit_coverage.<event_type>.no_org_context``) and skipped — the wrapper
  never invents a tenant to write into.

``bind_audit_actor_source()`` lets a route STRENGTHEN its declared
``actor_source`` once a stronger basis is established (a verified signature,
an authenticated principal), so the recorded provenance is always the
strongest TRUE statement about admission: a route passes its pre-admission
value (e.g. ``"unauthenticated"``) and promotes it after the check passes.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any, Literal

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modulo.auth.jwt import TenantPrincipal
from modulo.core.audit_logger import append_audit_event, append_audit_event_isolated
from modulo.core.audit_logger.labels import SYSTEM_ACTOR
from modulo.db.rls import set_rls_org, set_rls_user_context

_log = logging.getLogger(__name__)

__all__ = [
    "SYSTEM_ACTOR",
    "audit_session",
    "audited",
    "audited_system",
    "bind_audit_actor_source",
    "bind_audit_org",
]

#: How the request finished, as recorded in the coarse default payload:
#: ``"success"`` when the handler returned, ``"error"`` when it raised.
AuditOutcome = Literal["success", "error"]

#: A FastAPI dependency that resolves the acting principal for a request.
PrincipalResolver = Callable[..., Awaitable[TenantPrincipal]]

_LOG_KEY_PREFIX = "audit_coverage"


def _log_key(event_type: str) -> str:
    """Stable log key for an event type's append failures."""
    return f"{_LOG_KEY_PREFIX}.{event_type}.append_failed"


def _shared_session_factory() -> async_sessionmaker[AsyncSession]:
    """Session factory for the isolated audit write.

    Built on the process-shared engine — the same pool the API, dispatch and
    the SAQ worker share — with the same knobs ``api.dependencies``
    ``get_or_create_session_factory`` uses. The import is lazy for the same
    reason ``core.hitl_email_alerts._dispatch_session_factory`` keeps it lazy:
    importing ``modulo.db.session`` builds the process engine as a module-level
    side effect, which must not happen at *our* import time.
    """
    from modulo.db.session import get_shared_engine

    return async_sessionmaker(get_shared_engine(), expire_on_commit=False, autobegin=False)


async def audit_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield the fresh session the audit event is written on.

    A dependency in its own right so a test can replace it wholesale without
    touching the wrapper: ``app.dependency_overrides[audit_session] = ...``.
    """
    factory = _shared_session_factory()
    async with factory() as session:
        yield session


async def _append_isolated_or_raise(
    session: AsyncSession,
    principal: TenantPrincipal,
    *,
    event_type: str,
    resource_type: str,
    payload: dict[str, Any],
) -> None:
    """Append one audit event in a fresh transaction, letting errors propagate.

    This is ``append_audit_event_isolated``'s transaction block with the
    log-and-swallow removed: a fresh transaction, RLS context re-established
    inside it (``SET LOCAL`` reverts on COMMIT), then the chained append. The
    shared helper hard-codes fail-open, so it cannot serve ``fail_closed=True``.
    """
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        await append_audit_event(
            session,
            org_id=principal.organisation_id,
            event_type=event_type,
            actor_user_id=principal.account_id,
            resource_type=resource_type,
            payload_json=payload,
        )


async def _emit(
    *,
    session: AsyncSession,
    principal: TenantPrincipal,
    event_type: str,
    resource_type: str,
    payload: dict[str, Any],
    fail_closed: bool,
) -> None:
    """Record the event, applying the caller's failure policy.

    ``fail_closed=False`` logs and continues; ``fail_closed=True`` re-raises.
    Cancellation always propagates — a cancelled request must not be turned
    into an audit write.
    """
    log_key = _log_key(event_type)
    try:
        if fail_closed:
            await _append_isolated_or_raise(
                session,
                principal,
                event_type=event_type,
                resource_type=resource_type,
                payload=payload,
            )
        else:
            await append_audit_event_isolated(
                session,
                principal,
                resource_type=resource_type,
                event_type=event_type,
                payload=payload,
                log_key=log_key,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        if fail_closed:
            raise
        _log.warning(
            log_key,
            extra={
                "org_id": str(principal.organisation_id),
                "event_type": event_type,
                "resource_type": resource_type,
            },
            exc_info=True,
        )


def _coarse_payload(request: Request, outcome: AuditOutcome) -> dict[str, Any]:
    """Default payload: enough to tell what was called and how it finished.

    Deliberately coarse and identical for every annotated route: the resource
    id and the changed fields are not derivable from the request here. A route
    that needs that detail still calls ``append_audit_event_isolated`` itself
    for its own, richer event.
    """
    return {
        "http_method": request.method,
        "path": request.url.path,
        "outcome": outcome,
    }


def audited(
    event_type: str,
    resource_type: str,
    *,
    principal_dep: PrincipalResolver,
    fail_closed: bool = False,
) -> Callable[..., AsyncGenerator[None, None]]:
    """Dependency factory: record one audit event after the handler runs.

    Use it on a mutating route (the resolver import lives in the route
    module — see the module docstring for why ``core`` cannot default it)::

        dependencies=[
            Depends(
                audited("pipeline_updated", "pipeline", principal_dep=get_current_tenant_user)
            )
        ]

    Args:
        event_type: Chained-audit event type (``audit_events.event_type``).
        resource_type: Coarse resource kind the event is about.
        principal_dep: Resolver for the acting principal. REQUIRED — it must
            match the route's own auth mode so FastAPI's dependency cache
            shares the resolution with the route's permission check.
        fail_closed: Re-raise an append failure instead of logging it.

    Returns:
        An async-generator dependency suitable for ``Depends(...)`` or a
        route's ``dependencies=[...]`` list.
    """
    if not event_type.strip():
        raise ValueError("audited() requires a non-empty event_type")
    if not resource_type.strip():
        raise ValueError("audited() requires a non-empty resource_type")

    async def dependency(
        request: Request,
        session: AsyncSession = Depends(audit_session),
        principal: TenantPrincipal = Depends(principal_dep),
    ) -> AsyncGenerator[None, None]:
        """Record ``event_type`` once the route handler has finished."""
        try:
            yield
        except (GeneratorExit, asyncio.CancelledError):
            # Teardown without a completed handler (client gone / task
            # cancelled): no business outcome to record, and no await may run
            # during cancellation.
            raise
        except BaseException:
            # The handler failed (404/409/500 ...). Record the attempt, but
            # never let an audit failure replace the error already on its way
            # to the caller.
            await _emit(
                session=session,
                principal=principal,
                event_type=event_type,
                resource_type=resource_type,
                payload=_coarse_payload(request, "error"),
                fail_closed=False,
            )
            raise
        else:
            await _emit(
                session=session,
                principal=principal,
                event_type=event_type,
                resource_type=resource_type,
                payload=_coarse_payload(request, "success"),
                fail_closed=fail_closed,
            )

    return dependency


# ---------------------------------------------------------------------------
# Actor-less (system) variant — FAR-1516
# ---------------------------------------------------------------------------


def bind_audit_org(request: Request, org_id: uuid.UUID | str | None) -> None:
    """Publish the organisation an ``audited_system`` event belongs to (FAR-1516).

    Call it from the route handler: with the tenant org once it is known, or
    with the unattributed sentinel org (``SYSTEM_ORG_ID``) for ingress that has
    no tenant yet — rebinding the real org as soon as it resolves. ``None``
    leaves whatever was published earlier untouched, so a "try the tenant, fall
    back to nothing" call site cannot silently unpublish a good org.
    """
    if org_id is None or (isinstance(org_id, str) and not org_id.strip()):
        # None / "" both mean "no organisation published yet" — leave any
        # previously published org untouched rather than clobber it.
        return
    request.state.audit_org_id = str(org_id)


def bind_audit_actor_source(request: Request, actor_source: str) -> None:
    """Strengthen the ``actor_source`` an ``audited_system`` event records.

    The route declares its PRE-admission value on ``audited_system(...)`` and
    calls this once a stronger basis is established, so the recorded provenance
    is always the strongest true statement about how the request was admitted
    (``"unauthenticated"`` -> ``"signature_verified"`` / ``"authenticated"``).
    """
    if not isinstance(actor_source, str) or not actor_source.strip():
        raise ValueError("bind_audit_actor_source() requires a non-empty actor_source")
    request.state.audit_actor_source = actor_source


def _resolve_audit_org(request: Request) -> uuid.UUID | None:
    """The org the route published, or ``None`` when it published none.

    Attribute access + an ``isinstance`` gate, so a test double's
    auto-generated attributes can never masquerade as a published org.
    """
    try:
        raw = request.state.audit_org_id
    except AttributeError:
        return None
    if isinstance(raw, uuid.UUID):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        _log.warning(
            "audit_coverage: published audit org is not a UUID: %r",
            raw,
        )
        return None


def _resolve_actor_source(request: Request, default: str) -> str:
    """The route's promoted ``actor_source``, else the declared default."""
    try:
        raw = request.state.audit_actor_source
    except AttributeError:
        return default
    if isinstance(raw, str) and raw.strip():
        return raw
    return default


def _system_payload(request: Request, outcome: AuditOutcome, actor_source: str) -> dict[str, Any]:
    """Coarse payload plus the honest provenance markers for a system event."""
    payload = _coarse_payload(request, outcome)
    payload["actor"] = SYSTEM_ACTOR
    payload["actor_source"] = actor_source
    return payload


async def _append_system_or_raise(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    event_type: str,
    resource_type: str,
    payload: dict[str, Any],
) -> None:
    """Append one actor-less audit event in a fresh transaction, errors propagate.

    The actor-less twin of ``_append_isolated_or_raise``: same fresh
    transaction and ``SET LOCAL`` org context, but ``actor_user_id=NULL`` —
    there is no principal to attribute and one must never be invented. The
    ``SYSTEM_ACTOR`` marker plus ``actor_source`` in the payload carry the
    provenance instead.
    """
    async with session.begin():
        await set_rls_org(session, org_id)
        # No set_rls_user_context(): audit_events carries an org-only RLS
        # policy, and there is no user identity to set context for.
        await append_audit_event(
            session,
            org_id=org_id,
            event_type=event_type,
            actor_user_id=None,
            resource_type=resource_type,
            payload_json=payload,
        )


async def _emit_system(
    *,
    session: AsyncSession,
    org_id: uuid.UUID | None,
    event_type: str,
    resource_type: str,
    payload: dict[str, Any],
    fail_closed: bool,
) -> None:
    """Record an actor-less event under the caller's failure policy.

    ``org_id is None`` means the route published no organisation: an audit
    event is org-scoped, so there is nowhere to record it. That is logged and
    skipped rather than raised — a missing attribution must not fail the
    request — and never "fixed" by inventing a tenant.
    """
    log_key = _log_key(event_type)
    if org_id is None:
        _log.warning(
            f"{_LOG_KEY_PREFIX}.{event_type}.no_org_context",
            extra={"event_type": event_type, "resource_type": resource_type},
        )
        return
    try:
        await _append_system_or_raise(
            session,
            org_id=org_id,
            event_type=event_type,
            resource_type=resource_type,
            payload=payload,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        if fail_closed:
            raise
        _log.warning(
            log_key,
            extra={
                "org_id": str(org_id),
                "event_type": event_type,
                "resource_type": resource_type,
            },
            exc_info=True,
        )


def audited_system(
    event_type: str,
    resource_type: str,
    *,
    actor_source: str,
    fail_closed: bool = False,
) -> Callable[..., AsyncGenerator[None, None]]:
    """Dependency factory: record one actor-less audit event after the handler.

    Use it where no request principal exists — pre-auth, unauthenticated and
    signature-verified ingress (the route must publish its org with
    ``bind_audit_org`` — see the module docstring)::

        dependencies=[
            Depends(audited_system("login_attempted", "session", actor_source="pre_auth", fail_closed=True))
        ]

    Args:
        event_type: Chained-audit event type (``audit_events.event_type``).
        resource_type: Coarse resource kind the event is about.
        actor_source: How the request was admitted, from the route's
            perspective at dispatch (``pre_auth`` / ``unauthenticated`` /
            ``signature_verified`` / ...). A route may strengthen it later via
            ``bind_audit_actor_source``. REQUIRED — provenance must be stated.
        fail_closed: Re-raise an append failure instead of logging it.

    Returns:
        An async-generator dependency suitable for ``Depends(...)`` or a
        route's ``dependencies=[...]`` list.
    """
    if not event_type.strip():
        raise ValueError("audited_system() requires a non-empty event_type")
    if not resource_type.strip():
        raise ValueError("audited_system() requires a non-empty resource_type")
    if not actor_source.strip():
        raise ValueError("audited_system() requires a non-empty actor_source")

    async def dependency(
        request: Request,
        session: AsyncSession = Depends(audit_session),
    ) -> AsyncGenerator[None, None]:
        """Record ``event_type`` once the route handler has finished."""

        def _teardown(outcome: AuditOutcome) -> tuple[uuid.UUID | None, dict[str, Any]]:
            # Resolved at TEARDOWN (post-handler), not on entry, so the org and
            # actor_source the handler published during the request are what the
            # event carries.
            return _resolve_audit_org(request), _system_payload(
                request, outcome, _resolve_actor_source(request, actor_source)
            )

        try:
            yield
        except (GeneratorExit, asyncio.CancelledError):
            raise
        except BaseException:
            org_id, payload = _teardown("error")
            await _emit_system(
                session=session,
                org_id=org_id,
                event_type=event_type,
                resource_type=resource_type,
                payload=payload,
                fail_closed=False,
            )
            raise
        else:
            org_id, payload = _teardown("success")
            await _emit_system(
                session=session,
                org_id=org_id,
                event_type=event_type,
                resource_type=resource_type,
                payload=payload,
                fail_closed=fail_closed,
            )

    return dependency
