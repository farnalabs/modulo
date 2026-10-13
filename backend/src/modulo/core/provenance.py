"""The governed-run PROVENANCE TAXONOMY — ONE vocabulary for how Modulo knows a fact.

FAR-1693 / ADR-042 (unified taxonomy ratified 2026-10-12).

Two vocabularies used to describe the same idea — *how we know* — and drifted
apart:

* the **dispatch arm** stamped ``witnessed_via: <connector instance>`` on a
  dispatched node's output and left the *class* implicit; and
* the **agent-facts arm** (ADR-055) named only ``observed`` / ``reported``.

The unification replaces that split with one closed four-value taxonomy::

    observed  Modulo observed it through a credential it already holds.
    derived   Modulo computed it from observed facts.
    reported  arrived from ANY external surface — including a dispatched
              runtime — and is stamped ``witnessed_via: <connector/channel>``.
    attested  governance events Modulo records natively: approvals, guardrail
              verdicts, HITL decisions.

``witnessed_via`` is therefore **not** a class — it is the CHANNEL STAMP of a
``reported`` datum. The dispatch arm's marker folds into the taxonomy as
"``reported``, witnessed via <connector>", which is what this module
single-sources.

**Binding foreclosure (FAR-1693).** ``attested`` never implies the substrate's
data is true — substrate data stays ``reported``. No governed badge (HITL
passed, guardrails enforced) implies anything about a customer's CI internals.

**Engine-assigned, never self-asserted.** The wire never asserts a fact's class
(ADR-034's rule, reused): every writer of a class value must OVERWRITE any
incoming value, so a prompt-injected node output or a hostile substrate result
cannot claim a stronger provenance than the engine gives it.
"""

from __future__ import annotations

from typing import Final

from modulo.db.models.run import EXECUTION_ORIGIN_DISPATCHED

# ---------------------------------------------------------------------------
# The closed taxonomy
# ---------------------------------------------------------------------------

PROVENANCE_OBSERVED: Final[str] = "observed"
PROVENANCE_DERIVED: Final[str] = "derived"
PROVENANCE_REPORTED: Final[str] = "reported"
PROVENANCE_ATTESTED: Final[str] = "attested"

#: The closed set of provenance classes. A future class is added here first.
PROVENANCE_VALUES: Final[frozenset[str]] = frozenset(
    {
        PROVENANCE_OBSERVED,
        PROVENANCE_DERIVED,
        PROVENANCE_REPORTED,
        PROVENANCE_ATTESTED,
    }
)

#: The wire / output key carrying a datum's provenance class.
PROVENANCE_KEY: Final[str] = "provenance"

#: The channel-stamp key carried alongside the ``reported`` class — the
#: connector instance or channel the externally-sourced datum arrived through.
WITNESSED_VIA_KEY: Final[str] = "witnessed_via"

#: The class of EVERY datum a dispatch node folds back from the customer's
#: substrate — the substrate's status, logs, returned artifacts, job list. It is
#: ``reported``: it is the substrate's report, not Modulo's measurement. Never
#: ``attested`` — that would imply the substrate's data is true (foreclosed).
DISPATCH_DATUM_PROVENANCE: Final[str] = PROVENANCE_REPORTED


def execution_origin_provenance_class(origin: str | None) -> str | None:
    """The class of a run's externally-sourced data, from its ``execution_origin``.

    * ``'dispatched'`` -- the run's graph contains a ``dispatch`` node, so part
      of its work ran outside Modulo and the data folded back is ``reported``.
    * ``None`` -- executed by Modulo, or recorded before provenance shipped:
      there is no externally-sourced datum to type, so the class is ``None``.
      Deliberately NOT ``observed`` — a legacy row's provenance was never
      recorded, and claiming ``observed`` for it would over-claim (ADR-042's
      whole purpose is to make silent over-claiming impossible).
    """
    if origin == EXECUTION_ORIGIN_DISPATCHED:
        return PROVENANCE_REPORTED
    return None


__all__ = [
    "DISPATCH_DATUM_PROVENANCE",
    "PROVENANCE_ATTESTED",
    "PROVENANCE_DERIVED",
    "PROVENANCE_KEY",
    "PROVENANCE_OBSERVED",
    "PROVENANCE_REPORTED",
    "PROVENANCE_VALUES",
    "WITNESSED_VIA_KEY",
    "execution_origin_provenance_class",
]
