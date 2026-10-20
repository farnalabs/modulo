"""Fixture for `.semgrep/no-op-except-reraise.yml` (semgrep --test).

The rule fires ONLY when a concrete re-raise is the sole handler of its `try`
(a no-op, SonarCloud python:S2737). A concrete re-raise that is one of several
handlers is the correct FastAPI idiom and must NOT fire. Undefined names are
fine here: semgrep parses, it does not execute, and `.semgrep/tests/` is
excluded from ruff.
"""


def sole_handler_concrete() -> None:
    # ruleid: no-op-except-reraise
    try:
        do_work()
    except HTTPException:
        raise


def sole_handler_concrete_with_as() -> None:
    # ruleid: no-op-except-reraise
    try:
        do_work()
    except ValueError as _exc:
        raise


def idiom_concrete_before_broad() -> None:
    # ok: no-op-except-reraise
    try:
        do_work()
    except HTTPException:
        raise
    except Exception:
        log_exception()


def idiom_concrete_after_other() -> None:
    # ok: no-op-except-reraise
    try:
        do_work()
    except KeyError:
        log_exception()
    except HTTPException:
        raise


def sole_handler_with_else() -> None:
    # ok: no-op-except-reraise
    try:
        do_work()
    except KeyError:
        raise
    else:
        ok()


def sole_handler_with_finally() -> None:
    # ok: no-op-except-reraise
    try:
        do_work()
    except KeyError:
        raise
    finally:
        cleanup()


def broad_base_is_a_sibling_rule() -> None:
    # ok: no-op-except-reraise
    try:
        do_work()
    except Exception:
        raise


def handler_maps_to_a_new_error() -> None:
    # ok: no-op-except-reraise
    try:
        do_work()
    except KeyError:
        raise RuntimeError("mapped") from None
