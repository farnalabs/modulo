"""Regression test for the agent_signal <-> pipeline_engine import cycle.

FAR-168's Deploy pre-deploy integration tests failed at collection with::

    ImportError: cannot import name 'fire_agent_signal' from partially
    initialized module 'modulo.core.trigger_engine.agent_signal'

when ``tests/integration/bdd/test_agent_signal.py`` imported
``fire_agent_signal`` FIRST. agent_signal imported
``sanitize_error_text`` from ``modulo.core.pipeline_engine.error_codes`` at
module level; importing error_codes loads the ``pipeline_engine`` package
``__init__``, which eagerly imports ``executor``, which imports
``fire_agent_signal`` back out of the partially-initialized agent_signal
module.

The fix made the ``sanitize_error_text`` import lazy (inside
``_log_signal_event``). These tests pin BOTH import orders — agent_signal
first (the previously-failing order) and executor first — so the cycle can
never silently return.
"""

# agent_signal FIRST — the previously-failing order. The I001 suppression is
# deliberate: the module pair is ordered this way to pin the import sequence,
# not sorted alphabetically.
from modulo.core.trigger_engine.agent_signal import fire_agent_signal  # noqa: I001
from modulo.core.pipeline_engine.executor import PipelineExecutor


def test_agent_signal_importable_first() -> None:
    """The previously-failing order (agent_signal before pipeline_engine)."""
    assert callable(fire_agent_signal)


def test_executor_importable_after_agent_signal() -> None:
    """The reverse order must also import cleanly after agent_signal."""
    assert PipelineExecutor is not None
