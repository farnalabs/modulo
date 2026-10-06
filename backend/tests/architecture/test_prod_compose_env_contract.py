"""Architecture test: the production compose speaks Settings' env dialect.

FAR-1506. ``deploy/compose/docker-compose.prod.yml`` shipped passing
``MODULO_SECRET_KEY`` and ``MODULO_REDIS_URL`` - names no code reads - and did
not pass ``FERNET_KEY`` at all. ``Settings`` reads ``SECRET_KEY``,
``FERNET_KEY`` (both required, no default) and ``REDIS_URL``, so a fresh
bring-up raised a ``ValidationError`` at boot and crash-looped the backend,
while the Redis URL silently fell back to ``redis://localhost:6379/0``. A
``docker compose config`` parse cannot catch this class (it exited 0 on the
broken file), so the contract is asserted structurally here.

Four independent checks, each of which fails on the pre-fix file:

1. every env var the compose passes to the application is a name the app
   actually reads (a ``Settings`` env name, or - for the handful read straight
   from ``os.environ`` - a name whose read site exists in the source);
2. every ``Settings`` field with NO default is satisfied by the compose;
3. the compose's environment, resolved the way compose resolves it, actually
   constructs ``Settings`` - i.e. the deployment boots;
4. ``.env.prod.example`` documents the two required secrets.

Run from ``backend/``: ``uv run pytest tests/architecture/test_prod_compose_env_contract.py``.
"""

from __future__ import annotations

import base64
import os
import re
from pathlib import Path

import pytest
import yaml

from modulo.settings import Settings

# tests/architecture/ -> tests/ -> backend/ -> repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_BACKEND_ROOT = _REPO_ROOT / "backend"
_PROD_COMPOSE = _REPO_ROOT / "deploy" / "compose" / "docker-compose.prod.yml"
_ENV_EXAMPLE = _REPO_ROOT / ".env.prod.example"

#: The service in the prod compose that runs the application image. Only its
#: ``environment`` reaches the backend (postgres/redis/watchdog are separate).
_APP_SERVICE = "modulo"

#: Env vars the application reads straight from ``os.environ`` rather than
#: through ``Settings`` - with the source files that read them, asserted to
#: exist and to mention the name, so an allowlist entry cannot outlive the code
#: that justifies it. Anything else the compose passes MUST be a Settings name.
_NON_SETTINGS_ENV: dict[str, tuple[str, ...]] = {
    "DATABASE_ADMIN_URL": (
        "src/modulo/db/bootstrap.py",
        "src/modulo/db/migrations/env.py",
        "src/modulo/api/main.py",
    ),
    "MODULO_ENV": (
        "src/modulo/core/logging_config.py",
        "src/modulo/api/routes/deployment.py",
    ),
}

#: The values an operator supplies in ``deploy/compose/.env`` for the compose's
#: ``${VAR:?...}`` references - i.e. the generated secrets, shaped exactly the
#: way ``.env.prod.example`` tells an operator to generate them. Five names
#: since FAR-1509: the DB password and the two fail-closed SAQ web-auth values
#: joined SECRET_KEY / FERNET_KEY as no-default variables.
_OPERATOR_VALUES: dict[str, str] = {
    "SECRET_KEY": "compose-contract-test-secret-key-0123456789abcdef0123456789abcdef",
    # URL-safe base64 of 32 bytes = what Fernet.generate_key() emits and what a
    # valid Fernet key must decode to (cryptography rejects anything else, e.g.
    # hex or a short key, on first credential use).
    "FERNET_KEY": base64.urlsafe_b64encode(b"compose-contract-fernet-key-32b!").decode(),
    # The prod compose embeds this in DATABASE_URL / DATABASE_ADMIN_URL and
    # passes it to the postgres service, so the same value must satisfy all
    # three references - which is exactly what makes a silent `changeme`
    # default dangerous: it reached every one of them (FAR-1509).
    "MODULO_DB_PASSWORD": "compose-contract-test-db-password-0123456789abcdef",
    # The SAQ system worker's fail-closed web auth; the username is not a
    # secret, but compose requires both to be non-empty all the same.
    "SAQ_AUTH_USERNAME": "admin",
    "SAQ_AUTH_PASSWORD": "compose-contract-test-saq-auth-0123456789abcdef",
}

#: `${VAR}` / `${VAR:-default}` / `${VAR:?message}` - the only forms compose
#: interpolates and the only forms this file may use.
_VAR_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:\?|:-)?([^{}]*)\}")


def _load_compose() -> dict:
    if not _PROD_COMPOSE.exists():
        pytest.skip(f"Compose file not present: {_PROD_COMPOSE.relative_to(_REPO_ROOT)}")
    with _PROD_COMPOSE.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _compose_interpolation_text() -> str:
    """Every scalar VALUE in the compose, concatenated - comments excluded.

    The ``${VAR:?...}`` / ``${VAR:-...}`` scans below look for actual
    interpolations, and a raw-text scan also matches prose: the file's own
    explanatory comments legitimately write ``${VAR:?...}`` when telling an
    operator what the syntax means, which a comment-blind regex reads as a
    required variable named ``VAR``. Parsing the YAML first drops the comments
    (they are not part of the document) and leaves exactly the values compose
    interpolates.
    """

    def walk(node: object) -> str:
        if isinstance(node, dict):
            return "".join(walk(value) for value in node.values())
        if isinstance(node, list):
            return "".join(walk(value) for value in node)
        if isinstance(node, str):
            return node
        return ""

    return walk(_load_compose())


def _app_environment(compose: dict) -> dict[str, str]:
    """The raw ``environment`` mapping the compose passes to the app service."""
    services = compose.get("services", {})
    assert _APP_SERVICE in services, (
        f"missing service {_APP_SERVICE} in {_PROD_COMPOSE.relative_to(_REPO_ROOT)} - "
        "the application service was renamed; update this test with it"
    )
    environment = services[_APP_SERVICE].get("environment", {})
    assert isinstance(environment, dict), (
        f"{_APP_SERVICE}.environment must be a mapping (key: value) so its names can be audited; "
        f"got {type(environment).__name__}"
    )
    assert environment, f"{_APP_SERVICE}.environment is empty - nothing to audit"
    return {str(key): str(value) for key, value in environment.items()}


def _settings_env_names() -> dict[str, str]:
    """Env name -> Settings field name, derived the way pydantic-settings does.

    pydantic-settings v2 takes the env name from the field's ``alias`` when it
    has one, else the field name upper-cased (``model_config`` is
    ``case_sensitive=False``), which is the rule ``settings.py`` documents at
    the ``Settings`` class.
    """
    names: dict[str, str] = {}
    for field_name, field in Settings.model_fields.items():
        env_name = (field.alias or field_name).upper()
        names[env_name] = field_name
    return names


def _interpolate(value: str) -> str:
    """Resolve compose's ``${...}`` references against ``_OPERATOR_VALUES``.

    Compose reads ``deploy/compose/.env`` (the project directory) for the
    operator's own values; the test supplies exactly the generated secrets
    ``.env.prod.example`` tells an operator to put there, so a required
    reference to a name the example never documents fails loudly here instead
    of resolving to an empty string.
    """

    def _replace(match: re.Match[str]) -> str:
        name, operator, rest = match.group(1), match.group(2), match.group(3)
        if operator == ":-":
            return _OPERATOR_VALUES.get(name, "") or rest
        if operator == ":?":
            assert name in _OPERATOR_VALUES, (
                f"the compose requires operator variable {name} ({value!r}), which is not one of "
                f"the documented generated secrets {sorted(_OPERATOR_VALUES)} - either it is a typo "
                "for a Settings name (see .env.prod.example) or this test's operator map is stale"
            )
            return _OPERATOR_VALUES[name]
        return _OPERATOR_VALUES.get(name, "")

    resolved = _VAR_REF.sub(_replace, value)
    assert "${" not in resolved, f"unresolved compose interpolation left in {value!r}"
    return resolved


def test_every_env_var_passed_to_the_app_is_read_by_the_app():
    """No name may be passed that nothing reads (the FAR-1506 drift class)."""
    compose = _load_compose()
    environment = _app_environment(compose)
    known = set(_settings_env_names())
    unknown = sorted(name for name in environment if name not in known and name not in _NON_SETTINGS_ENV)

    assert not unknown, (
        f"{_PROD_COMPOSE.relative_to(_REPO_ROOT)} passes env var(s) the application never reads: "
        f"{unknown} (Settings reads {len(known)} names; {len(_NON_SETTINGS_ENV)} more are read "
        "straight from os.environ)\n"
        "A name Settings does not read is silently ignored at boot - this is exactly how "
        "MODULO_SECRET_KEY / MODULO_REDIS_URL shipped and the backend crash-looped (FAR-1506). "
        "If one of these is read straight from os.environ instead, add it to _NON_SETTINGS_ENV "
        "with the source file that reads it."
    )


def test_every_non_settings_env_var_has_a_live_read_site():
    """An allowlisted os.environ name must still be read by the source."""
    for name, source_files in _NON_SETTINGS_ENV.items():
        for relative in source_files:
            path = _BACKEND_ROOT / relative
            assert path.is_file(), f"_NON_SETTINGS_ENV[{name}] cites a missing file: backend/{relative}"
            assert name in path.read_text(encoding="utf-8"), (
                f"backend/{relative} no longer mentions {name} - the read site moved or was deleted, "
                "so the allowlist entry (and the compose's use of the name) must be revisited"
            )


def test_required_settings_fields_are_satisfied_by_the_compose():
    """Every ``Settings`` field with no default must be passed by the compose.

    ``secret_key``, ``fernet_key`` and ``database_url`` are ``Field(...)`` -
    no default - so an unsatisfied one is a ``ValidationError`` at boot and a
    crash-looping backend. ``FERNET_KEY`` was the one FAR-1506 found missing.
    """
    compose = _load_compose()
    environment = _app_environment(compose)
    provided = {name.upper() for name in environment}
    required = {(field.alias or name).upper() for name, field in Settings.model_fields.items() if field.is_required()}
    missing = sorted(required - provided)

    assert not missing, (
        f"{_PROD_COMPOSE.relative_to(_REPO_ROOT)} does not pass required Settings field(s) {missing} "
        "to the application - Settings has no default for them, so boot fails with a ValidationError "
        "and the backend crash-loops (FAR-1506)."
    )


def test_compose_environment_constructs_settings(monkeypatch: pytest.MonkeyPatch):
    """The compose's resolved environment must actually boot the application.

    Resolves the compose's `${...}` references the way compose does, installs
    the result as the process environment, and constructs ``Settings`` - the
    real boot path. This fails on the pre-fix file two ways over: the missing
    ``SECRET_KEY``/``FERNET_KEY`` raise, and a stale name such as
    ``MODULO_REDIS_URL`` leaves ``redis_url`` at its localhost default.
    """
    compose = _load_compose()
    environment = {name: _interpolate(value) for name, value in _app_environment(compose).items()}

    monkeypatch.setattr(os, "environ", environment)
    settings = Settings(_env_file=None)

    assert settings.secret_key == _OPERATOR_VALUES["SECRET_KEY"]
    assert settings.fernet_key == _OPERATOR_VALUES["FERNET_KEY"]
    # The key the example tells an operator to generate must be a key Fernet
    # will accept (32 url-safe-base64 bytes), not merely >= 32 characters -
    # Settings' own validator only checks the length, so an invalid shape
    # boots and then fails on first credential use.
    assert len(base64.urlsafe_b64decode(settings.fernet_key)) == 32
    assert settings.redis_url == environment.get("REDIS_URL"), (
        "REDIS_URL did not reach Settings.redis_url - the compose is passing a name Settings does "
        "not read, so the backend would silently use the localhost default (FAR-1506)"
    )


def test_env_example_documents_the_required_secrets():
    """``.env.prod.example`` must carry the names, without the stale aliases."""
    if not _ENV_EXAMPLE.exists():
        pytest.skip(".env.prod.example not present (running outside repo checkout)")
    example = _ENV_EXAMPLE.read_text(encoding="utf-8")
    active = {
        line.split("=", 1)[0].strip()
        for line in example.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }

    for name in ("SECRET_KEY", "FERNET_KEY"):
        assert name in active, (
            f".env.prod.example must document {name} as a settable variable - it is required by "
            "Settings and has no default"
        )
    for stale in ("MODULO_SECRET_KEY", "MODULO_REDIS_URL", "MODULO_FERNET_KEY"):
        assert stale not in active, (
            f".env.prod.example still documents {stale}, which Settings does not read - an operator "
            "setting it gets a crash-looping backend (FAR-1506)"
        )


def test_no_compose_variable_has_a_silent_default():
    """FAR-1509: a production secret may never fall back to a known string.

    ``MODULO_DB_PASSWORD:-changeme`` used to interpolate the public string
    `changeme` into ``POSTGRES_PASSWORD`` AND into both database URLs, so a
    production stack whose operator never set the variable came up fully
    working with a publicly-known password. Required variables must use
    ``${VAR:?...}``, never ``${VAR:-...}``.
    """
    compose = _compose_interpolation_text()
    defaulted = re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*):-", compose)
    secrets = sorted(name for name in defaulted if name in _OPERATOR_VALUES)

    assert not secrets, (
        f"{_PROD_COMPOSE.relative_to(_REPO_ROOT)} interpolates required variable(s) {secrets} with a "
        "`:-` default - compose would start a production stack without them, silently. Use "
        "`${VAR:?...}` naming the variable and how to set it (see SECRET_KEY / FERNET_KEY)."
    )
    assert compose.count("${MODULO_DB_PASSWORD:?") >= 3, (
        "MODULO_DB_PASSWORD must be required at every reference (POSTGRES_PASSWORD, DATABASE_URL, "
        "DATABASE_ADMIN_URL) - one defaulting reference is enough to reintroduce the silent "
        "`changeme` password"
    )


def test_required_compose_variables_are_documented_in_the_env_example():
    """Every ``${VAR:?...}`` in the compose must be settable via the example.

    Failing fast is only half the job: the file the quickstart tells an
    operator to copy must name every variable compose refuses to start
    without, or the first ``docker compose up`` errors over a name the
    operator has never seen (FAR-1509, deployment.md quickstart).
    """
    compose = _compose_interpolation_text()
    required = sorted(set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*):\?", compose)))
    assert required, "no `${VAR:?}` reference found - the fail-fast contract is gone"

    if not _ENV_EXAMPLE.exists():
        pytest.skip(".env.prod.example not present (running outside repo checkout)")
    example = _ENV_EXAMPLE.read_text(encoding="utf-8")
    active = {
        line.split("=", 1)[0].strip()
        for line in example.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }

    undocumented = [name for name in required if name not in active]
    assert not undocumented, (
        f"the compose requires {undocumented}, but .env.prod.example does not set them - an operator "
        "following the quickstart (cp .env.prod.example deploy/compose/.env) would hit a fail-fast "
        "error naming a variable the example never mentions"
    )
