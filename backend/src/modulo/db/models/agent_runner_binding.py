import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class AgentRunnerBinding(OrgScoped):
    """Per-agent Model Backend env-var binding (FAR-592 / D6).

    At provision time each binding injects one decrypted credential field of
    ``model_backend_id`` into the runner env under ``target_env_var`` (profile
    secrets < runner bindings < node ``env_vars_extra`` — the node wins).

    The ``model_backends`` FK is RESTRICT at the DB level so a bound backend
    cannot be deleted while any binding references it (the CRUD pre-delete
    inventory shows the operator the count; the FK stays the race-proof
    backstop). ``agents`` cascades: deleting the agent removes its bindings.
    """

    __tablename__ = "agent_runner_bindings"
    __table_args__ = (
        UniqueConstraint(
            "organisation_id",
            "agent_id",
            "target_env_var",
            name="uq_agent_runner_bindings_org_agent_target",
        ),
    )

    agent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    model_backend_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("model_backends.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    target_env_var: Mapped[str] = mapped_column(String(128), nullable=False)
    source_field: Mapped[str] = mapped_column(String(64), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    # Bindings live and die with their agent: no soft delete.
