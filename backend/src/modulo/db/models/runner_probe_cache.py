"""RunnerProbeCache — cached per-(org, machine) runner health probe (FAR-591, D5).

One row per (organisation_id, machine_id): the per-machine system-cron
health probe (60s cadence per ADR 029) writes the result of probing the
deployment's Docker engine THROUGH the socket-proxy — engine reachability,
the pinned runner image presence, and the engine's reported resources
(``/info`` CPU/memory, consumed by the engine-resource preflight).

The Runners page (D5) and the node editor read this cache — NEVER a
synchronous probe on the request path. Per-machine rows keep multi-machine
deployments honest (one shared row would collapse per-machine engine
health); the status strip aggregates worst-of across the org's rows and
renders ``status unknown (last checked Xs ago)`` when the cached result is
older than the staleness threshold (2x the probe interval).
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class RunnerProbeCache(OrgScoped):
    __tablename__ = "runner_probe_cache"
    __table_args__ = (
        UniqueConstraint(
            "organisation_id",
            "machine_id",
            name="uq_runner_probe_cache_org_machine",
        ),
    )

    machine_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    engine_reachable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    #: Worst-of image presence across the org's pinned runner images for this
    #: machine. ``None`` means the engine was unreachable (image state unknown).
    images_present: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: Per-image presence detail: ``{"<image_ref>": true|false}``.
    image_checks_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    #: Engine ``/info`` resources: ``{"cpu_count": int, "mem_total_mb": int}``.
    engine_info_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    probe_error: Mapped[str | None] = mapped_column(String(500))
    #: The probe timestamp itself (``updated_at`` moves on ANY column write).
    probed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
