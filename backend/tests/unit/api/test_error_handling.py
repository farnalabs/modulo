"""Unit tests for standardised error handling — ProblemDetail model, exception handlers, request_id middleware."""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.exception_handlers import (
    http_exception_handler,
    validation_exception_handler,
)
from modulo.api.middleware.catch_all import CatchAllMiddleware
from modulo.api.middleware.request_id import RequestIdMiddleware
from modulo.api.models.problem import ProblemDetail, ProblemType
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _make_app() -> FastAPI:
    app = FastAPI(debug=False)

    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)

    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(CatchAllMiddleware)

    @app.get("/test/ok")
    async def ok() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/test/not-found")
    async def not_found() -> None:
        raise HTTPException(status_code=404, detail="Resource not found")

    @app.get("/test/unauthorized")
    async def unauthorized() -> None:
        raise HTTPException(status_code=401, detail="Missing auth token")

    @app.get("/test/forbidden")
    async def forbidden() -> None:
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    @app.get("/test/conflict")
    async def conflict() -> None:
        raise HTTPException(status_code=409, detail="Resource already exists")

    @app.get("/test/rate-limited")
    async def rate_limited() -> None:
        raise HTTPException(status_code=429, detail="Too many requests")

    @app.get("/test/bad-request")
    async def bad_request() -> None:
        raise HTTPException(status_code=400, detail="Invalid input")

    @app.get("/test/internal-error")
    async def internal_error() -> None:
        raise ValueError("Something broke internally")

    @app.post("/test/validation")
    async def validation(body: TestBody) -> dict[str, str]:
        return {"body": body.name}

    return app


class TestBody(BaseModel):
    name: str = Field(min_length=1)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        cors_origins="http://example.com",
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    app = _make_app()
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestProblemModel:
    def test_problem_detail_creation(self) -> None:
        problem = ProblemDetail.from_type(ProblemType.NOT_FOUND, detail="Resource not found")
        assert problem.type == "urn:problem:modulo:not_found"
        assert problem.title == "Not Found"
        assert problem.status == 404
        assert problem.detail == "Resource not found"
        assert problem.instance is None
        assert problem.request_id is None

    def test_problem_detail_with_optional_fields(self) -> None:
        rid = str(uuid.uuid4())
        problem = ProblemDetail.from_type(
            ProblemType.VALIDATION_ERROR,
            detail="name: field required",
            instance="/test/validation",
            request_id=rid,
        )
        assert problem.detail == "name: field required"
        assert problem.instance == "/test/validation"
        assert problem.request_id == rid

    def test_problem_detail_serialization(self) -> None:
        problem = ProblemDetail.from_type(
            ProblemType.NOT_FOUND,
            detail="Pipeline with id 123 not found",
            request_id="req-abc",
        )
        dumped = problem.model_dump(mode="json", exclude_none=True)
        assert dumped["type"] == "urn:problem:modulo:not_found"
        assert dumped["title"] == "Not Found"
        assert dumped["status"] == 404
        assert dumped["detail"] == "Pipeline with id 123 not found"
        assert dumped["request_id"] == "req-abc"

    def test_problem_detail_to_response(self) -> None:
        problem = ProblemDetail.from_type(
            ProblemType.NOT_FOUND,
            detail="Not found",
            request_id="req-123",
        )
        resp = problem.to_response()
        assert resp.status_code == 404
        body = resp.body
        import json
        data = json.loads(body)
        assert data["type"] == "urn:problem:modulo:not_found"


class TestExceptionHandlers:
    def test_health_ok(self, client: TestClient) -> None:
        resp = client.get("/test/ok")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_404_returns_problem_detail(self, client: TestClient) -> None:
        resp = client.get("/test/not-found")
        assert resp.status_code == 404
        body = resp.json()
        assert body["type"] == "urn:problem:modulo:not_found"
        assert body["title"] == "Not Found"
        assert body["status"] == 404
        assert body["detail"] == "Resource not found"

    def test_401_returns_problem_detail(self, client: TestClient) -> None:
        resp = client.get("/test/unauthorized")
        assert resp.status_code == 401
        body = resp.json()
        assert body["type"] == "urn:problem:modulo:unauthorized"

    def test_403_returns_problem_detail(self, client: TestClient) -> None:
        resp = client.get("/test/forbidden")
        assert resp.status_code == 403
        body = resp.json()
        assert body["type"] == "urn:problem:modulo:forbidden"

    def test_409_returns_problem_detail(self, client: TestClient) -> None:
        resp = client.get("/test/conflict")
        assert resp.status_code == 409
        body = resp.json()
        assert body["type"] == "urn:problem:modulo:conflict"

    def test_429_returns_problem_detail(self, client: TestClient) -> None:
        resp = client.get("/test/rate-limited")
        assert resp.status_code == 429
        body = resp.json()
        assert body["type"] == "urn:problem:modulo:rate_limited"

    def test_400_returns_problem_detail(self, client: TestClient) -> None:
        resp = client.get("/test/bad-request")
        assert resp.status_code == 400
        body = resp.json()
        assert body["type"] == "urn:problem:modulo:bad_request"

    def test_unhandled_exception_returns_500(self, client: TestClient) -> None:
        resp = client.get("/test/internal-error")
        assert resp.status_code == 500
        body = resp.json()
        assert body["type"] == "urn:problem:modulo:internal_error"
        assert body["title"] == "Internal Error"
        assert body["detail"] == "An unexpected error occurred"

    def test_validation_error(self, client: TestClient) -> None:
        resp = client.post("/test/validation", json={"name": ""})
        assert resp.status_code == 422
        body = resp.json()
        assert body["type"] == "urn:problem:modulo:validation_error"
        assert body["title"] == "Validation Error"
        assert body["detail"] is not None

    def test_request_id_header_present(self, client: TestClient) -> None:
        resp = client.get("/test/ok")
        assert "X-Request-ID" in resp.headers
        rid = resp.headers["X-Request-ID"]
        assert len(rid) > 0
        uuid.UUID(rid)

    def test_request_id_on_error(self, client: TestClient) -> None:
        resp = client.get("/test/not-found")
        assert "X-Request-ID" in resp.headers
        rid = resp.headers["X-Request-ID"]
        uuid.UUID(rid)
        body = resp.json()
        assert body["request_id"] == rid


class TestRequestIdMiddleware:
    def test_middleware_injects_request_id(self) -> None:
        app = FastAPI()
        app.add_middleware(RequestIdMiddleware)

        @app.get("/ping")
        async def ping() -> dict[str, str]:
            return {"pong": "ok"}

        with TestClient(app) as client:
            resp = client.get("/ping")
            assert "X-Request-ID" in resp.headers
            rid = resp.headers["X-Request-ID"]
            uuid.UUID(rid)

    def test_request_id_unique_per_request(self) -> None:
        app = FastAPI()
        app.add_middleware(RequestIdMiddleware)

        @app.get("/ping")
        async def ping() -> dict[str, str]:
            return {"pong": "ok"}

        with TestClient(app) as client:
            r1 = client.get("/ping")
            r2 = client.get("/ping")
            assert r1.headers["X-Request-ID"] != r2.headers["X-Request-ID"]
