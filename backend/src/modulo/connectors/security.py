"""Shared connector credential redaction (FAR-513).

Every external-tool connector holds credentials (tokens, API keys, passwords).
When a remote API fails it often echoes those credentials back — in the request
URL (query-string auth, e.g. Trello's ``key``/``token``), in the response body
(``resp.text``), or in a transport error's ``repr``. Left unredacted, a leaked
credential escapes through run error detail, health checks, and logs.

This module hoists the connector-level credential redaction pattern that
``RestConnector`` pioneered (``_secret_values`` / ``_redact``) into one shared
place so every vendor connector can use it without duplicating the logic:

* :func:`credential_values` — extract the connector's credential values from its
  decrypted credentials dict (or ``__init__`` params), longest-first so a value
  that is a substring of a longer value is redacted correctly.
* :func:`redact_text` — value-based replacement (strip every credential value).
* :func:`redact_exc` — given an exception, return a same-*type* exception whose
  message has the credentials stripped. When the message is unchanged the
  ORIGINAL exception is returned unchanged, so normal operation (no credential
  in the message) is a strict no-op and never changes exception type or
  chaining.
* :class:`CredentialRedactor` — a tiny wrapper that binds a connector's
  credential values and exposes ``.redact(text)``, ``.redact_exc(exc)`` and a
  ``.wrapping()`` context manager that redacts any exception before it escapes
  a connector boundary (``query`` / ``write``).

The replacement mask (``***``) matches ``RestConnector`` so redaction is
consistent across all connectors. Values shorter than :data:`_MIN_SECRET_LEN`
are ignored — redacting a 1-2 char secret would mangle every occurrence of the
common substring it appears in.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import httpx

# The replacement applied to any redacted credential value (matches
# ``RestConnector._redact`` so every connector reports a consistent mask).
_MASK = "***"

# Values shorter than this are ignored (see module docstring).
_MIN_SECRET_LEN = 4


def _leaf_strings(value: Any) -> Iterator[str]:
    """Yield every leaf string reachable inside *value* (dicts, lists, scalars)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _leaf_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _leaf_strings(v)


def credential_values(creds: dict[str, Any] | None, *, min_len: int = _MIN_SECRET_LEN) -> tuple[str, ...]:
    """Collect the connector's credential values from *creds*, longest-first.

    Recurse every value so vendor-specific key names (``lin_api_key``,
    ``app_key``, nested OAuth payloads, ...) are covered — the set of keys a
    connector might use is open-ended, so the VALUE is what matters. Longest
    first so a value that is a substring of a longer value (e.g. a bare API key
    inside a ``Bearer <key>`` header prefix) is fully replaced.
    """
    if not creds:
        return ()
    secrets: set[str] = set()
    for value in creds.values():
        if isinstance(value, str):
            if len(value) >= min_len:
                secrets.add(value)
        else:
            for leaf in _leaf_strings(value):
                if len(leaf) >= min_len:
                    secrets.add(leaf)
    return tuple(sorted(secrets, key=len, reverse=True))


def redact_text(text: Any, secrets: Sequence[str]) -> str:
    """Strip every credential value from *text* (mirrors ``RestConnector._redact``)."""
    result = text if isinstance(text, str) else str(text)
    for secret in secrets:
        if secret and secret in result:
            result = result.replace(secret, _MASK)
    return result


def _scrub_headers(headers: httpx.Headers, secrets: Sequence[str]) -> list[tuple[str, str]]:
    """Return *headers* as ``(name, value)`` pairs with credential values stripped."""
    return [(k, redact_text(v, secrets)) for k, v in headers.items()]


def _scrub_request(request: httpx.Request | None, secrets: Sequence[str]) -> httpx.Request | None:
    """Return a copy of *request* with credential values stripped from URL/headers.

    ``httpx`` errors echo the full request URL (query-string auth included, as
    Trello does with ``key``/``token``) and headers, so the exception attached
    to an errored call must be rebuilt with a redacted URL/headers — otherwise
    the credential survives in ``exc.request.url`` even when the message is
    redacted. Returns the original request unchanged when nothing needs
    redacting (preserving identity so the no-op path stays a strict no-op).
    """
    if request is None:
        return None
    url = redact_text(str(request.url), secrets)
    headers = _scrub_headers(request.headers, secrets)
    if url == str(request.url) and headers == list(request.headers.items()):
        return request
    kwargs: dict[str, Any] = {"method": request.method, "url": url, "headers": headers}
    try:
        content = request.content
    except Exception:
        content = None
    if content is not None:
        kwargs["content"] = content
    try:
        return httpx.Request(**kwargs)
    except Exception:  # pragma: no cover - defensive fallback
        return request


def _scrub_response(response: httpx.Response, secrets: Sequence[str]) -> httpx.Response:
    """Return a copy of *response* with credential values stripped from text/URL/headers.

    The response body is itself a live socket of a credential echo, and its
    embedded ``.request`` carries the query-string URL — both are scrubbed so
    ``exc.response.text`` and ``exc.response.request.url`` never expose the
    credential after the exception is redacted. Returns the original response
    unchanged when nothing needs redacting.
    """
    request = _scrub_request(response.request, secrets)
    text = redact_text(response.text, secrets)
    headers = _scrub_headers(response.headers, secrets)
    if text == response.text and headers == list(response.headers.items()) and request is response.request:
        return response
    try:
        return httpx.Response(status_code=response.status_code, request=request, text=text, headers=headers)
    except Exception:  # pragma: no cover - defensive fallback
        return response


def redact_exc(exc: Exception, secrets: Sequence[str]) -> Exception:
    """Return a same-*type* exception whose message has credentials redacted.

    When the message contains no credential the ORIGINAL exception is returned
    unchanged (a strict no-op — normal operation never alters exception type,
    message, or chaining). When redaction applies, the exception is rebuilt to
    the same type with the redacted message AND a scrubbed ``request`` /
    ``response`` so the credential does not survive in the attached transport
    objects either (Trello's ``key``/``token`` live in the request query string,
    which ``exc.request.url`` / ``exc.response.request.url`` would otherwise
    still expose). The original ``__cause__`` is preserved.
    """
    message = redact_text(str(exc), secrets)
    if message == str(exc):
        return exc
    try:
        if isinstance(exc, httpx.HTTPStatusError):
            request = _scrub_request(exc.request, secrets)
            if request is None:  # pragma: no cover - HTTPStatusError always carries a request
                request = exc.request
            new: Exception = httpx.HTTPStatusError(
                message,
                request=request,
                response=_scrub_response(exc.response, secrets),
            )
        elif isinstance(exc, httpx.RequestError):
            new = _rebuild_httpx_request_error(exc, message, secrets)
        else:
            new = _rebuild_generic_error(exc, message)
    except Exception:  # pragma: no cover - defensive fallback
        new = RuntimeError(message)
    new.__cause__ = exc.__cause__
    return new


def _rebuild_httpx_request_error(exc: httpx.RequestError, message: str, secrets: Sequence[str] = ()) -> Exception:
    """Rebuild a ``httpx`` transport error with *message*, preserving ``request``.

    ``RequestError.request`` is a property that RAISES ``RuntimeError`` when the
    request was never set (e.g. a manually-constructed error), so it cannot be
    read via ``getattr(..., None)`` — it must be wrapped in a try/except. The
    rebuilt request has its URL/headers scrubbed so the credential cannot
    survive in ``exc.request.url``.
    """
    try:
        request = exc.request
    except Exception:
        request = None
    request = _scrub_request(request, secrets)
    try:
        return type(exc)(message, request=request)
    except TypeError:
        return type(exc)(message)


def _rebuild_generic_error(exc: Exception, message: str) -> Exception:
    """Rebuild a generic (or ``ValueError``-family) exception with *message*.

    ``ValueError`` subclasses (e.g. ``SlackError``, ``GitHubError``,
    ``JiraConnectorError``) all accept a single message argument, so the exact
    subclass type is preserved rather than being flattened to ``ValueError``.
    """
    try:
        return type(exc)(message)
    except Exception:
        if isinstance(exc, ValueError):
            return ValueError(message)
        if isinstance(exc, RuntimeError):
            return RuntimeError(message)
        return RuntimeError(message)


def redacting(method: Callable[..., Any]) -> Callable[..., Any]:
    """Method decorator that redacts any exception escaping the connector boundary.

    Reads ``self._redactor`` (a :class:`CredentialRedactor`) at call time and
    wraps the method body so an exception whose message contains a credential
    value is re-raised with that value stripped. A no-op for clean messages.
    Works on ``async def`` methods (the connector surface is async-first).
    """

    @functools.wraps(method)
    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        redactor: CredentialRedactor = self._redactor
        with redactor.wrapping():
            return await method(self, *args, **kwargs)

    return wrapper


class CredentialRedactor:
    """Binds a connector's credential values and redacts error/exception text.

    Construct from the connector's decrypted credentials dict via
    :meth:`from_creds`, or directly from ``__init__`` params (connectors that
    receive a ``token`` / ``api_key`` as individual arguments). Every public
    entry point that can surface a remote error — typically ``query`` and
    ``write`` — wraps its body in ``self._redactor.wrapping()`` so any
    exception that escapes has its credentials stripped before it propagates to
    the caller.
    """

    def __init__(self, secrets: Sequence[str] = ()) -> None:
        self._secrets = tuple(
            sorted(
                {s for s in secrets if isinstance(s, str) and len(s) >= _MIN_SECRET_LEN},
                key=len,
                reverse=True,
            ),
        )

    @classmethod
    def from_creds(cls, creds: dict[str, Any] | None) -> CredentialRedactor:
        """Build a redactor from a decrypted credentials dict."""
        return cls(credential_values(creds))

    @property
    def secrets(self) -> tuple[str, ...]:
        return self._secrets

    def redact(self, text: Any) -> str:
        return redact_text(text, self._secrets)

    def redact_exc(self, exc: Exception) -> Exception:
        return redact_exc(exc, self._secrets)

    @contextmanager
    def wrapping(self) -> Iterator[None]:
        """Context manager that redacts any exception before it escapes.

        A no-op for clean messages (returns the original exception unchanged),
        so existing behaviour is identical unless a credential value actually
        appears in an error message.
        """
        try:
            yield
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            repaired = redact_exc(exc, self._secrets)
            if repaired is exc:
                raise
            raise repaired from exc.__cause__
