"""Audit persistence for resolved managed workspace inputs (FAR-801, ADR 033).

This module records which workspace inputs were resolved for a given
(run_id, node_id, attempt_key) in the ``run_node_outputs`` table, keyed
by a dedicated ``AUDIT_NODE_ID`` sentinel.  The audit payload lives in
``outputs_json`` — the same column the retention byte estimator already
accounts for (``crud.run_retention._NODE_OUTPUT_BYTES``).

* :func:`record_resolved_inputs` — persist the initial resolution records.
* :func:`record_drift` — update final SHAs and drift flags after checkout.

Both functions are **best-effort**: a failure is logged and swallowed so
that a successful provision is never converted into a run failure.

The module is intentionally dependency-light (no LangGraph, no settings)
so it can be imported by the provisioning layer and unit-tested without
dragging the full runtime into them.

RLS discipline: the caller MUST set the RLS organisation context on the
session before calling these functions (``set_rls_org(session, org_id)``).
These functions do NOT call ``assert_write_org`` themselves — the
``run_node_outputs`` table's RLS policy enforces at the DB level; these
are best-effort writes that swallow failures anyway.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import ParseResult, urlparse, urlunparse

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.run_node_outputs import dialect_insert, resolve_dialect
from modulo.db.models.run import Run
from modulo.db.models.run_node_outputs import RunNodeOutput

_log = logging.getLogger(__name__)

# Dedicated node id for workspace-input audit rows — reserved ``__``-prefixed
# sentinel namespace, matching ``__run_meta__`` / ``__final__``.
AUDIT_NODE_ID = "__mwi_audit__"


# ---------------------------------------------------------------------------
# Value object
# ---------------------------------------------------------------------------

_VALID_STATUSES = frozenset({"resolved", "provisioned", "failed"})


@dataclass(frozen=True)
class WorkspaceInputAuditRecord:
    """Audit record for one resolved workspace input.

    Fields mirror the ADR 033 resolution output.  ``url_redacted`` MUST
    have userinfo stripped (``user:pass@`` removed) — the ``to_audit_dict``
    method enforces this invariant.
    """

    input_name: str
    connector_instance_id: str | None
    host: str
    url_redacted: str
    requested_ref_kind: str
    requested_ref_value: str
    resolved_sha: str
    final_sha: str | None
    drift_detected: bool
    dest: str
    status: str  # resolved | provisioned | failed

    def __post_init__(self) -> None:
        # Validate status at construction time (frozen dataclass = no __init__).
        if self.status not in _VALID_STATUSES:
            raise ValueError(
                f"WorkspaceInputAuditRecord.status {self.status!r} "
                f"is not valid — expected one of {sorted(_VALID_STATUSES)}"
            )

    def to_audit_dict(self) -> dict[str, Any]:
        """Serialise for persistence — NEVER contains credentials/secrets.

        Userinfo in ``url_redacted`` is stripped defensively (a second
        pass) even though the constructor expects it already stripped.
        """
        return {
            "input_name": self.input_name,
            "connector_instance_id": self.connector_instance_id,
            "host": self.host,
            "url_redacted": _strip_userinfo(self.url_redacted),
            "requested_ref_kind": self.requested_ref_kind,
            "requested_ref_value": self.requested_ref_value,
            "resolved_sha": self.resolved_sha,
            "final_sha": self.final_sha,
            "drift_detected": self.drift_detected,
            "dest": self.dest,
            "status": self.status,
        }


# ---------------------------------------------------------------------------
# URL redaction
# ---------------------------------------------------------------------------


def _strip_userinfo(url: str) -> str:
    """Remove ``user:pass@`` (or ``user@``) from a URL's authority.

    Returns the URL unchanged when there is no userinfo.  Handles both
    ``https://user:pass@host/path`` and ``git@host:path`` (SCP-style,
    which has no authority to strip).
    """
    parsed = urlparse(url)
    if not parsed.username:
        return url
    # Rebuild without userinfo: scheme://host/path?query#frag
    redacted = ParseResult(
        scheme=parsed.scheme,
        netloc=parsed.hostname or "",
        path=parsed.path,
        params=parsed.params,
        query=parsed.query,
        fragment=parsed.fragment,
    )
    return urlunparse(redacted)


def redact_url(url: str) -> str:
    """Public helper — strip userinfo from a URL.  Convenience for callers."""
    return _strip_userinfo(url)


# ---------------------------------------------------------------------------
# Persistence primitives
# ---------------------------------------------------------------------------

# The stored shape in ``outputs_json``:
# ``{"workspace_inputs": [<record.to_audit_dict(), ...>]}``
_PAYLOAD_KEY = "workspace_inputs"


async def record_resolved_inputs(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organisation_id: uuid.UUID,
    node_id: str,
    attempt_key: str,
    records: list[WorkspaceInputAuditRecord],
    status: str,
) -> None:
    """Persist the audit payload for resolved workspace inputs.

    Writes/updates the ``run_node_outputs`` row keyed by
    ``(run_id, AUDIT_NODE_ID, attempt_key)``.  The ``outputs_json``
    column stores the serialised audit list.  Idempotent: a retry for the
    same ``attempt_key`` overwrites the previous payload.

    ``status`` is the overall status of the input resolution (e.g.
    ``"resolved"`` or ``"failed"``).

    Best-effort: a failure is logged and swallowed — never raises.
    The caller MUST set the RLS org context on *session* before calling.
    """
    payload: dict[str, Any] = {
        _PAYLOAD_KEY: [r.to_audit_dict() for r in records],
        "status": status,
        "resolved_for_node_id": node_id,
    }
    try:
        insert_factory = dialect_insert(resolve_dialect(session))
        values: dict[str, Any] = {
            "run_id": run_id,
            "organisation_id": organisation_id,
            "node_id": AUDIT_NODE_ID,
            "attempt_key": attempt_key,
            "outputs_json": payload,
        }
        stmt = insert_factory(RunNodeOutput).values(**values)
        set_: dict[str, Any] = {
            "updated_at": func.current_timestamp(),
            "outputs_json": stmt.excluded.outputs_json,
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=["run_id", "node_id", "attempt_key"],
            set_=set_,
        )
        await session.execute(stmt, [values])
    except Exception:
        _log.exception(
            "workspace_input_audit: record_resolved_inputs failed "
            "(run_id=%s, node_id=%s, attempt_key=%s) — swallowed (best-effort)",
            run_id,
            node_id,
            attempt_key,
        )


async def record_drift(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organisation_id: uuid.UUID,
    node_id: str,
    attempt_key: str,
    final_shas: dict[str, str],
) -> None:
    """Store each input's ``final_sha`` and set ``drift_detected``.

    Reads the existing audit record (if any) from
    ``run_node_outputs``, updates each input's ``final_sha`` and
    ``drift_detected`` (True when ``final_sha != resolved_sha``), and
    writes it back.  When no prior record exists, the function is a
    no-op (the drift check ran after resolution, so this is unexpected
    but not fatal).

    Also writes ``runs.workspace_inputs_drift_detected`` in the SAME
    transaction so the drift flag is visible to terminalization before
    the run completes (FAR-189 ordering guard).

    ``drift_detected`` is recorded as False when there is no drift —
    this distinguishes "no drift" from "drift check never ran".

    Best-effort: a failure is logged and swallowed — never raises.
    The caller MUST set the RLS org context on *session* before calling.
    """
    try:
        # Read existing audit payload.
        row = (
            await session.execute(
                select(RunNodeOutput).where(
                    RunNodeOutput.run_id == run_id,
                    RunNodeOutput.node_id == AUDIT_NODE_ID,
                    RunNodeOutput.attempt_key == attempt_key,
                )
            )
        ).scalar_one_or_none()
        if row is None or not isinstance(row.outputs_json, dict):
            _log.warning(
                "workspace_input_audit: record_drift — no audit record found "
                "(run_id=%s, node_id=%s, attempt_key=%s) — no-op",
                run_id,
                node_id,
                attempt_key,
            )
            return

        existing_payload: dict[str, Any] = row.outputs_json
        audit_list: list[dict[str, Any]] = existing_payload.get(_PAYLOAD_KEY, [])
        updated_list: list[dict[str, Any]] = []
        any_drift = False
        for entry in audit_list:
            entry_name = entry.get("input_name", "")
            if entry_name in final_shas:
                updated_entry = dict(entry)  # shallow copy — frozen-origin dicts
                updated_entry["final_sha"] = final_shas[entry_name]
                updated_entry["drift_detected"] = updated_entry["final_sha"] != entry.get("resolved_sha")
                if updated_entry["drift_detected"]:
                    any_drift = True
                updated_list.append(updated_entry)
            else:
                updated_list.append(entry)

        existing_payload[_PAYLOAD_KEY] = updated_list
        insert_factory = dialect_insert(resolve_dialect(session))
        values = {
            "run_id": run_id,
            "organisation_id": organisation_id,
            "node_id": AUDIT_NODE_ID,
            "attempt_key": attempt_key,
            "outputs_json": existing_payload,
        }
        stmt = insert_factory(RunNodeOutput).values(**values)
        set_: dict[str, Any] = {
            "updated_at": func.current_timestamp(),
            "outputs_json": stmt.excluded.outputs_json,
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=["run_id", "node_id", "attempt_key"],
            set_=set_,
        )
        await session.execute(stmt, [values])

        # Write the first-class drift flag on the runs row in the SAME
        # transaction so terminalization reads the correct value.
        await session.execute(update(Run).where(Run.id == run_id).values(workspace_inputs_drift_detected=any_drift))
    except Exception:
        _log.exception(
            "workspace_input_audit: record_drift failed "
            "(run_id=%s, node_id=%s, attempt_key=%s) — swallowed (best-effort)",
            run_id,
            node_id,
            attempt_key,
        )
