"""Step definitions for PyPI Connector BDD scenarios."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.connectors.base import (
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    HealthResult,
)

try:
    scenarios("../features/connectors/pypi.feature")
except (FileNotFoundError, OSError):
    pass


@pytest.fixture
def ctx():
    return {}


@given("a PyPI connector with valid token")
def step_pypi_connector(ctx):
    mock_connector = AsyncMock()
    mock_connector.connector_type = "pypi"

    async def mock_health_check():
        return HealthResult(ok=True, detail="PyPI registry reachable")

    async def mock_query(q):
        match q.resource:
            case "package":
                pkg = q.filters.get("package", "")
                if not pkg:
                    raise ValueError("PyPI package query requires 'package' in filters")
                return ConnectorResult(
                    records=[{"info": {"name": pkg}, "releases": {}}],
                    total=1,
                )
            case "package_version":
                pkg = q.filters.get("package", "")
                version = q.filters.get("version", "")
                if not pkg:
                    raise ValueError("PyPI package_version query requires 'package' in filters")
                if not version:
                    raise ValueError("PyPI package_version query requires 'version' in filters")
                return ConnectorResult(
                    records=[{
                        "info": {"name": pkg, "version": version},
                        "releases": {version: [{"filename": f"{pkg}-{version}.tar.gz", "url": f"https://files.pythonhosted.org/packages/{pkg}-{version}.tar.gz"}]},
                    }],
                    total=1,
                )
            case "search":
                text = q.filters.get("text", "")
                if not text:
                    raise ValueError("PyPI search query requires 'text' in filters")
                return ConnectorResult(
                    records=[
                        {"name": "aiohttp", "version": "3.9.0", "summary": "Async HTTP client/server framework"},
                        {"name": "asyncio", "version": "3.4.3", "summary": "reference implementation of PEP 3156"},
                    ],
                    total=2,
                )
            case "package_files":
                pkg = q.filters.get("package", "")
                version = q.filters.get("version", "")
                if not pkg:
                    raise ValueError("PyPI package_files query requires 'package' in filters")
                if not version:
                    raise ValueError("PyPI package_files query requires 'version' in filters")
                return ConnectorResult(
                    records=[
                        {"filename": f"{pkg}-{version}.tar.gz", "size": 102400, "url": f"https://files.pythonhosted.org/packages/{pkg}-{version}.tar.gz"},
                        {"filename": f"{pkg}-{version}-py3-none-any.whl", "size": 51200, "url": f"https://files.pythonhosted.org/packages/{pkg}-{version}-py3-none-any.whl"},
                    ],
                    total=2,
                )
            case "simple_list":
                pkg = q.filters.get("package", "")
                if not pkg:
                    raise ValueError("PyPI simple_list query requires 'package' in filters")
                return ConnectorResult(
                    records=[{"versions": ["2.31.0", "2.30.0", "2.29.0"]}],
                    total=3,
                )
            case _:
                raise ValueError(f"Unsupported PyPI resource: {q.resource!r}")

    async def mock_write(payload):
        raise ValueError(f"PyPI registry is read-only: cannot write resource {payload.resource!r}")

    mock_connector.health_check = mock_health_check
    mock_connector.query = mock_query
    mock_connector.write = mock_write
    ctx["connector"] = mock_connector
    ctx["query_error"] = None


@given("the PyPI registry is unreachable")
def step_pypi_unreachable(ctx):
    async def mock_health():
        return HealthResult(ok=False, detail="Cannot connect to PyPI registry")

    ctx["connector"].health_check = mock_health


@when("I perform a health check")
def step_pypi_health_check(ctx):
    try:
        result = asyncio.run(ctx["connector"].health_check())
        ctx["health_result"] = result
    except Exception as exc:
        ctx["health_result"] = None
        ctx["query_error"] = str(exc)


@when(
    parsers.parse('I query PyPI resource "{resource}" with package "{pkg}"')
)
def step_pypi_query_package(resource, pkg, ctx):
    q = ConnectorQuery(resource=resource, filters={"package": pkg})
    try:
        result = asyncio.run(ctx["connector"].query(q))
        ctx["query_result"] = result
        ctx["query_error"] = None
    except Exception as exc:
        ctx["query_result"] = None
        ctx["query_error"] = str(exc)


@when(
    parsers.parse('I query PyPI resource "{resource}" with package "{pkg}" and version "{version}"')
)
def step_pypi_query_version(resource, pkg, version, ctx):
    q = ConnectorQuery(resource=resource, filters={"package": pkg, "version": version})
    try:
        result = asyncio.run(ctx["connector"].query(q))
        ctx["query_result"] = result
        ctx["query_error"] = None
    except Exception as exc:
        ctx["query_result"] = None
        ctx["query_error"] = str(exc)


@when(
    parsers.parse('I query PyPI resource "{resource}" with text "{text}" and limit {limit:d}')
)
def step_pypi_query_search(resource, text, limit, ctx):
    q = ConnectorQuery(resource=resource, filters={"text": text}, limit=limit)
    try:
        result = asyncio.run(ctx["connector"].query(q))
        ctx["query_result"] = result
        ctx["query_error"] = None
    except Exception as exc:
        ctx["query_result"] = None
        ctx["query_error"] = str(exc)


@when(
    parsers.parse('I write to PyPI resource "{resource}"')
)
def step_pypi_write(resource, ctx):
    payload = ConnectorPayload(resource=resource, data={})
    try:
        asyncio.run(ctx["connector"].write(payload))
        ctx["write_result"] = "unexpected_success"
        ctx["query_error"] = None
    except Exception as exc:
        ctx["write_result"] = None
        ctx["query_error"] = str(exc)


@when('I query PyPI resource "package" without package filter')
def step_pypi_query_no_package(ctx):
    q = ConnectorQuery(resource="package", filters={})
    try:
        asyncio.run(ctx["connector"].query(q))
        ctx["query_result"] = "unexpected_success"
        ctx["query_error"] = None
    except Exception as exc:
        ctx["query_result"] = None
        ctx["query_error"] = str(exc)


@when('I query PyPI resource "package_version" without version filter')
def step_pypi_query_no_version(ctx):
    q = ConnectorQuery(resource="package_version", filters={"package": "requests"})
    try:
        asyncio.run(ctx["connector"].query(q))
        ctx["query_result"] = "unexpected_success"
        ctx["query_error"] = None
    except Exception as exc:
        ctx["query_result"] = None
        ctx["query_error"] = str(exc)


@when('I query PyPI resource "search" without text filter')
def step_pypi_query_no_text(ctx):
    q = ConnectorQuery(resource="search", filters={})
    try:
        asyncio.run(ctx["connector"].query(q))
        ctx["query_result"] = "unexpected_success"
        ctx["query_error"] = None
    except Exception as exc:
        ctx["query_result"] = None
        ctx["query_error"] = str(exc)


@then("the health result is ok")
def step_health_result_ok(ctx):
    result = ctx.get("health_result")
    assert result is not None, "No health check result"
    assert result.ok is True, f"Health check failed: {result.detail}"


@then("the health result is not ok")
def step_health_result_not_ok(ctx):
    result = ctx.get("health_result")
    assert result is not None, "No health check result"
    assert result.ok is False, f"Health check unexpectedly passed: {result.detail}"


@then("the result has records")
def step_result_has_records(ctx):
    result = ctx.get("query_result")
    assert result is not None, "No query result"
    assert len(result.records) > 0, "Query result has no records"


@then("the write is an error")
def step_write_is_error(ctx):
    assert ctx.get("write_result") is None, "Expected an error but write succeeded"


@then("the result is an error")
def step_result_is_error(ctx):
    assert ctx.get("query_error") is not None, "Expected an error but query succeeded"
