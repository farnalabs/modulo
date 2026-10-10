"""Conformance test: connector error details scrub credential echoes (FAR-1651).

Two structural guarantees, both designed to fail loudly when the pattern is
eroded rather than to pass vacuously:

1. ``ConnectorBase`` keeps both redaction hooks (``_credential_values`` and
   ``_redacted_detail``), the default recipe carries no credentials, and the
   hook's redaction is the single shared ``CredentialRedactor`` implementation.
2. Every ``ConnectorBase`` subclass defined under ``modulo.connectors``
   overrides ``_credential_values`` to hand the base hook its real credential
   values — unless the class is on the explicit ``NO_CREDENTIAL_CLASSES``
   allowlist (abstract bases, credential-free transports, test doubles). A
   new connector class added without a recipe is a hard failure.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from typing import Any

import httpx
import respx

from modulo.connectors.base import ConnectorBase
from modulo.connectors.github import GitHubConnector
from modulo.connectors.security import _MASK, CredentialRedactor

#: Classes allowed to rely on the no-credential default from
#: ``ConnectorBase``. Everything else must override ``_credential_values``
#: with its real credential values.
#: - CIRunnerBase / TicketTrackerBase: abstract bases, no credentials.
#: - FilesystemConnector / ShellConnector: local transports, no credentials.
#: - RestConnector: redacts through ``_secret_values()``/``_redact``;
#:   its ``_credential_values`` returns that same list (rest/__init__.py).
#: - The ``_``-prefixed entries are test doubles with no real credentials.
NO_CREDENTIAL_CLASSES: frozenset[str] = frozenset(
    {
        "CIRunnerBase",
        "FilesystemConnector",
        "RestConnector",
        "ShellConnector",
        "TicketTrackerBase",
        "_AzurePipelinesTestDouble",
        "_BuildkiteTestDouble",
        "_CircleCITestDouble",
        "_GitHubActionsTestDouble",
        "_GitLabCITestDouble",
        "_JenkinsTestDouble",
        "_TeamCityTestDouble",
    }
)

_PLANTED_TOKEN = "ghpo1651plantedcredentialvalue"


def _connector_classes() -> list[type]:
    """Import every connector module; return all ConnectorBase subclasses."""
    pkg = importlib.import_module("modulo.connectors")
    for module_info in pkgutil.walk_packages(pkg.__path__, prefix=pkg.__name__ + "."):
        importlib.import_module(module_info.name)
    seen: set[type] = set()

    def walk(cls: type) -> None:
        for sub in cls.__subclasses__():  # type: ignore[var-annotated]
            if sub not in seen:
                seen.add(sub)
                walk(sub)

    walk(ConnectorBase)
    return [c for c in seen if c.__module__.startswith("modulo.connectors")]


def _credential_recipe_origin(cls: type[ConnectorBase]) -> str:
    """Qualname of the function providing the effective ``_credential_values``."""
    fn: Any = cls._credential_values
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return getattr(fn, "__qualname__", "")


def test_connector_base_keeps_redaction_hooks() -> None:
    """The hooks must stay on the base — removing one must fail this test."""
    assert hasattr(ConnectorBase, "_credential_values")
    assert hasattr(ConnectorBase, "_redacted_detail")
    redact_sig = inspect.signature(ConnectorBase._redacted_detail)
    assert list(redact_sig.parameters) == ["self", "text"]
    recipe_sig = inspect.signature(ConnectorBase._credential_values)
    assert list(recipe_sig.parameters) == ["self"]


def test_no_credential_default_recipe_is_empty() -> None:
    """A subclass skipping the recipe inherits the empty no-credential default."""
    holder = type(
        "NoCredHolder",
        (ConnectorBase,),
        {
            "connector_type": property(lambda self: None),
            "health_check": lambda self: None,
            "query": lambda self: None,
            "write": lambda self: None,
        },
    )
    obj = holder()
    assert not obj._credential_values()


def test_base_hook_routes_through_shared_redactor() -> None:
    """``_redacted_detail`` must build on ``CredentialRedactor`` (no forks)."""
    holder = type(
        "Holder",
        (ConnectorBase,),
        {
            "_credential_values": lambda self: (_PLANTED_TOKEN,),
            "connector_type": property(lambda self: None),
            "health_check": lambda self: None,
            "query": lambda self: None,
            "write": lambda self: None,
        },
    )
    obj = holder()
    out: str = obj._redacted_detail(f"echo {_PLANTED_TOKEN}")
    assert _PLANTED_TOKEN not in out
    assert CredentialRedactor((_PLANTED_TOKEN,)).secrets == (_PLANTED_TOKEN,)
    assert _MASK in out


def test_every_connector_class_declares_credential_recipe() -> None:
    """Any new connector class without a recipe fails here with its name."""
    classes = _connector_classes()
    assert classes, "subclass scavenging failed; expected connector classes"
    offenders: list[str] = []
    for cls in classes:
        if cls.__qualname__ in NO_CREDENTIAL_CLASSES:
            continue
        origin = _credential_recipe_origin(cls)
        if origin == "ConnectorBase._credential_values":
            offenders.append(cls.__qualname__)
    assert not offenders, (
        "Connector subclasses missing the _credential_values recipe (FAR-1651): "
        f"{offenders}. Give each an override returning its credential values, or "
        "add it to NO_CREDENTIAL_CLASSES with a reason if it genuinely has none."
    )


@respx.mock
async def test_health_detail_redacts_planted_credential_github() -> None:
    """End-to-end: GitHub /user 500 body echoing the token is redacted."""
    connector = GitHubConnector(token=_PLANTED_TOKEN)
    respx.get("https://api.github.com/user").mock(return_value=httpx.Response(500, text=f"oops {_PLANTED_TOKEN}"))
    result = await connector.health_check()
    assert result.ok is False
    detail = result.detail or ""
    assert _PLANTED_TOKEN not in detail
    assert _MASK in detail
