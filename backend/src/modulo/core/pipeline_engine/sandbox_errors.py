"""Sandbox exception types (FAR-1085).

Lightweight module — no heavy imports (langgraph, e2b, langchain_core).
The API layer and other modules that need to catch these exceptions can
import from here without pulling in the full node_runner dependency tree.
"""


class SandboxNodeFailedError(Exception):
    """A sandbox-agent node failed due to sandbox infrastructure (retryable).

    Raised for a stall (idle watchdog), a command timeout, or a non-zero exit
    code with no parseable ``output.json``. The executor maps this to the
    retryable path (fenced reset to ``pending`` + SAQ retry) instead of a
    silent wrong-success completion.

    ``node_id`` is carried so the executor's FAR-228 idempotency gate (guard B)
    can resolve which node failed without re-deriving it from the message.
    Omitting ``node_id`` (e.g. ``SandboxNodeFailedError("msg")`` in tests)
    disables guard B — the transient retry proceeds exactly as before.
    """

    def __init__(self, message: str = "", *, node_id: str | None = None) -> None:
        super().__init__(message)
        self.node_id = node_id


class SandboxTierRefusedError(SandboxNodeFailedError):
    """A provider tier refused this dispatch at provision time (FAR-592 D6).

    Terminal (D7-refusal posture): the tier cannot safely honour the dispatch,
    so it refuses rather than failing open. Raised by two call sites:

    * The Local (host-subprocess) tier, when bindings inject standing host-env
      credentials and the profile lacks ``allow_runner_env_bindings`` — the
      Local tier has no container isolation, so it MUST refuse.
    * The Docker / Bundled Runner tier, when the profile requests
      ``network_policy='selected'`` — Docker cannot enforce per-host egress
      allowlists (no ``NET_ADMIN`` in the hardened workspace), so it MUST
      refuse instead of silently granting full outbound (FAR-1064).

    Maps to ``sandbox.tier_refused`` via the executor's LEGACY_ALIASES.
    """
