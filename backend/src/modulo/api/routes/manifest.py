from fastapi import APIRouter, HTTPException

from modulo.core.manifest import get_manifest

router = APIRouter(prefix="/api/v1", tags=["manifest"])


@router.get("/manifest")
async def manifest_endpoint():
    try:
        return get_manifest()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to load manifest: {exc}") from exc
