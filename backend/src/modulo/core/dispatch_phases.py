"""FAR-1088 / FAR-1649: the dispatch-phase label vocabulary — dependency-free.

The phase labels written to ``runs.dispatch_phase`` (migration 0273) are a
shared vocabulary: the executor stamps them (``pipeline_execution``'s tracker),
the SAQ worker stamps the claim/setup legs, and the reconcile machinery in
``cron_helpers`` branches on the durable first-node-start label.

They therefore MUST live in a module every one of those consumers can import
without dragging in the others: ``pipeline_execution`` reaches ``langgraph`` at
module level, and ``cron_helpers`` is an API-facing seam (import-linter forbids
the API layer transitively importing langgraph). Before FAR-1649 the labels
lived in ``pipeline_execution``, so ``cron_helpers`` could only reach
``PHASE_FIRST_NODE_DISPATCHED`` through a lazy import + an import-linter
whitelist entry coupling the runner-capacity safety sweep to langgraph. This
module is a leaf — no imports at all — so both sides import the ONE definition
and the raw-SQL / ORM spellings cannot desynchronise.

``pipeline_execution`` re-exports these names (``from
modulo.core.dispatch_phases import PHASE_*``), so every existing
``from modulo.core.pipeline_execution import PHASE_*`` import keeps working.
"""

from __future__ import annotations

# Named phases the executor passes through between claim and first node
# dispatch. The tracker is shared between execute_run /
# run_executor_with_watchdog (the claim+setup side) and the executor (the
# dispatch side). When the zombie watchdog fires, it reads the tracker to
# report WHERE the executor is stuck — not just THAT it is stuck.
PHASE_NOT_STARTED = "not_started"
PHASE_CLAIMED = "claimed"
PHASE_LOADING_SETUP = "loading_setup"
PHASE_SETUP_COMPLETE = "setup_complete"
PHASE_EXECUTOR_RUNNING = "executor_running"
PHASE_LOADING_CONTEXT = "loading_context"
PHASE_CAPACITY_CHECK = "capacity_check"
PHASE_SPEND_CHECK = "spend_check"
PHASE_INIT_ENV = "init_env"
PHASE_GRAPH_COMPILE = "graph_compile"
PHASE_STREAMING = "streaming"
# FAR-1422: makes the pre-node vs node-started transition durable — a run that
# STARTED a node then hung persists ``first_node_dispatched`` instead of
# looking like it never dispatched anything. FAR-1649: this is also the
# "a node MAY have executed side effects" marker every reconcile re-dispatch
# decision must exclude (a re-dispatch would re-execute the started node).
PHASE_FIRST_NODE_DISPATCHED = "first_node_dispatched"

__all__ = [
    "PHASE_CAPACITY_CHECK",
    "PHASE_CLAIMED",
    "PHASE_EXECUTOR_RUNNING",
    "PHASE_FIRST_NODE_DISPATCHED",
    "PHASE_GRAPH_COMPILE",
    "PHASE_INIT_ENV",
    "PHASE_LOADING_CONTEXT",
    "PHASE_LOADING_SETUP",
    "PHASE_NOT_STARTED",
    "PHASE_SETUP_COMPLETE",
    "PHASE_SPEND_CHECK",
    "PHASE_STREAMING",
]
