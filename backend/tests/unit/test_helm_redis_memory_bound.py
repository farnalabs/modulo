"""FAR-2002 - the Helm chart's embedded Redis is bounded and authenticated.

The chart must keep a ``maxmemory`` bound and a ``volatile-*`` policy.  When
embedded Redis is enabled it must also require a password and pass that
password to Redis through a shell-expanded environment variable, while the
same password is used in ``REDIS_URL``.
"""

from __future__ import annotations

import base64
import re
import shutil
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_CHART = _REPO_ROOT / "deploy" / "helm" / "modulo"
_TEMPLATE = _CHART / "templates" / "redis-deployment.yaml"
_HELPERS = _CHART / "templates" / "_helpers.tpl"
_README = _REPO_ROOT / "deploy" / "helm" / "modulo" / "README.md"


def _template_text() -> str:
    return _TEMPLATE.read_text(encoding="utf-8")


def _flagged_value(flag: str) -> str:
    text = _template_text()
    match = re.search(rf'"{re.escape(flag)}"\s+"([^"]+)"', text)
    assert match is not None, f"{_TEMPLATE.name} passes no {flag} to redis-server"
    return match.group(1)


def _helm_template(values: dict[str, str]) -> str | None:
    helm = shutil.which("helm")
    if helm is None:
        return None
    command = [
        helm,
        "template",
        "redis-auth-test",
        str(_CHART),
        "--set",
        "backend.env.SECRET_KEY=test-secret-key-012345678901234567890123",
        "--set",
        "backend.env.FERNET_KEY=test-fernet-key-012345678901234567890123",
        "--set",
        "redis.embedded=true",
    ]
    command.extend(f"--set-string={key}={value}" for key, value in values.items())
    result = subprocess.run(  # noqa: S603
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result.stdout


def test_helm_redis_has_a_maxmemory_bound() -> None:
    assert _flagged_value("--maxmemory").endswith(("mb", "gb"))


def test_helm_embedded_redis_auth_runtime_wiring() -> None:
    text = _template_text()
    helpers = _HELPERS.read_text(encoding="utf-8")

    assert 'required "redis.password or redis.existingSecret is required when redis.embedded=true"' in text
    assert 'command: ["sh", "-c"]' in text
    assert 'exec redis-server --maxmemory 256mb --maxmemory-policy volatile-lru --requirepass "$REDIS_PASSWORD"' in text
    assert "name: REDIS_PASSWORD" in text
    assert "key: REDIS_PASSWORD" in text
    assert "$(REDIS_PASSWORD)" not in text
    assert "REDIS_URL" in helpers
    assert "modulo.redisPassword" in helpers
    assert 'printf "redis://:%s@%s-redis:6379/%d"' in helpers

    rendered = _helm_template({"redis.password": "runtime-test-password"})
    if rendered is not None:
        assert 'command: ["sh", "-c"]' in rendered
        assert '--requirepass "$REDIS_PASSWORD"' in rendered
        assert "runtime-test-password" not in rendered
        assert "REDIS_PASSWORD" in rendered

        # The REDIS_URL in the Secret is base64-encoded. Extract it, decode, and verify.
        import yaml

        redis_url_b64 = None
        for doc in yaml.safe_load_all(rendered):
            if doc and doc.get("kind") == "Secret":
                redis_url_b64 = doc.get("data", {}).get("REDIS_URL")
                break
        assert redis_url_b64 is not None, "REDIS_URL not found in rendered Secret"
        redis_url_plain = base64.b64decode(redis_url_b64).decode()
        assert redis_url_plain == "redis://:runtime-test-password@redis-auth-test-modulo-redis:6379/0"

        assert base64.b64encode(b"runtime-test-password").decode() in rendered
    else:
        assert shutil.which("helm") is None


def test_helm_readme_local_kind_command_supplies_redis_auth() -> None:
    readme = _README.read_text(encoding="utf-8")
    local_kind = readme.split("## Local Development (kind)", maxsplit=1)[1].split("## Upgrade", maxsplit=1)[0]

    assert "--set redis.embedded=true" in local_kind
    assert "--set redis.password=replace-with-a-development-password" in local_kind


def test_helm_redis_policy_only_evicts_ttl_bearing_keys() -> None:
    policy = _flagged_value("--maxmemory-policy")
    assert policy.startswith("volatile-"), (
        f"{_TEMPLATE.name} uses policy {policy!r}; an allkeys-* policy can evict the queue's "
        "structural list/zset, which nothing repairs (FAR-2002)"
    )
