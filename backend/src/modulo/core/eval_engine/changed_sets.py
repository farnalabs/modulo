"""Platform-resolved changed sets + the ``diff_policy`` check (FAR-1691).

Change-level checks historically ran on the agent's SELF-REPORTED
``changed_files`` list. A self-reported set is agent-controlled, so a gate that
trusts it can be evaded by an agent that simply omits a file it changed. This
module resolves the changed set on the PLATFORM and records where each set came
from, using the shared provenance taxonomy (``modulo.core.provenance``):

* ``observed`` - the platform read the changed set from the VCS connector at
  the run's pinned commit SHA. This is the trustworthy set.
* ``reported`` - the set the agent emitted in its own output.

Both sets are retained; the set a check actually EVALUATES is the ``observed``
set when one exists, falling back to ``reported`` only when the platform could
not observe one. The provenance of that choice is recorded and surfaced, never
collapsed into "the changed files".

``diff_policy`` is a deterministic, non-model-mediated check over the resolved
set: path globs (an optional allow list and a deny list), forbidden paths, and
agent-config / executable-risk files. The last exists because a worm class
(observed in the wild as "Miasma" / "CoSnitch") persists by writing agent and
editor configuration - Claude Code, Cursor and VS Code config, ``CLAUDE.md`` /
``AGENTS.md`` instructions, MCP server config, and CI workflow definitions - so a
change touching those paths is an executable-risk input even when every line
looks benign.

The module is PURE: no database and no network. :func:`record_changed_set_evidence`
is the one seam that touches the store - it appends the changed-set facts to the
generic Evidence layer (the first-class, append-only run-evidence surface) through
the store's single authorised write path.
"""

from __future__ import annotations

import fnmatch
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.eval_engine.evidence_layer import append_evidence
from modulo.core.provenance import (
    PROVENANCE_OBSERVED,
    PROVENANCE_REPORTED,
)

_log = logging.getLogger(__name__)

#: The provenance classes a changed set may carry. Both are drawn from the
#: shared taxonomy: a platform-resolved set is ``observed``; an agent-emitted
#: set is ``reported``.
CHANGED_SET_PROVENANCE_VALUES: Final[frozenset[str]] = frozenset({PROVENANCE_OBSERVED, PROVENANCE_REPORTED})

#: Path globs whose contents are executable-risk inputs: agent/editor
#: configuration and CI definitions that a repository worm can use to persist
#: across runs or to run code on a contributor's machine. Matched case-
#: sensitively with :func:`fnmatch.fnmatchcase` against the normalised path.
AGENT_CONFIG_RISK_PATTERNS: Final[tuple[str, ...]] = (
    ".claude/**",
    ".cursor/**",
    ".vscode/**",
    ".github/workflows/**",
    "CLAUDE.md",
    "**/CLAUDE.md",
    "AGENTS.md",
    "**/AGENTS.md",
    ".cursorrules",
    "**/.cursorrules",
    ".mcp.json",
    "**/.mcp.json",
)

#: Evidence-store key prefix for changed-set facts. Owned by the ``derived``
#: producer in ``evidence_layer.KEY_NAMESPACE_OWNERSHIP`` - the resolved set is
#: derived from observed and reported facts.
CHANGED_SET_EVIDENCE_PREFIX: Final[str] = "changed_set_"


class ChangedSetResolutionError(ValueError):
    """Raised when no changed set at all is available to resolve."""


class DiffPolicyConfigError(ValueError):
    """Raised when a ``diff_policy`` config is malformed.

    A malformed policy fails CLOSED - the caller treats it as the check being
    unable to run, never as "no violations found".
    """


def normalize_path(path: str) -> str:
    """Canonicalise a repo-relative path for glob matching.

    Backslashes become ``/``, a leading ``./`` is stripped, and any leading
    ``/`` is removed, so ``src\\a.py``, ``./src/a.py`` and ``/src/a.py`` all
    compare equal to the glob ``src/a.py``.
    """
    normalized = path.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


def _pattern_matches(path: str, pattern: str) -> bool:
    """Match *path* against *pattern*, with ``**/`` also matching a root file.

    ``fnmatch`` has no special ``**`` handling: ``**/x`` is ``*/x``, which
    requires the path to contain a slash and therefore never matches a
    root-level ``x``. A leading ``**/`` is additionally tried without the
    prefix so both ``a/x`` and root-level ``x`` match - the behaviour an
    operator writing a repo-wide glob expects.
    """
    if fnmatch.fnmatchcase(path, pattern):
        return True
    if pattern.startswith("**/"):
        return fnmatch.fnmatchcase(path, pattern[3:])
    return False


def _matches_any(path: str, patterns: Sequence[str]) -> bool:
    return any(_pattern_matches(path, pattern) for pattern in patterns)


def is_executable_risk_path(path: str) -> bool:
    """True when *path* is an agent-config / executable-risk input."""
    return _matches_any(normalize_path(path), AGENT_CONFIG_RISK_PATTERNS)


@dataclass(frozen=True, slots=True)
class ChangedSet:
    """A set of changed files together with where it came from.

    ``provenance`` is drawn from :data:`CHANGED_SET_PROVENANCE_VALUES`
    (``observed`` / ``reported``); ``source`` names the channel (e.g.
    ``"vcs_connector"`` or ``"agent_output"``) so the two are never conflated.
    """

    files: tuple[str, ...]
    provenance: str
    source: str
    commit_sha: str | None = None

    def __post_init__(self) -> None:
        if self.provenance not in CHANGED_SET_PROVENANCE_VALUES:
            raise ValueError(
                f"ChangedSet.provenance must be one of {sorted(CHANGED_SET_PROVENANCE_VALUES)}; got {self.provenance!r}"
            )
        if not self.source:
            raise ValueError("ChangedSet.source must be a non-empty channel name")

    @classmethod
    def observed(
        cls,
        files: Sequence[str],
        *,
        source: str = "vcs_connector",
        commit_sha: str | None = None,
    ) -> ChangedSet:
        """Build the platform-resolved (``observed``) set."""
        return cls(files=tuple(files), provenance=PROVENANCE_OBSERVED, source=source, commit_sha=commit_sha)

    @classmethod
    def reported(cls, files: Sequence[str], *, source: str = "agent_output") -> ChangedSet:
        """Build the agent-emitted (``reported``) set."""
        return cls(files=tuple(files), provenance=PROVENANCE_REPORTED, source=source)

    def normalized_files(self) -> tuple[str, ...]:
        """The set as a de-duplicated, sorted tuple of normalised paths."""
        return tuple(sorted({normalize_path(f) for f in self.files if f.strip()}))


@dataclass(frozen=True, slots=True)
class ResolvedChangedSet:
    """The observed and reported sets, plus the one a check evaluates.

    ``evaluated`` is the observed set when it exists (the platform's)
    and otherwise the reported set (the agent's). ``evaluated_provenance``
    names which - so a consumer can always tell whether a gate ran against
    a platform-observed set or merely the agent's self-report.
    """

    observed: ChangedSet | None
    reported: ChangedSet | None
    evaluated: ChangedSet

    @property
    def evaluated_provenance(self) -> str:
        return self.evaluated.provenance

    @property
    def observed_only(self) -> tuple[str, ...]:
        """Paths the platform saw that the agent did not report."""
        observed = set(self.observed.normalized_files()) if self.observed is not None else set()
        reported = set(self.reported.normalized_files()) if self.reported is not None else set()
        return tuple(sorted(observed - reported))

    @property
    def reported_only(self) -> tuple[str, ...]:
        """Paths the agent reported that the platform did not see."""
        observed = set(self.observed.normalized_files()) if self.observed is not None else set()
        reported = set(self.reported.normalized_files()) if self.reported is not None else set()
        return tuple(sorted(reported - observed))

    @property
    def drift(self) -> tuple[str, ...]:
        """Symmetric difference between the observed and reported sets."""
        return tuple(sorted(set(self.observed_only) | set(self.reported_only)))


def resolve_changed_set(
    *,
    observed: ChangedSet | None = None,
    reported: ChangedSet | None = None,
) -> ResolvedChangedSet:
    """Resolve the set a check should evaluate, and record both with provenance.

    The observed (platform) set wins when present; the reported (agent) set is
    the fallback only when the platform could not observe one. Raises
    :class:`ChangedSetResolutionError` when neither is available - a check with
    no changed set must fail closed rather than silently evaluate nothing.
    """
    if observed is None and reported is None:
        raise ChangedSetResolutionError(
            "no changed set available: neither an observed (platform) nor a reported (agent) set was supplied"
        )
    if observed is not None and observed.provenance != PROVENANCE_OBSERVED:
        raise ChangedSetResolutionError(f"observed set must carry provenance 'observed', got {observed.provenance!r}")
    if reported is not None and reported.provenance != PROVENANCE_REPORTED:
        raise ChangedSetResolutionError(f"reported set must carry provenance 'reported', got {reported.provenance!r}")
    evaluated = observed if observed is not None else reported
    assert evaluated is not None  # guarded above: at least one is present
    return ResolvedChangedSet(observed=observed, reported=reported, evaluated=evaluated)


@dataclass(frozen=True, slots=True)
class DiffPolicyConfig:
    """A declarative ``diff_policy`` over the resolved changed set.

    * ``allow`` - when non-empty, every changed path must match at least one
      pattern (an allow list); a path matching none is a violation.
    * ``deny`` - changed paths matching any pattern are violations.
    * ``forbidden`` - paths that must never change at all; matching any is a
      violation. Kept distinct from ``deny`` so an operator can express the two
      intents separately (e.g. deny-all-plus-exceptions vs hard-forbidden).
    * ``flag_agent_config`` - classify agent-config / executable-risk paths.
    * ``agent_config_is_violation`` - when true, any executable-risk path is
      also a violation; when false the risk inputs are surfaced but do not by
      themselves fail the check.
    """

    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    flag_agent_config: bool = True
    agent_config_is_violation: bool = False

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any]) -> DiffPolicyConfig:
        """Build a config from an eval's ``config_json`` dict.

        Raises :class:`DiffPolicyConfigError` on any malformed value so a bad
        definition fails closed instead of silently checking nothing.
        """

        def _globs(key: str) -> tuple[str, ...]:
            raw = config.get(key, ())
            if raw is None:
                return ()
            if isinstance(raw, str):
                raise DiffPolicyConfigError(f"diff_policy.{key} must be a list of glob strings, not a bare string")
            if not isinstance(raw, (list, tuple)):
                raise DiffPolicyConfigError(f"diff_policy.{key} must be a list of glob strings")
            for item in raw:
                if not isinstance(item, str):
                    raise DiffPolicyConfigError(f"diff_policy.{key} entries must be strings, got {type(item).__name__}")
            return tuple(raw)

        def _flag(key: str, default: bool) -> bool:
            raw = config.get(key, default)
            if not isinstance(raw, bool):
                raise DiffPolicyConfigError(f"diff_policy.{key} must be a boolean, got {type(raw).__name__}")
            return raw

        return cls(
            allow=_globs("allow"),
            deny=_globs("deny"),
            forbidden=_globs("forbidden"),
            flag_agent_config=_flag("flag_agent_config", True),
            agent_config_is_violation=_flag("agent_config_is_violation", False),
        )


@dataclass(frozen=True, slots=True)
class DiffPolicyOutcome:
    """The result of a ``diff_policy`` check over the resolved set."""

    passed: bool
    evaluated_provenance: str
    evaluated_count: int
    forbidden_matches: tuple[str, ...]
    disallowed_matches: tuple[str, ...]
    risk_inputs: tuple[str, ...]
    detail: str


def evaluate_diff_policy(
    config: DiffPolicyConfig | Mapping[str, Any],
    resolved: ResolvedChangedSet,
) -> DiffPolicyOutcome:
    """Evaluate *config* against the resolved set's evaluated files.

    Pure and deterministic. Returns a :class:`DiffPolicyOutcome` whose
    ``evaluated_provenance`` always names which set was checked, and whose
    ``risk_inputs`` lists the executable-risk paths regardless of pass/fail so
    the finding is never hidden behind a passing verdict.
    """
    policy = config if isinstance(config, DiffPolicyConfig) else DiffPolicyConfig.from_mapping(config)
    files = resolved.evaluated.normalized_files()

    forbidden_matches = tuple(f for f in files if _matches_any(f, policy.forbidden))
    disallowed_matches = tuple(f for f in files if policy.allow and not _matches_any(f, policy.allow))
    denied_matches = tuple(f for f in files if _matches_any(f, policy.deny))
    risk_inputs = tuple(f for f in files if is_executable_risk_path(f)) if policy.flag_agent_config else ()

    violations = set(forbidden_matches) | set(disallowed_matches) | set(denied_matches)
    if policy.agent_config_is_violation:
        violations |= set(risk_inputs)

    passed = not violations
    detail = (
        f"diff_policy: provenance={resolved.evaluated_provenance} evaluated={len(files)} "
        f"forbidden={len(forbidden_matches)} denied={len(denied_matches)} "
        f"disallowed={len(disallowed_matches)} risk={len(risk_inputs)}"
    )
    if not passed:
        detail = f"{detail} violations={sorted(violations)}"

    return DiffPolicyOutcome(
        passed=passed,
        evaluated_provenance=resolved.evaluated_provenance,
        evaluated_count=len(files),
        forbidden_matches=forbidden_matches,
        disallowed_matches=disallowed_matches,
        risk_inputs=risk_inputs,
        detail=detail,
    )


def changed_set_evidence_values(
    resolved: ResolvedChangedSet,
    outcome: DiffPolicyOutcome | None = None,
) -> dict[str, Any]:
    """The Evidence-layer key/value facts describing *resolved* (and *outcome*).

    Both sets are recorded with their provenance, plus which set was evaluated
    and the observed/reported drift, so the run-evidence surface can show what
    the platform saw versus what the agent claimed - never just one collapsed
    list. All keys carry :data:`CHANGED_SET_EVIDENCE_PREFIX`.
    """
    values: dict[str, Any] = {
        "changed_set_observed": list(resolved.observed.files) if resolved.observed is not None else None,
        "changed_set_reported": list(resolved.reported.files) if resolved.reported is not None else None,
        "changed_set_evaluated": list(resolved.evaluated.files),
        "changed_set_evaluated_provenance": resolved.evaluated_provenance,
        "changed_set_drift": list(resolved.drift),
    }
    if outcome is not None:
        values["changed_set_policy_passed"] = outcome.passed
        values["changed_set_policy_risk_inputs"] = list(outcome.risk_inputs)
    else:
        values["changed_set_policy_passed"] = None
    return values


def record_changed_set_evidence(
    db: AsyncSession,
    *,
    organisation_id: UUID,
    run_id: UUID,
    resolved: ResolvedChangedSet,
    outcome: DiffPolicyOutcome | None = None,
) -> list[Any]:
    """Append the changed-set facts to the run's Evidence rows.

    Routes every fact through :func:`append_evidence` - the store's single
    authorised write path - as the ``derived`` producer (the resolved set is
    computed from observed and reported facts). Returns the appended rows; the
    caller's transaction commits them.

    Raises :class:`~modulo.core.eval_engine.evidence_layer.EvidenceWriteAuthorisationError`
    if a key falls outside the producer's owned namespace.
    """
    rows: list[Any] = []
    for key, value in changed_set_evidence_values(resolved, outcome).items():
        rows.append(
            append_evidence(
                db,
                organisation_id=organisation_id,
                key=key,
                subject_type="run",
                subject_id=str(run_id),
                value=value,
                producer_type="derived",
            )
        )
    return rows


__all__ = [
    "AGENT_CONFIG_RISK_PATTERNS",
    "CHANGED_SET_EVIDENCE_PREFIX",
    "CHANGED_SET_PROVENANCE_VALUES",
    "ChangedSet",
    "ChangedSetResolutionError",
    "DiffPolicyConfig",
    "DiffPolicyConfigError",
    "DiffPolicyOutcome",
    "ResolvedChangedSet",
    "changed_set_evidence_values",
    "evaluate_diff_policy",
    "is_executable_risk_path",
    "normalize_path",
    "record_changed_set_evidence",
    "resolve_changed_set",
]
