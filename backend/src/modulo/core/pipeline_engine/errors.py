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
