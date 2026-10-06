"""Architecture test: the audit-coverage ratchet (FAR-1472).

Every mutating REST route (``.post``/``.put``/``.patch``/``.delete``) must
either carry an ``audited(...)`` dependency or sit in the generated baseline
``audit_coverage_baseline.txt``. A NEW unannotated route fails here; so does a
baseline line whose route has since been annotated (the baseline only shrinks).

Deliberately an architecture test rather than a semgrep rule: semgrep has no
Windows binary and the pre-commit wrapper fails open on the missing
``semgrep-core``, so a rule that cannot run locally would land unenforced.

Deliberate exemptions in the baseline: read-only POSTs such as
``parameter_schemas.py:validate_parameter_values_endpoint`` - a ``POST`` that
writes nothing, so chaining an audit event per validation call would only add
chain noise.
"""

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
_SCRIPTS_DIR = _BACKEND_DIR / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import audit_coverage as scanner  # noqa: E402

BASELINE_PATH = _BACKEND_DIR / "tests" / "architecture" / "audit_coverage_baseline.txt"

_UNANNOTATED_ROUTE = """\
from fastapi import APIRouter

router = APIRouter()


@router.post("/widgets")
async def create_widget() -> dict[str, str]:
    return {"ok": "true"}
"""

_DEPENDENCIES_FORM = """\
from fastapi import APIRouter, Depends

from modulo.core.audit_coverage import audited

router = APIRouter()


@router.post("/widgets", dependencies=[Depends(audited("widget_created", "widget"))])
async def create_widget() -> dict[str, str]:
    return {"ok": "true"}
"""

_DECORATOR_FORM = """\
from fastapi import APIRouter

from modulo.core.audit_coverage import audited

router = APIRouter()


@router.post("/widgets")
@audited("widget_created", "widget")
async def create_widget() -> dict[str, str]:
    return {"ok": "true"}
"""

_READ_ROUTE = """\
from fastapi import APIRouter

router = APIRouter()


@router.get("/widgets")
async def list_widgets() -> dict[str, str]:
    return {"ok": "true"}
"""


_SYSTEM_DEPENDENCIES_FORM = """\
from fastapi import APIRouter, Depends

from modulo.core.audit_coverage import audited_system

router = APIRouter()


@router.post("/widgets", dependencies=[Depends(audited_system("widget_seen", "widget", actor_source="pre_auth"))])
async def create_widget() -> dict[str, str]:
    return {"ok": "true"}
"""


_SYSTEM_DECORATOR_FORM = """\
from fastapi import APIRouter

from modulo.core.audit_coverage import audited_system

router = APIRouter()


@router.post("/widgets")
@audited_system("widget_seen", "widget", actor_source="unauthenticated")
async def create_widget() -> dict[str, str]:
    return {"ok": "true"}
"""

_MULTILINE_DECORATOR = """\
from fastapi import APIRouter, Depends

from modulo.core.audit_coverage import audited

router = APIRouter()


@router.post(
    "/widgets",
    status_code=201,
    dependencies=[Depends(audited("widget_created", "widget"))],
)
async def create_widget() -> dict[str, str]:
    return {"ok": "true"}
"""


def test_scanner_flags_an_unannotated_mutating_route():
    """The ratchet must see a plain POST with no annotation."""
    assert scanner.scan_source(_UNANNOTATED_ROUTE, "widgets.py") == {"widgets.py:create_widget"}


def test_scanner_accepts_the_dependencies_form():
    """``dependencies=[Depends(audited(...))]`` on the route decorator."""
    assert not scanner.scan_source(_DEPENDENCIES_FORM, "widgets.py")


def test_scanner_accepts_the_decorator_form():
    """``@audited(...)`` on the handler is an accepted annotation shape."""
    assert not scanner.scan_source(_DECORATOR_FORM, "widgets.py")


def test_scanner_accepts_a_multiline_route_decorator():
    """Real route decorators span several lines - the AST handles that."""
    assert not scanner.scan_source(_MULTILINE_DECORATOR, "widgets.py")


def test_scanner_never_flags_reads():
    """GET routes are out of scope: reads are not mutations."""
    assert not scanner.scan_source(_READ_ROUTE, "widgets.py")


def test_scanner_accepts_the_audited_system_form():
    """``audited_system(...)`` (actor-less, FAR-1516) counts as annotated."""
    assert not scanner.scan_source(_SYSTEM_DEPENDENCIES_FORM, "widgets.py")


def test_scanner_accepts_the_audited_system_decorator_form():
    """The actor-less variant is accepted in the decorator shape too."""
    assert not scanner.scan_source(_SYSTEM_DECORATOR_FORM, "widgets.py")


def test_scan_covers_the_whole_route_tree():
    """Guards against a scanner that silently finds nothing (vacuous pass)."""
    route_files = sorted(scanner.ROUTES_DIR.glob("*.py"))
    assert len(route_files) >= 80, f"route tree looks empty: {len(route_files)} file(s) in {scanner.ROUTES_DIR}"
    assert scanner._count_mutating_routes() >= 300, "scanner found too few mutating routes - it is not reading the tree"


def test_no_unannotated_mutating_routes_outside_the_baseline():
    """The ratchet: a new mutating route must be annotated (or baselined)."""
    unannotated = scanner.scan_tree()
    new_violations, _stale = scanner.compare(unannotated, scanner.read_baseline())
    assert not new_violations, (
        f"{len(new_violations)} mutating route(s) have no audited(...) dependency and are not baselined:\n  "
        + "\n  ".join(new_violations)
        + "\n\nEither annotate the route or, for a deliberate exemption, regenerate the baseline "
        "with: uv run python scripts/audit_coverage.py --update"
    )


def test_baseline_contains_no_stale_entries():
    """A baselined route that is now annotated must leave the baseline."""
    unannotated = scanner.scan_tree()
    _new, stale_entries = scanner.compare(unannotated, scanner.read_baseline())
    assert not stale_entries, (
        f"{len(stale_entries)} baseline entr(ies) are annotated now - regenerate the baseline "
        "with: uv run python scripts/audit_coverage.py --update:\n  " + "\n  ".join(stale_entries)
    )


def test_baseline_is_sorted_so_regeneration_is_idempotent():
    """``--update`` output must be byte-stable: header + sorted entries."""
    baseline = BASELINE_PATH.read_text(encoding="utf-8")
    entries = [line for line in baseline.splitlines() if line.strip() and not line.startswith("#")]
    assert entries == sorted(entries), "baseline entries are not sorted - rerun the generator"
    assert scanner.render_baseline(set(entries)) == baseline, "regenerating would change the baseline file"
