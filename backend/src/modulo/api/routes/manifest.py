import logging

from fastapi import APIRouter, HTTPException

from modulo.api.db_error_handling import handle_db_errors
from modulo.core.manifest import get_manifest

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["manifest"])


@handle_db_errors("manifest.manifest_endpoint")
@router.get("/manifest")
async def manifest_endpoint() -> dict[str, object]:
    try:
        return get_manifest()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to load manifest: {exc}") from exc
