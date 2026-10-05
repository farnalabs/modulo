"""Watchdog (Gatus) config-render and compose-wiring tests.

Where this lives: compose/deploy configuration tests already sit beside their
subject - `tests/unit/test_docker_compose_wiring.py` covers the compose file's
service wiring, `tests/architecture/test_compose_loopback_ports.py` guards its
port bindings - so the render half belongs in ``tests/unit/`` next to the
former. The container-start half (the "quiet" claim, proven against a real
container) lives in ``tests/docker/test_watchdog_container.py``, the repo's
docker-marked suite.

There is no render *script*: Gatus expands ``${VAR}`` in the config file
itself, so ``_render`` below reproduces that expansion (an unset variable
expands to an empty string, which is what Gatus does) and the tests assert on
the YAML that Gatus would then parse.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CONFIG_PATH = _REPO_ROOT / "deploy" / "watchdog" / "config.yaml"
_DOCKERFILE_PATH = _REPO_ROOT / "deploy" / "watchdog" / "Dockerfile"
_COMPOSE_PATH = _REPO_ROOT / "docker-compose.yml"
_PROD_COMPOSE_PATH = _REPO_ROOT / "deploy" / "compose" / "docker-compose.prod.yml"

# The probe target is parameterised in config.yaml (`url: ${WATCHDOG_PROBE_URL}`)
# because the two compose deployments probe different services; the default is
# baked as the image's ENV (Dockerfile) so a bare `docker run` and the root
# compose need no extra configuration. Every real container therefore has this
# variable set - `_render` supplies it for the same reason it supplies the SMTP
# variables: to reproduce the YAML Gatus actually parses.
_IMAGE_DEFAULT_PROBE_URL = "http://backend:8000/healthz/ready"
_PROD_PROBE_URL = "http://modulo:80/healthz/ready"

# The app's SMTP variables - reused verbatim, never renamed (one SMTP setup
# serves HITL email alerts and health alerts).
_SMTP_VARS = (
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "EMAIL_FROM",
    "ALERT_EMAIL_TO",
)

# What docker-compose.yml passes when the operator has set nothing
# (`${SMTP_HOST:-}` etc.), i.e. the supported default state.
_NO_CREDENTIALS = dict.fromkeys(_SMTP_VARS, "")

_CREDENTIALS = {
    "SMTP_HOST": "smtp.example.com",
    "SMTP_PORT": "587",
    "SMTP_USERNAME": "alerts@example.com",
    "SMTP_PASSWORD": "hunter2",
    "EMAIL_FROM": "alerts@example.com",
    "ALERT_EMAIL_TO": "ops@example.com, oncall@example.com",
}


def _render(env: dict[str, str]) -> Any:
    """Reproduce Gatus's config expansion, then parse the result.

    Gatus substitutes ``${VAR}`` in the file *before* YAML parsing (verified on
    the pinned image: an unset ``port: ${SMTP_PORT}`` parses as YAML null, not
    as the literal string), and an unset variable expands to an empty value.
    """
    text = _CONFIG_PATH.read_text(encoding="utf-8")
    for name, value in env.items():
        text = text.replace(f"${{{name}}}", value)
    text = text.replace("${WATCHDOG_PROBE_URL}", _IMAGE_DEFAULT_PROBE_URL)
    unresolved = [name for name in _SMTP_VARS if f"${{{name}}}" in text]
    assert not unresolved, f"config references variables the test did not supply: {unresolved}"
    return yaml.safe_load(text)


def _gatus_would_configure_email(cfg: Any) -> bool:
    """Mirror Gatus's own email-provider validation for the pinned image.

    Observed on twinproduction/gatus:v5.37.0: with empty ``from``/``to`` it
    logs ``Ignoring provider=email due to error=from and to fields are
    required``; with ``from``/``to`` set but no port it logs
    ``port must be between 1 and 65535``. In both cases the container keeps
    running with ``configuredProviders=[]``.
    """
    email = cfg["alerting"]["email"]
    if not email["from"] or not email["to"]:
        return False
    return isinstance(email["port"], int) and 1 <= email["port"] <= 65535


def test_watchdog_config_wires_smtp_env_when_credentials_are_present():
    cfg = _render(dict(_CREDENTIALS))
    email = cfg["alerting"]["email"]

    assert email["from"] == _CREDENTIALS["EMAIL_FROM"]
    assert email["to"] == _CREDENTIALS["ALERT_EMAIL_TO"]
    assert email["host"] == _CREDENTIALS["SMTP_HOST"]
    assert email["port"] == 587
    assert email["username"] == _CREDENTIALS["SMTP_USERNAME"]
    assert email["password"] == _CREDENTIALS["SMTP_PASSWORD"]
    assert _gatus_would_configure_email(cfg) is True


def test_watchdog_config_is_valid_and_alerting_free_without_credentials():
    cfg = _render(dict(_NO_CREDENTIALS))

    # The quiet state must still be a config Gatus can load: yaml.safe_load
    # raising here would mean the container would exit on parse.
    endpoint = cfg["endpoints"][0]
    assert endpoint["url"] == "http://backend:8000/healthz/ready"
    assert _gatus_would_configure_email(cfg) is False


def test_watchdog_config_asserts_body_status_not_just_the_http_status():
    conditions = _render(dict(_CREDENTIALS))["endpoints"][0]["conditions"]

    assert "[STATUS] == 200" in conditions
    assert "[BODY].status == ok" in conditions


def test_watchdog_alerting_thresholds_dedupe_and_mark_recovery():
    alert = _render(dict(_CREDENTIALS))["endpoints"][0]["alerts"][0]

    assert alert["type"] == "email"
    assert alert["failure-threshold"] == 3
    assert alert["success-threshold"] == 2
    assert alert["send-on-resolved"] is True


def test_watchdog_compose_service_starts_independently_of_the_backend():
    compose = yaml.safe_load(_COMPOSE_PATH.read_text(encoding="utf-8"))
    service = compose["services"]["watchdog"]

    # It reports the backend being unhealthy, so it must never wait on it.
    assert "depends_on" not in service
    assert service["restart"] == "unless-stopped"
    assert service["healthcheck"]["test"][0] == "CMD"
    assert set(service["environment"]) == set(_SMTP_VARS)
    assert any("deploy/watchdog/config.yaml" in volume for volume in service["volumes"])


def test_watchdog_compose_port_is_loopback_only():
    compose = yaml.safe_load(_COMPOSE_PATH.read_text(encoding="utf-8"))
    ports = compose["services"]["watchdog"]["ports"]

    assert ports == ["127.0.0.1:8082:8080"]


def test_watchdog_probe_url_is_parameterised_not_hardcoded():
    """The shared config must not pin one deployment's service topology.

    The root compose probes ``backend:8000`` and the production compose probes
    ``modulo:80`` (the all-in-one image puts uvicorn behind nginx on port 80);
    a hardcoded URL would leave exactly one of them probing nothing.
    """
    raw = _CONFIG_PATH.read_text(encoding="utf-8")
    url_lines = [line.strip() for line in raw.splitlines() if line.strip().startswith("url:")]

    assert url_lines == ["url: ${WATCHDOG_PROBE_URL}"]


def test_watchdog_image_bakes_the_default_probe_url():
    """An unset ``${WATCHDOG_PROBE_URL}`` panics the container (no default
    syntax in Gatus), so the image ENV is what keeps a bare ``docker run`` and
    the root compose working."""
    dockerfile = _DOCKERFILE_PATH.read_text(encoding="utf-8")
    assert f"WATCHDOG_PROBE_URL={_IMAGE_DEFAULT_PROBE_URL}" in dockerfile


def test_watchdog_prod_compose_wiring_matches_the_root_service():
    """The production compose carries the same watchdog, reusing deploy/watchdog/.

    Same six SMTP variables, enabled by default (no profile), no ``depends_on``
    (it reports the app being unhealthy), and the prod-specific probe target.
    """
    compose = yaml.safe_load(_PROD_COMPOSE_PATH.read_text(encoding="utf-8"))
    service = compose["services"]["watchdog"]

    assert "profiles" not in service, "watchdog must be enabled by default"
    assert "depends_on" not in service
    assert service["restart"] == "unless-stopped"
    assert service["build"] == "../watchdog", "must build the shared context, not a copy"
    assert set(service["environment"]) == set(_SMTP_VARS) | {"WATCHDOG_PROBE_URL"}
    assert service["environment"]["WATCHDOG_PROBE_URL"] == _PROD_PROBE_URL
    assert any("../watchdog/config.yaml" in volume for volume in service["volumes"])
    assert service["healthcheck"]["test"][0] == "CMD"


def test_watchdog_prod_compose_port_is_loopback_only():
    compose = yaml.safe_load(_PROD_COMPOSE_PATH.read_text(encoding="utf-8"))
    ports = compose["services"]["watchdog"]["ports"]

    assert ports == ["127.0.0.1:8083:8080"]


def test_watchdog_image_drops_root_in_the_final_stage():
    """The runtime container must not run as root (Sonar docker:S6471).

    The alpine base defaults to root; an unprivileged ``USER`` in the final
    stage is what keeps the image out of that finding, so guard it structurally
    rather than relying on a one-off fix.
    """
    final_stage: list[str] = []
    for line in _DOCKERFILE_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("FROM "):
            final_stage = []
        final_stage.append(stripped)

    user_directives = [line for line in final_stage if line.startswith("USER ")]
    assert user_directives, "final image stage must drop root with a USER directive"
    last_user = user_directives[-1].split(None, 1)[1].strip()
    assert last_user not in {"root", "0", "root:root"}, f"final stage still runs as {last_user}"


def test_watchdog_image_bases_are_pinned_to_specific_tags():
    from_lines = [
        line.split()[1]
        for line in _DOCKERFILE_PATH.read_text(encoding="utf-8").splitlines()
        if line.startswith("FROM ")
    ]

    assert from_lines, "Dockerfile must declare its base images"
    for ref in from_lines:
        assert ":" in ref, f"base image is untagged: {ref}"
        tag = ref.rsplit(":", 1)[1]
        assert tag not in {"latest", "stable"}, f"base image floats on a moving tag: {ref}"
