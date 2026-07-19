"""Contract tests for the MCP setup handoff endpoint."""

from fastapi import FastAPI

from modulo.api.routes.mcp_setup import router


def test_complete_setup_accepts_unwrapped_request_body():
    app = FastAPI()
    app.include_router(router)

    operation = app.openapi()["paths"]["/api/v1/model-backends/{backend_id}/complete-setup"]["post"]
    body_schema = operation["requestBody"]["content"]["application/json"]["schema"]

    assert body_schema == {"$ref": "#/components/schemas/CompleteSetupRequest"}
    request_schema = app.openapi()["components"]["schemas"]["CompleteSetupRequest"]
    assert set(request_schema["properties"]) == {"token", "api_key"}
