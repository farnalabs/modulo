from fastapi import APIRouter

from modulo.core.manifest import get_manifest

router = APIRouter(prefix="/api/v1", tags=["manifest"])


@router.get("/manifest")
async def manifest_endpoint():
    return get_manifest()
