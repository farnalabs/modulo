"""Architecture test: every shipped Compose Redis is bounded and evicts safely.

FAR-2002. The pre-fix default set no ``maxmemory``, so Redis ran ``noeviction``:
under a worker wedge the SAQ queue grew until enqueue writes failed. The bound
and the policy are now part of the shipped artefact, and this test makes a
regression (a new Compose file, or a dropped flag) fail CI rather than surface
as a production OOM.

``volatile-lru`` is load-bearing, not cosmetic: it evicts ONLY keys carrying a
TTL. In the SAQ key space those are job hashes (queued ones are TTL'd by
``core.dispatch``), which ``dispatcher_reconcile`` can re-dispatch. The queue's
structural keys (``queued``/``active`` lists, ``incomplete`` zset) are
persistent, so ``allkeys-lru`` could evict queued work with no job hash left to
detect — see docs/configuration-reference.md § Redis & Task Queue.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# Repo root (three levels up from this test file: tests/architecture/ → tests/ → backend/ → repo root).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

# Every shipped Compose file that defines the Redis the queue runs on.
_COMPOSE_REDIS: list[tuple[Path, str]] = [
    (_REPO_ROOT / "docker-compose.yml", "redis"),
    (_REPO_ROOT / "docker-compose.local.yml", "redis-local"),
    (_REPO_ROOT / "deploy" / "compose" / "docker-compose.prod.yml", "redis"),
]


def _redis_command(path: Path, service: str) -> list[str]:
    with path.open(encoding="utf-8") as fh:
        compose = yaml.safe_load(fh)
    service_def = compose["services"][service]
    command = service_def.get("command")
    assert isinstance(command, list), (
        f"{path.relative_to(_REPO_ROOT)} services.{service}.command must be the list form "
        "(a shell-form string would not pin the flags reliably)"
    )
    return [str(part) for part in command]


@pytest.mark.parametrize(
    ("compose_path", "service"),
    _COMPOSE_REDIS,
    ids=[f"{p.relative_to(_REPO_ROOT)}:{s}" for p, s in _COMPOSE_REDIS],
)
def test_shipped_redis_has_a_maxmemory_bound(compose_path: Path, service: str) -> None:
    """A shipped Redis without maxmemory is the FAR-2002 unbounded default."""
    command = _redis_command(compose_path, service)
    assert "--maxmemory" in command, (
        f"{compose_path.relative_to(_REPO_ROOT)} services.{service} sets no --maxmemory; "
        "Redis would run noeviction and fail enqueue writes once the host fills (FAR-2002)"
    )
    maxmemory = command[command.index("--maxmemory") + 1]
    assert maxmemory.startswith("${REDIS_MAXMEMORY:-"), (
        f"{compose_path.relative_to(_REPO_ROOT)} services.{service} must default the bound while "
        f"allowing an operator override, got {maxmemory!r}"
    )


@pytest.mark.parametrize(
    ("compose_path", "service"),
    _COMPOSE_REDIS,
    ids=[f"{p.relative_to(_REPO_ROOT)}:{s}" for p, s in _COMPOSE_REDIS],
)
def test_shipped_redis_policy_only_evicts_repairable_keys(compose_path: Path, service: str) -> None:
    """The policy must be ``volatile-*`` — never an ``allkeys-*`` policy.

    ``allkeys-lru`` / ``allkeys-random`` can evict the queue's structural list
    and zset keys, which the reconciler cannot detect or repair.
    """
    command = _redis_command(compose_path, service)
    assert "--maxmemory-policy" in command, (
        f"{compose_path.relative_to(_REPO_ROOT)} services.{service} sets no --maxmemory-policy "
        "(Redis's default policy is noeviction)"
    )
    policy = command[command.index("--maxmemory-policy") + 1]
    assert policy.startswith("volatile-"), (
        f"{compose_path.relative_to(_REPO_ROOT)} services.{service} uses policy {policy!r}; "
        "only a volatile-* policy confines eviction to TTL-bearing job hashes the reconciler "
        "can re-dispatch (FAR-2002)"
    )
