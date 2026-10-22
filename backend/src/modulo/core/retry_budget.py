"""The ONE retry budget — execution ceiling, consumed count, transport clamp (FAR-2076).

A run must be executed **at most ``1 + retry_policy.max_retries`` times**. Before
this module every path that can grant an execution counted a *different* thing:
the run-level policy path counted ``node_attempt_count``, the watchdog counted
``max(node_attempt_count, claim_count - 1)``, the nodeless repair re-derived the
same expression by hand, the SAQ transport ignored the policy entirely, and the
``dispatcher_reconcile`` re-enqueue backstop counted nothing. A pipeline with
``max_retries: 2`` therefore reached a fourth execution.

The authority for "executions used" is ``runs.execution_attempt_count``, a
durable counter incremented exactly once per **execution attempt of the graph**
(``execute_run`` that passes the capacity check). It is NEVER incremented for a
``resume_run`` claim (a checkpoint continuation, not a graph execution) and
NEVER for a capacity-demoted claim (the attempt did not begin). A pre-node death
(checkpointer setup, graph compile, connector-hub init) DOES count — the
attempt was made.

Zero-import leaf module
-----------------------
This module imports nothing, deliberately. It is imported by ``dispatch``,
``cron_helpers``, ``worker_handoff``, the executor, the watchdog and the graph
validator — including modules that must not transit LangGraph (the
``api -> langgraph`` import-linter contract) or take part in the
executor <-> graph_validator cycle. A leaf with no imports can be imported from
any of them, and it means the bound below has exactly ONE definition in the
tree: a validator that raised its own copy of the bound would make every stored
policy read as malformed and silently disable every ceiling.
"""

#: Upper bound a ``retry_policy.max_retries`` may take.
#:
#: The validator (``graph_validator``), the runtime policy reader and every
#: budget consumer share THIS value — never a second hard-coded copy. Raising
#: the bound is a one-line change here; a stored policy above it is treated as
#: malformed (fail closed) by :func:`retry_policy_max_retries`.
RETRY_POLICY_MAX_RETRIES = 5


def _non_negative_int(value: object) -> int:
    """Coerce a possibly-None / non-int counter to a non-negative int."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def retry_policy_max_retries(policy: object) -> int | None:
    """The policy's validated ``max_retries``, or ``None`` when it imposes no budget.

    ``None`` means "there is no policy at all" — an absent (``None``) or empty
    (``{}``) policy — which preserves the pre-FAR-2076 behaviour of no ceiling.

    A NON-EMPTY policy always yields an int, because a policy that exists is a
    budget:

    * an absent / explicitly-``null`` ``max_retries`` → ``0``. The run-level
      path has always treated an absent budget as "no retries" (the API accepts
      and persists ``{"on": ["failure"]}``), so ``0`` is the truthful reading.
      This closes the fail-open where such a policy returned ``None`` and
      granted an unbounded number of executions.
    * a malformed / out-of-range value (bool, non-int, negative, above
      :data:`RETRY_POLICY_MAX_RETRIES`) → ``0``. Fail closed: a value the
      validator would reject must never *widen* a budget.
    * an explicit ``0`` → ``0`` (the policy really did authorise zero retries).
    """
    if not isinstance(policy, dict):
        # No policy at all. A non-dict is a caller/shape error, not a budget:
        # preserve the historical "no ceiling" behaviour rather than inventing
        # a one-execution ceiling for a value we cannot interpret.
        return None
    if not policy:
        return None
    max_retries = policy.get("max_retries")
    if isinstance(max_retries, bool) or not isinstance(max_retries, int):
        return 0
    if not 0 <= max_retries <= RETRY_POLICY_MAX_RETRIES:
        return 0
    return max_retries


def retry_max_executions(policy: object) -> int | None:
    """Execution ceiling a ``retry_policy`` imposes (``1 + max_retries``), or ``None``.

    ``None`` means "no policy, no ceiling": an absent or empty policy, or a
    non-dict. Every non-empty policy imposes a ceiling — ``max_retries: 0``
    (and an absent budget, normalised to ``0``) yields ``1``: the run may
    execute once and nothing may grant it a second execution.

    This is the ceiling whichever path grants an execution. It deliberately
    does NOT consult the policy's ``on`` events: a path that has already
    matched an event (the policy re-dispatch, the watchdog) keeps its
    event-specific budget; this ceiling only answers "how many executions can
    this policy ever authorise".
    """
    max_retries = retry_policy_max_retries(policy)
    if max_retries is None:
        return None
    return max_retries + 1


def retry_executions_consumed(*, execution_attempt_count: object = 0) -> int:
    """How many executions of the graph this run has already consumed.

    The SINGLE measure every granting path compares against its ceiling
    (``runs.execution_attempt_count``). Never derived from ``claim_count``
    (which grows on capacity-demoted and resume claims — non-executions) and
    never from ``node_attempt_count`` (which grows only once a node-level
    attempt begins, so a pre-node death records nothing).
    """
    return _non_negative_int(execution_attempt_count)


def retry_budget_exhausted(
    policy: object,
    *,
    execution_attempt_count: object = 0,
) -> bool:
    """Has the policy's execution ceiling already been reached (FAR-2076)?

    ``False`` when the policy imposes no ceiling (absent / empty policy),
    preserving existing behaviour. A run at exactly ``1 + max_retries``
    executions is exhausted: no further execution may be granted.
    """
    ceiling = retry_max_executions(policy)
    if ceiling is None:
        return False
    return retry_executions_consumed(execution_attempt_count=execution_attempt_count) >= ceiling


def clamp_saq_retries(
    policy: object,
    *,
    execution_attempt_count: object = 0,
    configured: int,
) -> int:
    """Bound the SAQ transport retry allowance by the policy budget (FAR-2076).

    SAQ's ``retries`` is the number of RE-attempts after the first execution,
    so a job enqueued with ``retries=N`` can execute a run up to ``N + 1``
    times. Left at ``settings.saq_run_retries`` (5) it silently outran a
    pipeline's ``retry_policy.max_retries`` (2): the transport granted
    executions the policy had already refused.

    Returns the configured value unchanged when the policy imposes no ceiling
    (existing behaviour preserved), else ``min(configured, remaining - 1)``
    where ``remaining`` is the executions the ceiling still permits including
    the one this enqueue creates. Never negative: a run already at or beyond
    its ceiling is enqueued with zero transport retries (the reconcile
    backstop terminal-fails that shape instead of re-dispatching it).
    """
    ceiling = retry_max_executions(policy)
    if ceiling is None:
        return configured
    remaining = ceiling - retry_executions_consumed(execution_attempt_count=execution_attempt_count)
    return max(0, min(configured, remaining - 1))
