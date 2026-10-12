"""Unit tests for modulo.api.openapi.

The OpenAPI document must describe the body this API actually sends. FastAPI
auto-declares a ``422`` response shaped like its own ``HTTPValidationError``
(``detail`` as an array), while every error handler renders RFC 9457 Problem
Details with a string ``detail`` — the mismatch is exactly what the nightly
``Schemathesis API Fuzz`` gate fails on (``response_schema_conformance``). These
tests lock the corrected document and prove a real 422 body validates against
the schema the document advertises.
"""

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from modulo.api.openapi import (
    PROBLEM_DETAIL_NAME,
    PROBLEM_DETAIL_REF,
    VALIDATION_ERROR_REF,
    apply_error_response_schemas,
)


def _documented_responses(document: dict, status: str):
    for path_item in (document.get("paths") or {}).values():
        for operation in path_item.values():
            response = operation.get("responses", {}).get(status)
            if response is not None:
                yield response


def _live_document() -> dict:
    from modulo.api.main import app

    return app.openapi()


def _validator_for(document: dict, ref: str) -> Draft202012Validator:
    return Draft202012Validator({"$ref": ref, "components": document["components"]})


def _one_path(responses: dict) -> dict:
    return {"/things": {"get": {"responses": responses}}}


class TestApplyErrorResponseSchemas:
    """Direct coverage of the document rewrite, independent of app import."""

    def test_swaps_validation_error_refs_for_problem_details(self) -> None:
        document = {
            "paths": _one_path(
                {
                    "422": {"content": {"application/json": {"schema": {"$ref": VALIDATION_ERROR_REF}}}},
                    "200": {"description": "ok"},
                }
            ),
            "components": {
                "schemas": {"HTTPValidationError": {"type": "object"}, "ValidationError": {"type": "object"}}
            },
        }

        apply_error_response_schemas(document)

        responses = document["paths"]["/things"]["get"]["responses"]
        schema = responses["422"]["content"]["application/json"]["schema"]
        assert schema == {"$ref": PROBLEM_DETAIL_REF}
        assert document["components"]["schemas"][PROBLEM_DETAIL_NAME]["type"] == "object"
        assert "HTTPValidationError" not in document["components"]["schemas"]
        assert responses["200"] == {"description": "ok"}

    def test_is_idempotent(self) -> None:
        document = {
            "paths": _one_path({"422": {"schema": {"$ref": VALIDATION_ERROR_REF}}}),
            "components": {"schemas": {"HTTPValidationError": {"type": "object"}}},
        }

        apply_error_response_schemas(document)
        first = repr(document)

        apply_error_response_schemas(document)

        assert repr(document) == first

    def test_document_without_validation_responses_needs_no_problem_detail(self) -> None:
        """``swaps == 0`` with no existing component is a true no-op.

        A document that declares no request-validation response has nothing to
        rewrite and nothing to advertise, so the ``ProblemDetail`` component is
        not synthesised.
        """
        document = {"paths": _one_path({"200": {"description": "ok"}})}

        apply_error_response_schemas(document)

        assert PROBLEM_DETAIL_NAME not in document["components"]["schemas"]
        assert document["paths"]["/things"]["get"]["responses"] == {"200": {"description": "ok"}}

    def test_keeps_components_that_are_still_referenced(self) -> None:
        document = {
            "paths": _one_path({"422": {"schema": {"$ref": VALIDATION_ERROR_REF}}}),
            "components": {
                "schemas": {
                    "HTTPValidationError": {"properties": {"detail": {"$ref": "#/components/schemas/ValidationError"}}},
                    "ValidationError": {"type": "object"},
                    "Unrelated": {"properties": {"nested": {"$ref": "#/components/schemas/ValidationError"}}},
                }
            },
        }

        apply_error_response_schemas(document)

        assert "HTTPValidationError" not in document["components"]["schemas"]
        assert "ValidationError" in document["components"]["schemas"]


class TestLiveDocument:
    """The document the app serves, and the body it serves alongside it."""

    def test_no_fastapi_validation_error_ref_survives(self) -> None:
        document = _live_document()
        assert VALIDATION_ERROR_REF not in str(document)
        assert PROBLEM_DETAIL_REF in str(document)

    def test_every_declared_422_body_is_problem_details(self) -> None:
        document = _live_document()
        bodies = [
            response["content"]["application/json"]["schema"]
            for response in _documented_responses(document, "422")
            if "content" in response
        ]
        assert bodies, "no 422 response bodies declared — the rewrite did not run"
        assert all(schema == {"$ref": PROBLEM_DETAIL_REF} for schema in bodies)

    def test_real_validation_error_body_matches_the_declared_schema(self) -> None:
        from modulo.api.main import app

        client = TestClient(app)
        response = client.get("/api/v1/schemas", params=[("_fresh", "null")])

        assert response.status_code == 422, response.text
        body = response.json()
        assert body["type"] == "urn:problem:modulo:validation_error"
        assert isinstance(body["detail"], str)

        _validator_for(app.openapi(), PROBLEM_DETAIL_REF).validate(body)
