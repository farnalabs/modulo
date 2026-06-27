from typing import Any

from pydantic import BaseModel


class ErrorDetail(BaseModel):
    code: str
    message: str
    detail: str | None = None
    request_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Override to include backward-compatible top-level keys."""
        result = super().model_dump(*args, **kwargs)
        result["detail"] = self.error.message
        return result
