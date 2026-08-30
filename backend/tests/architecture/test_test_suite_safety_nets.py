"""Architecture tests: safety nets that keep CI honest.

Three lenses guard against the quiet ways a test suite loses coverage:

- **Quarantine registry consistency** — every ``test_id`` in the repo-root
  ``.quarantine.yml`` must resolve to a real test file and carry the fields
  the plugin relies on. A stale entry referencing a deleted/renamed test
  silently stops protecting anything (the xfail marker never applies), which
  turns the flaky-test quarantine into a false sense of security.
- **``@awaiting-implementation`` deselection pin** — the ``-m 'not
  awaiting-implementation'`` addopts deselects every scenario tagged
  ``@awaiting-implementation`` from every run. That exclusion is deliberate
  only while the set is small and known; a newly-tagged scenario (or an
  accidentally removed tag) must fail CI so the change is a deliberate,
  reviewed decision rather than a silent weaken/strengthen of the run set.
- **Feature-file skip tags need a reason** — Gherkin ``@skip``/``@xfail``
  tags (pytest-bdd) are only self-documenting when the immediately preceding
  line explains why; the ``.py``-level skip lens in
  ``test_test_suite_quality.py`` cannot see Gherkin tags.

Every lens reports actionable file:line diagnostics instead of a bare
"assert not violations", mirroring the sibling architecture tests.
"""

from __future__ import annotations

import ast
from pathlib import Path

import yaml

BACKEND = Path(__file__).resolve().parent.parent.parent
REPO = BACKEND.parent
TESTS = BACKEND / "tests"
FEATURES = TESTS / "bdd" / "features"
QUARANTINE_FILE = REPO / ".quarantine.yml"

#: Workflow self-report payloads (GitHub Actions → POST .../journeys/self-report)
#: must name the lifecycle-map stage the workflow completes. The merge/deploy
#: stages are EXTERNAL (a GitHub Actions workflow, no ``pipeline_id``), so the
#: endpoint can only advance journeys into them via ``stage_id`` — a payload
#: that drops it silently leaves journeys stuck at their old stage.
SELF_REPORT_WORKFLOWS: dict[str, str] = {
    "merge-queue.yml": "merge",
    "deploy.yml": "deploy",
}

#: Scenarios deliberately excluded from every run via the
#: ``awaiting-implementation`` marker (pyproject addopts). Keyed by feature
#: file path relative to ``backend/``. Changing this set is a deliberate
#: product decision — the tag marks desired behaviour that is not yet
#: implemented, so a scenario in this set NEVER runs in CI.
PINNED_AWAITING_IMPLEMENTATION: dict[str, frozenset[str]] = {
    "tests/bdd/features/personas/alice-devx-sme.feature": frozenset(
        {
            "Alice models her current SDLC with manual nodes",
            "Alice replaces a manual QA step with an agent",
            "Alice proves HITL compliance to an auditor",
            "Alice reverts a step replacement when the agent underperforms",
            "Alice's team owns pipeline config but QA can only view",
            "Alice's SOC 2 auditor reviews HITL evidence",
            "Alice adds automated evals before increasing agent autonomy",
        }
    ),
    "tests/bdd/features/personas/elena-engineering-director.feature": frozenset(
        {
            "Elena sees a consolidated org dashboard",
            "Elena sees token spend by team and pipeline",
            "Elena spots a quality regression in her eval dashboard",
            "Elena decides between models based on eval comparison",
            "Elena receives a weekly quality report via Slack",
            "Elena aligns eval suites with team OKRs",
            "Elena sees whether HITL effort is decreasing over time",
        }
    ),
    "tests/bdd/features/personas/jordan-community-contributor.feature": frozenset(
        {
            "Jordan contributes a new agent to the community library",
            "Jordan's contribution carries versioned provenance",
            "Jordan updates his contributed primitive with improvements",
            "Jordan packages evals alongside his contributed agent",
        }
    ),
    "tests/bdd/features/personas/marcus-ciso.feature": frozenset(
        {
            "Marcus verifies the audit log is append-only",
            "Marcus confirms no data leaves the organisation's infrastructure",
            "Marcus confirms offboarding immediately revokes access",
        }
    ),
    "tests/bdd/features/personas/priya-platform-engineer.feature": frozenset(
        {
            "Priya self-hosts Modulo on existing infrastructure",
            "Priya integrates Okta SSO with JIT provisioning",
            "Priya isolates teams so they only see their own pipelines",
            "Priya A/B tests Claude Sonnet vs GPT-4o on the same pipeline",
            "Priya enforces minimum eval thresholds per team",
            "Priya's pipelines fail over when a model provider has an outage",
            "Priya sees org-wide adoption metrics",
            "Priya's HITL rejections grow the eval suite automatically",
        }
    ),
    "tests/bdd/features/pipelines/run_variants.feature": frozenset(
        {
            "Coverage gaps are reported for a variant group",
        }
    ),
    "tests/bdd/features/pipelines/scheduling.feature": frozenset(
        {
            "Cron trigger fires and creates a run",
            "Polling trigger fires when condition is met",
            "Polling trigger does not fire when condition not met",
            "Polling trigger logs error when connector fails",
        }
    ),
    "tests/bdd/features/pipelines/webhook_trigger.feature": frozenset(
        {
            "Webhook with valid HMAC creates a run",
            "Webhook with invalid HMAC is rejected",
            "Webhook with expired timestamp is rejected",
            "Duplicate webhook payload is rejected",
            "Flood protection rejects when at max concurrent runs",
        }
    ),
    "tests/bdd/features/plugins/plugin_registry.feature": frozenset(
        {
            "Discover installed plugins",
            "Get plugin detail",
            "Plugin discovery on startup",
            "Plugin manifest validation",
        }
    ),
    "tests/bdd/features/variants/variant_groups.feature": frozenset(
        {
            "Sequential execution order matches insertion order",
            "Variant comparison returns eval scores per node and token cost",
            "Eval coverage gap is detected when variants diverge but evals match",
            "Comparison shows token cost breakdown per variant",
        }
    ),
    "tests/bdd/features/workflows/import.feature": frozenset(
        {
            "Import valid pipeline bundle",
            "Import rejects tampered bundle with invalid Ed25519 signature",
            "Import resolves connector type conflicts with disambiguation",
            "Import resolves schema version conflicts with disambiguation suffix",
            "Import handles duplicate pipeline names with suffix",
        }
    ),
    "tests/bdd/features/agents/schema_assignment.feature": frozenset(
        {
            "Remove schema assignment",
        }
    ),
    "tests/bdd/features/composites/composite_library.feature": frozenset(
        {
            "Composite content_json validation — missing required fields returns error",
        }
    ),
    "tests/bdd/features/errors/recovery.feature": frozenset(
        {
            "Already running run cannot be recovered",
            "Manual fix then resume",
            "Recovery preserves node 1 output",
            "Recovery with modified run_context",
            "Resume from checkpoint after failure",
        }
    ),
    "tests/bdd/features/errors/retry.feature": frozenset(
        {
            "Retry from failed node",
            "Retry from start restarts the entire pipeline",
            "Retry on successful run is rejected",
            "Retry resets downstream state",
            "Retry with new run_context",
        }
    ),
    "tests/bdd/features/mcp/human_only.feature": frozenset(
        {
            "Audit logs distinguish MCP vs human actions",
            "MCP can list but not act on human-only gates",
            "MCP cannot bypass human-only gate",
        }
    ),
    "tests/bdd/features/mcp/library_browse.feature": frozenset(
        {
            "MCP library_browse is read-only",
            "MCP lists library primitives",
            "MCP searches library primitives",
            "MCP without library:browse scope is blocked",
        }
    ),
    "tests/bdd/features/mcp/review_hitl.feature": frozenset(
        {
            "MCP approves a gate",
            "MCP cannot approve without claim",
            "MCP lists pending gates",
            "MCP rejects a gate",
            "MCP without hitl:review scope is blocked",
        }
    ),
    "tests/bdd/features/mcp/trigger.feature": frozenset(
        {
            "MCP client triggers a run",
            "MCP trigger for non-existent pipeline returns error",
            "MCP trigger respects scope limits",
            "MCP trigger with run_context",
            "MCP trigger without auth is rejected",
        }
    ),
    "tests/bdd/features/observability/active_run_observability.feature": frozenset(
        {
            "Run detail exposes the active-run observability fields",
            "Run event stream exposes node lifecycle events",
        }
    ),
    "tests/bdd/features/model_backends/health_check.feature": frozenset(
        {
            "Health check respects org scoping",
            "Healthy model backend returns ok",
            "Stub backend always returns healthy",
            "Unhealthy model backend returns error",
        }
    ),
    "tests/bdd/features/pipelines/concurrency.feature": frozenset(
        {
            "Completed run frees concurrency slot",
            "Concurrency limit is enforced per-org",
            "Concurrent runs exceeding limit are rejected",
            "Concurrent runs within limit are allowed",
            "Different pipelines do not affect each others concurrency",
        }
    ),
    "tests/bdd/features/schemas/create.feature": frozenset(
        {
            "Invalid JSON Schema is rejected",
        }
    ),
    "tests/bdd/features/ui/eval_dashboard.feature": frozenset(
        {
            "Compare two runs side-by-side",
            "Empty state when no evals exist",
            "Filter eval runs by pass/fail status",
            "View eval results for a completed run",
        }
    ),
    "tests/bdd/features/ui/org_settings.feature": frozenset(
        {
            "Invite a new member",
            "Non-admin cannot access settings",
            "Revoke an API key",
            "Update organisation name",
            "View organisation settings page",
        }
    ),
    "tests/bdd/features/ui/pipeline_builder.feature": frozenset(
        {
            "Add an agent node to the canvas",
            "Configure an agent's prompt",
            "Connect two nodes with an edge",
            "Delete a node from the canvas",
            "Load pipeline builder page",
        }
    ),
    "tests/bdd/features/ui/real_time_updates.feature": frozenset(
        {
            "HITL approval notification appears",
            "Status updates arrive via WebSocket",
            "WebSocket reconnects after disconnect",
        }
    ),
    "tests/bdd/features/ui/run_detail.feature": frozenset(
        {
            "Expand a node to view its output",
            "Live status updates via WebSocket",
            "Prompt reveal shows dialog with system and user messages",
            "Sensitive values are masked in run output",
            "Sensitive values are masked in the prompt reveal response",
            "View run logs",
            "View run status and details",
        }
    ),
    "tests/bdd/features/ui/theme_switching.feature": frozenset(
        {
            "Default theme is standard",
            "Switch to agent theme",
            "Theme preference persists across reload",
        }
    ),
}


def _feature_scenario_tags(feature: Path) -> list[tuple[int, str, set[str]]]:
    """Return ``(line, scenario_name, tags)`` for every scenario in a feature file."""
    scenarios: list[tuple[int, str, set[str]]] = []
    tags: set[str] = set()
    for lineno, raw in enumerate(feature.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("@"):
            tags = {t for t in line.split() if t.startswith("@")}
            continue
        if line.startswith(("Scenario Outline", "Scenario:")):
            scenarios.append((lineno, line.split(":", 1)[1].strip(), tags))
            tags = set()
    return scenarios


def _quarantine_entries(quarantine_file: Path | None = None) -> list[dict]:
    qf = quarantine_file or QUARANTINE_FILE
    if not qf.exists():
        return []
    data = yaml.safe_load(qf.read_text(encoding="utf-8"))
    if not data:
        return []
    return list(data.get("quarantine") or [])


def _stripped_symbol(nodeid_symbol: str) -> str:
    """Strip the ``[...]`` parametrization suffix from *each* ``::`` segment of a
    nodeid symbol so ``test_foo[abc]`` resolves to ``test_foo``, and a
    parametrized class nodeid ``TestFoo[param]::test_bar`` keeps its
    ``::test_bar`` method component instead of degrading to a bare-class check
    that would mask a renamed/deleted method."""
    return "::".join(part.split("[", 1)[0] for part in nodeid_symbol.split("::"))


def _resolves_definitions(nodeid_symbol: str, symbols: set[str]) -> bool:
    """Return True when ``nodeid_symbol`` resolves against the collected set.

    pytest item nodeids are either a bare ``test_*`` function (``test_foo``) or
    a ``Class::method`` chain (``TestFoo::test_bar``), each possibly with a
    ``[...]`` parametrization suffix.

    A class reference MUST carry an explicit ``::method`` component: pytest
    never emits ``file.py::TestFoo`` as an item nodeid, so a bare-class entry
    would be accepted by the lens but could never match the plugin's verbatim
    ``item.nodeid in quarantined`` check — a false pass. A ``Class::method`` node
    only resolves when *both* the class and the exact method exist (exact-set
    membership, not substring containment), so a renamed method is caught even
    though its class still does.
    """
    symbol = _stripped_symbol(nodeid_symbol)
    parts = symbol.split("::")
    if len(parts) == 1:
        # Bare function: pytest only collects ``test_*`` names. A bare class
        # (TestFoo) is never a valid pytest item nodeid, so reject it.
        return parts[0].startswith("test_") and parts[0] in symbols
    if len(parts) == 2:
        cls, method = parts
        return cls in symbols and f"{cls}::{method}" in symbols
    # 3+ segments (e.g. Class::TestCaseKind::nodes) — exact membership only.
    return symbol in symbols


def _collect_definitions(path: Path) -> set[str]:
    """Return the set of pytest-collectable test symbols defined in ``path``.

    Walks the module AST for every ``test_*`` function (top level or inside a
    ``Test*`` class) plus every ``Test*`` class, matching pytest's collection
    rules closely enough to catch a renamed/deleted test. (BDD scenario modules
    are handled separately — see :func:`_is_bdd_scenario_module`.)
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return set()

    symbols: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            symbols.add(node.name)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            symbols.add(node.name)
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name.startswith("test_"):
                    symbols.add(f"{node.name}::{sub.name}")
    return symbols


def _is_bdd_scenario_module(path: Path) -> bool:
    """Return True for pytest-bdd step/scenario modules.

    pytest-bdd 7.x modules that call ``scenarios(...)`` at import time have
    their ``test_<scenario>`` item functions injected at runtime, so they never
    appear in static source — a legitimate entry such as
    ``test_audit_export_steps.py::test_paginated_csv_export_loads_events`` would
    be falsely flagged "symbol not found" by the AST walk. The plugin still
    xfails those items by verbatim nodeid, so we skip AST symbol validation for
    these modules (a stale BDD entry is still caught by the file-existence
    check). Recognised by the ``scenarios(`` injection point or its
    ``pytest_bdd`` import.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return "scenarios(" in source or "import pytest_bdd" in source or "from pytest_bdd" in source


def _resolve_quarantine_target(
    test_id: str, backend: Path | None = None, repo: Path | None = None
) -> tuple[Path | None, str | None, str]:
    """Return ``(file, test_name, error)`` for a quarantine ``test_id``.

    ``file`` is the resolved module path (or ``None`` when the nodeid is
    malformed or the file does not exist), ``test_name`` is the symbol after
    the ``::`` separator (or ``None`` on failure), and ``error`` is non-empty
    exactly when the entry cannot be resolved to a file.
    """
    if "::" not in test_id:
        return None, None, "entry missing '::' separator between file and test name"
    path_part, test_part = test_id.split("::", 1)
    if not path_part:
        return None, None, "entry missing file path before '::' — a test_id must be 'file.py::test_name'"
    backend = backend or BACKEND
    repo = repo or REPO
    resolved = backend / path_part if path_part.startswith("tests/") else repo / path_part
    if not resolved.exists():
        return None, None, f"file not found ({resolved}) — rename or remove the entry"
    if not resolved.is_file():
        return None, None, f"path is not a file ({resolved}) — a test_id must point at a test module"
    return resolved, test_part, ""


def _quarantine_violations(quarantine_file: Path, backend: Path, repo: Path) -> list[str]:
    """Return the list of stale/incomplete ``.quarantine.yml`` entry violations.

    Extracted from :func:`test_quarantine_registry_entries_resolve` so it can be
    exercised directly by unit tests against a synthetic registry (the real
    ``.quarantine.yml`` holds only commented examples, so the lens iterates an
    empty list and the symbol-resolution code paths never run in CI).
    """
    violations: list[str] = []
    for entry in _quarantine_entries(quarantine_file):
        test_id = str(entry.get("test_id", ""))
        if not test_id:
            violations.append("  entry missing test_id")
            continue
        _path, test_part, error = _resolve_quarantine_target(test_id, backend, repo)
        if error:
            violations.append(f"  {test_id}: {error}")
            continue
        if not test_part:
            violations.append(f"  {test_id}: missing test name after '::'")
            continue
        if not _is_bdd_scenario_module(_path) and not _resolves_definitions(test_part, _collect_definitions(_path)):
            violations.append(
                f"  {test_id}: test symbol {_stripped_symbol(test_part)!r} not found in {_path} — "
                "the test was renamed/deleted, so this quarantine entry protects nothing"
            )
        if not entry.get("reason"):
            violations.append(f"  {test_id}: missing required 'reason' field")
        if not entry.get("expiry"):
            violations.append(f"  {test_id}: missing required 'expiry' field (ISO 8601)")
    return violations


def _workflow_run_blocks(workflow: Path) -> list[str]:
    """Return every step ``run:`` string in a GitHub Actions workflow."""
    data = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    blocks: list[str] = []
    for job in (data.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            run = step.get("run")
            if isinstance(run, str):
                blocks.append(run)
    return blocks


def test_workflow_self_report_payloads_name_the_stage():
    """The merge-queue and deploy workflows POST a self-report payload to
    ``.../journeys/self-report`` naming the lifecycle-map stage they complete.
    The merge/deploy stages are external (GitHub Actions, no ``pipeline_id``),
    so the endpoint advances journeys into them via ``stage_id`` — a payload
    that drops it silently leaves journeys stuck at their old stage. Drift
    here is invisible to the unit tests, which exercise the endpoint with
    hand-built bodies."""
    violations = []
    for filename, expected_stage in SELF_REPORT_WORKFLOWS.items():
        path = REPO / ".github" / "workflows" / filename
        if not path.exists():
            violations.append(f"  .github/workflows/{filename}: workflow file not found")
            continue
        found_self_report = False
        for block in _workflow_run_blocks(path):
            if "workflow_self_report" not in block:
                continue
            found_self_report = True
            if f'stage_id: "{expected_stage}"' not in block:
                violations.append(
                    f"  .github/workflows/{filename}: workflow_self_report payload missing "
                    f'stage_id: "{expected_stage}" — the endpoint needs it to advance journeys '
                    "into the external merge/deploy stage"
                )
        if not found_self_report:
            violations.append(f"  .github/workflows/{filename}: no workflow_self_report payload step found")
    assert not violations, "Workflow self-report payloads drifted from the lifecycle-map stage contract.\n" + "\n".join(
        violations
    )


def test_awaiting_implementation_set_is_pinned():
    """The deselected ``@awaiting-implementation`` scenario set must match the
    pinned set exactly — adding or removing a tag silently changes what CI
    runs and needs an explicit, reviewed decision."""
    actual: dict[str, set[str]] = {}
    for feature in sorted(FEATURES.rglob("*.feature")):
        for _lineno, name, tags in _feature_scenario_tags(feature):
            if "@awaiting-implementation" in tags:
                actual.setdefault(feature.relative_to(BACKEND).as_posix(), set()).add(name)

    if actual == PINNED_AWAITING_IMPLEMENTATION:
        return

    problems: list[str] = []
    for rel in sorted(set(actual) | set(PINNED_AWAITING_IMPLEMENTATION)):
        got = actual.get(rel, set())
        pinned = PINNED_AWAITING_IMPLEMENTATION.get(rel, frozenset())
        for name in sorted(got - pinned):
            problems.append(
                f"  {rel}: scenario {name!r} newly tagged @awaiting-implementation — REMOVE the tag or extend the pin"
            )
        for name in sorted(pinned - got):
            problems.append(
                f"  {rel}: scenario {name!r} no longer tagged @awaiting-implementation — behaviour must now run in CI"
            )
    assert not problems, "The @awaiting-implementation deselection set drifted from the pin.\n" + "\n".join(problems)


def test_quarantine_registry_entries_resolve():
    """Every quarantined test_id must point at a real test file, resolve to a
    real collectable test symbol inside that file, and carry the fields the
    plugin needs — a stale entry (a missing file, or a renamed/deleted test
    function or class) is a dead safety net that silently stops protecting
    anything because the xfail marker never applies."""
    violations = _quarantine_violations(QUARANTINE_FILE, BACKEND, REPO)
    assert not violations, (
        "Found stale or incomplete .quarantine.yml entries — a quarantined test that "
        "cannot resolve is silently never xfailed.\n" + "\n".join(violations)
    )


def test_feature_skip_tags_have_reason():
    """Gherkin ``@skip``/``@xfail`` tags must be preceded by a comment that
    says why — an undocumented tag silently removes the scenario from CI."""
    violations = []
    for feature in sorted(FEATURES.rglob("*.feature")):
        lines = feature.read_text(encoding="utf-8").splitlines()
        for lineno, raw in enumerate(lines, start=1):
            if raw.strip().startswith(("@skip", "@xfail")):
                prev = lines[lineno - 2].strip() if lineno >= 2 else ""
                if not prev.startswith("#"):
                    violations.append(
                        f"  {feature.relative_to(BACKEND)}:{lineno}  {raw.strip()} — no '#' reason comment above"
                    )
    assert not violations, "Found @skip/@xfail scenario tags without a reason comment.\n" + "\n".join(violations)
