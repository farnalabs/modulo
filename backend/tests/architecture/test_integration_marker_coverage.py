"""Architecture test: every tests/integration/test_*.py carries the integration marker.

The deploy pre-gate runs ``pytest tests/integration/ -m integration -n 2``.
A test file WITHOUT the integration marker on its module-level ``pytestmark``
assignment contributes ZERO collected tests there — pytest deselects the whole
module, so entire files could silently vanish from the pre-deploy gate while
still running in the full suite. This scanner fails when any integration test
module's ``pytestmark`` lacks ``pytest.mark.integration``, keeping the sweep
enforced (FAR-583). The check parses the AST, so single-line assignments and
multi-line ``pytestmark = [...]`` lists are both covered.
"""

import ast
from pathlib import Path

INTEGRATION_DIR = Path(__file__).resolve().parent.parent / "integration"


def _pytestmark_missing_integration(path: Path) -> bool:
    """True when the module's pytestmark (if any) lacks pytest.mark.integration."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        if value is not None and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in targets):
            return "pytest.mark.integration" not in ast.unparse(value)
    return True


def test_every_integration_test_file_has_integration_marker():
    unmarked = []
    for path in sorted(INTEGRATION_DIR.rglob("test_*.py")):
        if _pytestmark_missing_integration(path):
            unmarked.append(str(path.relative_to(INTEGRATION_DIR.parent)))
    assert not unmarked, (
        f"Found {len(unmarked)} integration test file(s) whose `pytestmark` lacks `pytest.mark.integration` — "
        "the deploy pre-gate (`pytest tests/integration/ -m integration -n 2`) silently deselects "
        "their tests:\n" + "\n".join(f"  {p}" for p in unmarked)
    )
