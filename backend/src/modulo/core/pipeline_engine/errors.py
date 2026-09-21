"""Pipeline-engine-specific exception types.

Kept in a leaf module (no internal pipeline_engine imports) so both
``node_runner`` and ``executor`` can raise/catch without a circular import.
"""

from __future__ import annotations


class RouterNoMatchError(Exception):
    """Raised by a Router node's routing function when no rule matches and
    there is no ``default`` rule.

    The executor catches this specifically and terminalizes the run with the
    ``router_no_match`` status (a terminal, non-failure status) rather than
    letting it bubble up as an unclassified ``failed``.
    """

    def __init__(self, node_id: str | None = None, detail: str | None = None) -> None:
        self.node_id = node_id
        message = "Router node"
        if node_id:
            message = f"Router node {node_id!r}"
        message += " found no matching rule and no default target"
        if detail:
            message += f": {detail}"
        super().__init__(message)


class NodeMissingModelBackendError(Exception):
    """Raised when a model-backed node (agent_id present) resolves to no model backend.

    An agent node is model-backed by intent: the operator configured an agent
    for it.  When that agent's ``model_backend_id`` is null (or the resolved
    backend was removed), the node must NOT silently return the stub — that
    masks a real misconfiguration as a successful execution with zero tokens.

    The executor catches this and terminalizes the run with the
    ``config.missing_model_backend`` error code.
    """

    def __init__(self, node_id: str, agent_id: str) -> None:
        self.node_id = node_id
        self.agent_id = agent_id
        super().__init__(
            f"Agent node {node_id!r} references agent {agent_id!r} "
            f"which has no model_backend_id configured. "
            f"Assign a model backend to the agent, or use a "
            f"connector/manual node type if no model call is intended."
        )
