"""FAR-2002 - the Helm chart's embedded Redis is bounded and evicts safely.

The Compose files were fixed first; the Helm chart still passed
``--maxmemory-policy allkeys-lru``, which can evict the SAQ queue's structural
keys (the ``queued`` list and the ``incomplete`` zset). Nothing repairs a
missing structural key, so that is a stranded run - the same failure mode the
FAR-2002 Step-0 evidence rejected for ``volatile-lru``.

The chart must keep a ``maxmemory`` bound and a ``volatile-*`` policy. This is
the Helm sibling of ``tests/architecture/test_compose_redis_memory_bound.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_TEMPLATE = _REPO_ROOT / "deploy" / "helm" / "modulo" / "templates" / "redis-deployment.yaml"


def _flagged_value(flag: str) -> str:
    text = _TEMPLATE.read_text(encoding="utf-8")
    match = re.search(rf'"{re.escape(flag)}"\s+"([^"]+)"', text)
    assert match is not None, f"{_TEMPLATE.name} passes no {flag} to redis-server"
    return match.group(1)


def test_helm_redis_has_a_maxmemory_bound() -> None:
    assert _flagged_value("--maxmemory").endswith(("mb", "gb"))


def test_helm_redis_policy_only_evicts_ttl_bearing_keys() -> None:
    policy = _flagged_value("--maxmemory-policy")
    assert policy.startswith("volatile-"), (
        f"deploy/helm/modulo/templates/redis-deployment.yaml uses policy {policy!r}; "
        "an allkeys-* policy can evict the queue's structural list/zset, which nothing "
        "repairs (FAR-2002)"
    )
