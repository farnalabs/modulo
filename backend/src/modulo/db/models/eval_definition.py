import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class EvalDefinition(OrgScoped):
    __tablename__ = "eval_definitions"
    __table_args__ = (
        CheckConstraint(
            "eval_type IN ('llm_judge', 'regex', 'json_schema', 'custom_function')",
            name="ck_eval_definitions_type",
        ),
        CheckConstraint(
            "failure_behaviour IN ('warn', 'block')",
            name="ck_eval_definitions_failure_behaviour",
        ),
    )

    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("pipelines.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    node_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    eval_type: Mapped[str] = mapped_column(String(30), nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    failure_behaviour: Mapped[str] = mapped_column(String(10), nullable=False, server_default="warn")
    pass_threshold: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    suite_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
