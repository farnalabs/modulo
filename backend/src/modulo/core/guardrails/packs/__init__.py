"""Guardrail policy pack content (FAR-216 PR B).

Each submodule ships the concrete controls and guardrail mappings for one
compliance bundle (SOC 2, GDPR, ...). The pack framework (validation, CI gate,
warn-mode-first rollout) lives in :mod:`modulo.core.guardrails.policy_pack`
(FAR-216 PR A) — this package is content only, never framework.
"""

from __future__ import annotations

from modulo.core.guardrails.packs.soc2 import SOC2_PACK, build_soc2_pack

__all__ = ["SOC2_PACK", "build_soc2_pack"]
