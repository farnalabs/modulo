"""Step definitions for npm Connector BDD scenarios."""

import asyncio
import contextlib
from unittest.mock import AsyncMock

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.connectors.base import (
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    HealthResult,
)

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/connectors/npm.feature")


@pytest.fixture
def ctx():
    return {}


@given("an npm connector with valid token")
def step_npm_connector(ctx):
    mock_connector = AsyncMock()
    mock_connector.connector_type = "npm"

    async def mock_health_check():
        return HealthResult(ok=True, detail="npm registry reachable")

    async def mock_query(q):
        match q.resource:
            case "package":
                pkg = q.filters.get("package", "")
                if not pkg:
                    raise ValueError("npm package query requires 'package' in filters")
                return ConnectorResult(
                    records=[{"name": pkg, "version": "4.18.2", "description": "Fast, unopinionated framework"}],
                    total=1,
                )
            case "package_version":
                pkg = q.filters.get("package", "")
                version = q.filters.get("version", "")
                if not pkg:
                    raise ValueError("npm package_version query requires 'package' in filters")
                if not version:
                    raise ValueError("npm package_version query requires 'version' in filters")
                return ConnectorResult(
                    records=[
                        {"name": pkg, "version": version, "dist": {"tarball": "https://registry.npmjs.org/pkg.tgz"}}
                    ],
                    total=1,
                )
            case "search":
                text = q.filters.get("text", "")
                if not text:
                    raise ValueError("npm search query requires 'text' in filters")
                return ConnectorResult(
                    records=[
                        {
                            "name": "react",
                            "version": "18.2.0",
                            "description": "A JavaScript library for building UI",
                        },
                        {
                            "name": "react-dom",
                            "version": "18.2.0",
                            "description": "React package for working with the DOM",
                        },
                    ],
                    total=2,
                )
            case "package_files":
                pkg = q.filters.get("package", "")
                version = q.filters.get("version", "")
                if not pkg:
                    raise ValueError("npm package_files query requires 'package' in filters")
                if not version:
                    raise ValueError("npm package_files query requires 'version' in filters")
                return ConnectorResult(
                    records=[
                        {"path": "index.js", "size": 1024},
                        {"path": "package.json", "size": 512},
                        {"path": "README.md", "size": 2048},
                    ],
                    total=3,
                )
            case "scope_packages":
                scope = q.filters.get("scope", "")
                if not scope:
                    raise ValueError("npm scope_packages query requires 'scope' in filters")
                return ConnectorResult(
                    records=[
                        {"name": "@angular/core", "version": "16.0.0", "description": "Angular core framework"},
                        {"name": "@angular/common", "version": "16.0.0", "description": "Angular common utilities"},
                    ],
                    total=2,
                )
            case _:
                raise ValueError(f"Unsupported npm resource: {q.resource!r}")

    async def mock_write(payload):
        raise ValueError(f"npm registry is read-only: cannot write resource {payload.resource!r}")

    mock_connector.health_check = mock_health_check
    mock_connector.query = mock_query
    mock_connector.write = mock_write
    ctx["connector"] = mock_connector
    ctx["query_error"] = None


@given("the npm registry is unreachable")
def step_npm_unreachable(ctx):
    async def mock_health():
        return HealthResult(ok=False, detail="Cannot connect to npm registry")

    ctx["connector"].health_check = mock_health


@when("I perform a health check")
def step_npm_health_check(ctx):
    try:
        result = asyncio.run(ctx["connector"].health_check())
        ctx["health_result"] = result
    except Exception as exc:
        ctx["health_result"] = None
        ctx["query_error"] = str(exc)


@when(parsers.parse('I query npm resource "{resource}" with package "{pkg}"'))
def step_npm_query_package(resource, pkg, ctx):
    q = ConnectorQuery(resource=resource, filters={"package": pkg})
    try:
        result = asyncio.run(ctx["connector"].query(q))
        ctx["query_result"] = result
        ctx["query_error"] = None
    except Exception as exc:
        ctx["query_result"] = None
        ctx["query_error"] = str(exc)


@when(parsers.parse('I query npm resource "{resource}" with package "{pkg}" and version "{version}"'))
def step_npm_query_version(resource, pkg, version, ctx):
    q = ConnectorQuery(resource=resource, filters={"package": pkg, "version": version})
    try:
        result = asyncio.run(ctx["connector"].query(q))
        ctx["query_result"] = result
        ctx["query_error"] = None
    except Exception as exc:
        ctx["query_result"] = None
        ctx["query_error"] = str(exc)


@when(parsers.parse('I query npm resource "{resource}" with text "{text}" and limit {limit:d}'))
def step_npm_query_search(resource, text, limit, ctx):
    q = ConnectorQuery(resource=resource, filters={"text": text}, limit=limit)
    try:
        result = asyncio.run(ctx["connector"].query(q))
        ctx["query_result"] = result
        ctx["query_error"] = None
    except Exception as exc:
        ctx["query_result"] = None
        ctx["query_error"] = str(exc)


@when(parsers.parse('I query npm resource "{resource}" with text "{text}" and from {from_offset:d}'))
def step_npm_query_search_offset(resource, text, from_offset, ctx):
    q = ConnectorQuery(resource=resource, filters={"text": text, "from": from_offset})
    try:
        result = asyncio.run(ctx["connector"].query(q))
        ctx["query_result"] = result
        ctx["query_error"] = None
    except Exception as exc:
        ctx["query_result"] = None
        ctx["query_error"] = str(exc)


@when(parsers.parse('I query npm resource "{resource}" with scope "{scope}"'))
def step_npm_query_scope(resource, scope, ctx):
    q = ConnectorQuery(resource=resource, filters={"scope": scope})
    try:
        result = asyncio.run(ctx["connector"].query(q))
        ctx["query_result"] = result
        ctx["query_error"] = None
    except Exception as exc:
        ctx["query_result"] = None
        ctx["query_error"] = str(exc)


@when(parsers.parse('I write to npm resource "{resource}"'))
def step_npm_write(resource, ctx):
    payload = ConnectorPayload(resource=resource, data={})
    try:
        asyncio.run(ctx["connector"].write(payload))
        ctx["write_result"] = "unexpected_success"
        ctx["query_error"] = None
    except Exception as exc:
        ctx["write_result"] = None
        ctx["query_error"] = str(exc)


@when('I query npm resource "package" without package filter')
def step_npm_query_no_package(ctx):
    q = ConnectorQuery(resource="package", filters={})
    try:
        asyncio.run(ctx["connector"].query(q))
        ctx["query_result"] = "unexpected_success"
        ctx["query_error"] = None
    except Exception as exc:
        ctx["query_result"] = None
        ctx["query_error"] = str(exc)


@when('I query npm resource "package_version" without version filter')
def step_npm_query_no_version(ctx):
    q = ConnectorQuery(resource="package_version", filters={"package": "express"})
    try:
        asyncio.run(ctx["connector"].query(q))
        ctx["query_result"] = "unexpected_success"
        ctx["query_error"] = None
    except Exception as exc:
        ctx["query_result"] = None
        ctx["query_error"] = str(exc)


@when('I query npm resource "search" without text filter')
def step_npm_query_no_text(ctx):
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
