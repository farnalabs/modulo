"""Conformance test: connector error details scrub credential echoes (FAR-1651).

Four structural guarantees, each designed to fail loudly when the pattern is
eroded rather than to pass vacuously:

1. ``ConnectorBase`` keeps both redaction hooks (``_credential_values`` and
   ``_redacted_detail``), the default recipe carries no credentials, and the
   hook's redaction is the single shared ``CredentialRedactor`` implementation.
2. Every ``ConnectorBase`` subclass defined under ``modulo.connectors``
   overrides ``_credential_values`` to hand the base hook its real credential
   values — unless the class is on the explicit ``NO_CREDENTIAL_CLASSES``
   allowlist (abstract bases, credential-free transports). A new connector
   class added without a recipe is a hard failure.
3. Every recipe, when executed against a stump instance whose every attribute
   carries a planted secret, yields a NON-EMPTY sequence containing that
   planted secret — proving the recipe actually binds something redactable
   (a recipe returning ``()`` or omitting the real credential fails here).
4. No ``HealthResult(...)`` construction or ``detail =`` assignment in
   ``modulo.connectors`` source builds an error-detail from a violation token
   (raw ``.text`` echo, ``str(exc)``, f-string ``{exc}``) without a redaction
   call in the same expression region. The scanner is validated against
   planted violations in the same test so the gate cannot erode silently.
"""

from __future__ import annotations

import importlib
import inspect
import pathlib
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
#: Everything else found under ``modulo.connectors`` either defines its own
#: recipe or inherits one from a concrete parent (the CI test doubles inherit
#: from the real connectors they double for, so their recipe origin is the
#: real class's override — no allowlist entry is needed).
NO_CREDENTIAL_CLASSES: frozenset[str] = frozenset(
    {
        "CIRunnerBase",
        "FilesystemConnector",
        "ShellConnector",
        "TicketTrackerBase",
    }
)

_PLANTED_TOKEN = "ghpo1651plantedcredentialvalue"

_REDACT_MARKERS = (
    "_redacted_detail(",
    ".redact(",
    "redact(",
    "redact_text(",
)

# Violation tokens: content-bearing echoes that must never reach a detail
# unredacted. `.text` is the response-body echo; the rest are exception echoes.
_VIOLATION_TOKENS = (
    ".text",
    "str(e)",
    "str(exc)",
    "{e}",
    "{exc}",
)


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


def _region_of(src: str, start_idx: int, open_token: str) -> str:
    """Expand from ``start_idx`` through a balanced paren/bracket region."""
    i = src.find(open_token, start_idx)
    if i == -1:
        return src[start_idx : start_idx + 200]
    depth = 0
    j = i
    n = len(src)
    in_str: str | None = None
    while j < n:
        ch = src[j]
        if in_str is not None:
            if ch == "\\":
                j += 2
                continue
            if ch == in_str:
                in_str = None
            j += 1
            continue
        if ch in ('"', "'"):
            in_str = ch
            j += 1
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return src[i : j + 1]
        j += 1
    return src[i:n]


def scan_detail_regions(source: str) -> list[str]:
    """Return the ``HealthResult(...)``/``detail =`` regions with raw echoes.

    A region is a FINDING only when it both (a) builds its text from a
    violation token (response-body echo, ``str(exc)``, f-string ``{exc}``) and
    (b) contains no redaction call. Regions that route through
    ``_redacted_detail`` / ``.redact()`` / ``redact_text`` pass even when they
    echo those tokens (that is the redacted-at-source pattern).
    """
    findings: list[str] = []
    idx = 0
    while True:
        cands = [(source.find(tok, idx), tok) for tok in ("HealthResult(", "detail =")]
        cands = [(pos, tok) for pos, tok in cands if pos != -1]
        if not cands:
            break
        pos, tok = min(cands)
        text = _region_of(source, pos, tok)
        end = pos + len(text)
        has_token = any(p in text for p in _VIOLATION_TOKENS) or '"{e' in text or '{e"' in text
        has_redact = any(m in text for m in _REDACT_MARKERS)
        if has_token and not has_redact:
            findings.append(f"line {source[:pos].count(chr(10)) + 1}: {text[:160]!r}")
        idx = max(end, pos + 1)
    return findings


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


class _PlantedSecret(str):
    """A str that doubles as a credential-holder object.

    Recipes read credential values through three shapes — plain attribute
    (``self._token``), ``self._redactor.secrets``, and
    ``self._secret_values()``. The stump's ``__getattr__`` returns an instance
    of this single class for every attribute read, and the class exposes a
    ``secrets`` tuple containing itself and is call-able, covering all three
    shapes (including recipes that concatenate ``self._basic_secrets``).
    """

    secrets = property(lambda self: (self,))

    def __call__(self) -> tuple[str, ...]:
        return (self,)


def _secret_stump() -> Any:
    planted = _PlantedSecret(_PLANTED_TOKEN)

    class _Stump:
        def __getattr__(self, name: str) -> Any:
            # Recipes that carry their own baked-in wire-form tuple
            # (e.g. Confluence's ``self._basic_secrets``) expect a sequence;
            # every other attribute read is the plain planted credential.
            if name.endswith("secrets"):
                return (planted,)
            return planted

    return _Stump()


def test_every_recipe_binds_redactable_values() -> None:
    """Each connector's recipe, executed against a planted stump, must yield the secret.

    A recipe that returns an empty sequence, or one that omits the real
    credential (so redaction no-ops), fails with the class and the observed
    value list. Wire-form recipes (basic-auth base64 blobs) also contain the
    planted secret itself, which is the guarantee that matters: the redactor
    must at minimum strip the raw credential.
    """
    classes = _connector_classes()
    assert classes, "subclass scavenging failed; expected connector classes"
    offenders: list[str] = []
    for cls in classes:
        if cls.__qualname__ in NO_CREDENTIAL_CLASSES:
            continue
        stump = _secret_stump()
        try:
            values: Any = cls._credential_values(stump)  # type: ignore[call-arg]
        except Exception as exc:
            offenders.append(f"{cls.__qualname__}: raised {type(exc).__name__}: {exc}")
            continue
        items = tuple(values)
        if not items or _PLANTED_TOKEN not in items:
            offenders.append(f"{cls.__qualname__}: bound {items!r}")
    assert not offenders, (
        "Connector recipes that do not bind redactable credential values (FAR-1651): "
        f"{offenders}. The recipe must include the real credential so redaction is effective."
    )


def test_scanner_detects_planted_violations() -> None:
    """The scanner catches both erosion mutations — echo and wrapper removal."""
    echo_mutation = 'result = HealthResult(ok=False, detail=f"HTTP error: {exc}")'
    echo_findings = scan_detail_regions(echo_mutation)
    assert echo_findings, "scanner missed the raw-echo HealthResult mutation"
    assert any("{exc}" in f or "str(exc)" in f or "HTTP error" in f for f in echo_findings)

    wrapper_removed = 'detail = "vault upstream said: " + response.text'
    assert scan_detail_regions(wrapper_removed), "scanner missed the wrapper-removal mutation"
    assert scan_detail_regions("detail = str(exc)[:200]"), "scanner missed str(exc) mutation"

    # Redacted-at-source patterns must stay clean — the scanner is not a
    # redaction-mandatory hammer on every detail site, only on raw echoes.
    assert not scan_detail_regions(
        'return HealthResult(ok=False, detail=self._redacted_detail(f"HTTP {resp.status_code}: {request.url}"))'
    )
    assert not scan_detail_regions("return health_check_failure(exc, self._redacted_detail)")


def test_connector_source_contains_no_raw_echo_details() -> None:
    """Every real ``HealthResult``/``detail =`` region routes echoes through a redactor."""
    pkg = importlib.import_module("modulo.connectors")
    root = pathlib.Path(pkg.__file__).parent  # type: ignore[arg-type, union-attr]
    assert root.is_dir(), f"connector package directory missing: {root}"
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for finding in scan_detail_regions(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(root)} {finding}")
    assert not offenders, (
        "Raw (unredacted) error-echo detail constructions in modulo/connectors (FAR-1651): "
        f"{offenders}. Route the detail through the connector's redactor "
        "(`self._redacted_detail` / `self._redactor.redact`) before it is persisted."
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
