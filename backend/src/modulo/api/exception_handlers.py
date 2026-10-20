import logging
import traceback
import urllib.parse

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from modulo.api.frontend_url import resolve_frontend_url
from modulo.api.models.problem import (
    ProblemDetail,
    ProblemException,
    ProblemType,
    problem_from_http_exception,
    problem_from_validation_error,
)
from modulo.auth.sso import SsoCallbackError
from modulo.db.capacity import StorageExhaustedError
from modulo.settings import Settings, get_settings

_log = logging.getLogger(__name__)

_HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"})


def union_allow_header(request: Request) -> str | None:
    """Every method the OpenAPI document declares for the request's path template.

    Starlette builds the ``Allow`` header of a 405 from the FIRST route whose
    path matched but whose method did not: ``Router`` keeps a single ``partial``
    and ``Route.handle`` advertises ``self.methods`` of that one route. FastAPI
    registers each verb as its own ``APIRoute``, so a collection resource split
    into ``GET ""`` and ``POST ""`` advertises only the verb of whichever route
    was registered first — RFC 9110 §15.5.6 requires the header to list the
    methods the resource actually supports, and Schemathesis's
    ``allow_header_conformance`` check compares it against the OpenAPI document
    (the nightly ``Schemathesis API Fuzz`` gate failed on exactly this).

    The document is the source of truth rather than a walk of ``app.routes``:
    FastAPI nests included routers behind private wrapper objects, and the
    document is exactly what a client (and the fuzz gate) is validated against.
    ``FastAPI.openapi()`` caches the generated schema on the app, so the lookup
    is a dict read after the first call; routes hidden with
    ``include_in_schema=False`` are absent by construction, so the advertised
    set can never exceed the documented one.

    Returns ``None`` when the request carries no application scope (the plain
    stubbed request objects some unit tests hand to the handler), the app is
    not a FastAPI app, the path is not in the document, or schema generation
    fails — the caller then keeps whatever header the exception already had.
    """
    scope = getattr(request, "scope", None)
    if not isinstance(scope, dict):
        return None
    openapi = getattr(scope.get("app"), "openapi", None)
    if openapi is None:
        return None
    route = scope.get("route")
    template = getattr(route, "path", None) or str(request.url.path)
    try:
        paths = openapi().get("paths", {})
    except Exception:
        _log.warning(
            "exception_handlers.openapi_allow_lookup_failed",
            extra={"path": template},
            exc_info=True,
        )
        return None
    declared = paths.get(template)
    if not isinstance(declared, dict):
        return None
    methods = {str(method).upper() for method in declared} & _HTTP_METHODS
    if not methods:
        return None
    return ", ".join(sorted(methods))


def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    headers = dict(exc.headers or {})
    if exc.status_code == status.HTTP_405_METHOD_NOT_ALLOWED:
        allow = union_allow_header(request)
        if allow is not None:
            headers["Allow"] = allow
    if isinstance(exc, ProblemException):
        problem = exc.problem
        problem.request_id = getattr(request.state, "request_id", None)
        return problem.to_response(headers=headers)
    problem = problem_from_http_exception(request, exc)
    return problem.to_response(headers=headers)


def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    problem = problem_from_validation_error(request, exc.errors())
    return problem.to_response()


def storage_exhausted_exception_handler(request: Request, exc: StorageExhaustedError) -> JSONResponse:
    """Map the DB-capacity ``StorageExhaustedError`` to HTTP 503 (FAR-426).

    A ``fixed``-mode DB at/over the 98% hard-stop refuses NEW run creation.
    Rendered as ``urn:problem:modulo:storage_exhausted`` so the frontend can
    distinguish "storage is full — clear work" from a generic outage.
    """
    rid = getattr(request.state, "request_id", None)
    return ProblemDetail.from_type(
        problem_type=ProblemType.STORAGE_EXHAUSTED,
        detail=str(exc),
        instance=str(request.url.path),
        request_id=rid,
    ).to_response()


def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all for any exception not handled by specific handlers.

    Logs the full exception with stack trace and returns a structured
    ProblemDetail 500 response with correlation_id.
    """
    rid = getattr(request.state, "request_id", None)
    _log.exception(
        "exception_handlers.unhandled_exception",
        extra={
            "method": request.method,
            "path": str(request.url.path),
            "request_id": rid,
            "exc_type": type(exc).__name__,
            "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        },
    )
    try:
        return ProblemDetail.from_type(
            problem_type=ProblemType.INTERNAL_ERROR,
            detail="An unexpected error occurred",
            instance=str(request.url.path),
            request_id=rid,
        ).to_response()
    except Exception:
        return ProblemDetail.fallback_internal_error(rid)


# ---------------------------------------------------------------------------
# SSO callback failure -> frontend login page (FAR-1791)
# ---------------------------------------------------------------------------


def sso_error_redirect(settings: Settings, code: str, slug: str | None) -> RedirectResponse:
    """303-redirect the browser to the frontend login page with an SSO error code.

    The target is assembled ONLY from server-owned values — the configured
    frontend base (``resolve_frontend_url``) and a DB-resolved org slug — never
    from caller-supplied input, so it cannot be abused as an open redirect.
    ``code`` is drawn from the fixed vocabulary in ``modulo.auth.sso``
    (``SSO_ERROR_*``), so exception text never reaches the URL.

    303 (See Other), not the success path's 307, converts the IdP's POST
    navigation (the SAML ACS) into a GET of the login page.
    """
    base = resolve_frontend_url(settings)
    suffix = f"/login/{urllib.parse.quote(slug)}" if slug else "/login"
    return RedirectResponse(
        url=f"{base}{suffix}?sso_error={code}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def sso_callback_error_handler(_request: Request, exc: SsoCallbackError) -> RedirectResponse:
    """Render a raised :class:`SsoCallbackError` as the login-page 303 redirect.

    Registered for ``SsoCallbackError`` in ``main.py``. The callback routes
    RAISE the error (rather than returning the redirect inline) so the
    ``audited_system("saml_acs_attempted", ...)`` dependency observes the
    failure and records an honest ``outcome="error"``; this handler is the
    single place the redirect is built, over the shared
    :func:`sso_error_redirect` mapping. ``_request`` is part of the handler
    signature Starlette requires but is unused: the target derives only from
    server settings and the exception, never from the request. The handler is
    deliberately synchronous — it performs no I/O and awaits nothing.
    """
    return sso_error_redirect(get_settings(), exc.code, exc.slug)
