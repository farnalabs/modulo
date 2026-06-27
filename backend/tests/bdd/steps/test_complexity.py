"""Step definitions for the Complexity Reviewer library primitive feature."""

from pytest_bdd import given, scenarios

# ---------------------------------------------------------------------------
# Register feature file
# ---------------------------------------------------------------------------
try:
    scenarios("../../features/complexity/complexity_reviewer.feature")
except (FileNotFoundError, OSError):
    pass

# ===========================================================================
# complexity/complexity_reviewer.feature  —  TODO stub (no scenarios yet)
# ===========================================================================


@given("a pipeline with a complexity reviewer node")
def step_complexity_reviewer_node() -> bool:
    """Placeholder: complexity scorer integration as a pipeline node."""
    return True


@given("a complexity score threshold is configured")
def step_complexity_threshold() -> bool:
    """Placeholder: threshold-based branching scenario."""
    return True


@given("the complexity scorer returns a high score")
def step_high_complexity_score() -> bool:
    """Placeholder: high-score routing scenario."""
    return True


@given("the complexity scorer returns a low score")
def step_low_complexity_score() -> bool:
    """Placeholder: low-score routing scenario."""
    return True
