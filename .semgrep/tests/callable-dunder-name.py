# Fixture for the `callable-dunder-name` rule set. Not imported by the app or
# the test suite; it exists only for the semgrep rule test runner, which reads
# the `ruleid` / `ok` annotations on each case. Rule A catches a `__name__` /
# `__qualname__` read off a value annotated `Callable`; rule B catches such a
# read on an error path (an `except` handler, or a logging call) unless the
# value is provably a class.
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)


# --- Rule A: value annotated `Callable` -----------------------------------


def unsafe_annotated(fn: Callable[..., Any]) -> str:
    # ruleid: callable-annotated-dunder-name
    return fn.__name__


def unsafe_annotated_qualname(fn: Callable[..., Any]) -> str:
    # ruleid: callable-annotated-dunder-name
    return fn.__qualname__


def safe_annotated_uses_label(fn: Callable[..., Any]) -> str:
    # ok: callable-annotated-dunder-name
    from modulo.util import callable_label

    return callable_label(fn)


# --- Rule B: read on an error path ----------------------------------------


def unsafe_except_as() -> None:
    try:
        connect()
    except Exception as exc:  # noqa: BLE001
        # ruleid: dunder-name-on-error-path
        name = builder.__name__
        _ = (exc, name)


def unsafe_except_bare_type() -> None:
    try:
        connect()
    except ConnectionError:
        # ruleid: dunder-name-on-error-path
        name = builder.__name__
        _ = name


def unsafe_bare_except() -> None:
    try:
        connect()
    except:  # noqa: E722
        # ruleid: dunder-name-on-error-path
        name = builder.__name__
        _ = name


def unsafe_logging_read(obj: Any) -> None:
    # ruleid: dunder-name-on-error-path
    log.info("using %s", obj.__name__)


def safe_class_dunder(obj: Any) -> None:
    try:
        connect()
    except Exception:  # noqa: BLE001
        # ok: dunder-name-on-error-path
        log.info("class %s", obj.__class__.__name__)


def safe_class_qualname(obj: Any) -> str:
    try:
        connect()
    except Exception:  # noqa: BLE001
        # ok: dunder-name-on-error-path
        return obj.__class__.__qualname__


def safe_type_dunder(obj: Any) -> None:
    try:
        connect()
    except Exception:  # noqa: BLE001
        # ok: dunder-name-on-error-path
        log.info("type %s", type(obj).__name__)


def safe_annotated_type(fn: type) -> str:
    try:
        connect()
    except Exception:  # noqa: BLE001
        # ok: dunder-name-on-error-path
        return fn.__name__
