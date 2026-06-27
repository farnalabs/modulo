import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from modulo.api.middleware.rate_limiter import RateLimitMiddleware, redis_available
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/rate-limits", tags=["admin-rate-limits"])


class RateLimitRuleResponse(BaseModel):
    path_prefix: str
    max_requests: int
    window_s: int


class RateLimitStatusResponse(BaseModel):
    mode: str
    rules: list[RateLimitRuleResponse]


class RateLimitRuleUpdate(BaseModel):
    path_prefix: str
    max_requests: int = Field(gt=0)
    window_s: int = Field(ge=1)


class RateLimitUpdateRequest(BaseModel):
    rules: list[RateLimitRuleUpdate]


def _require_admin(principal: AuthenticatedPrincipal) -> None:
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can manage rate limits",
        )


@router.get("", response_model=RateLimitStatusResponse)
async def get_rate_limits(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
) -> RateLimitStatusResponse:
    _require_admin(current_user)
    rules = [RateLimitRuleResponse(path_prefix=p, max_requests=m, window_s=w) for p, m, w in RateLimitMiddleware.RULES]
    return RateLimitStatusResponse(
        mode="redis" if redis_available else "in_memory",
        rules=rules,
    )


@router.put("", response_model=RateLimitStatusResponse)
async def update_rate_limits(
    body: RateLimitUpdateRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
) -> RateLimitStatusResponse:
    _require_admin(current_user)
    new_rules = [(r.path_prefix, r.max_requests, r.window_s) for r in body.rules]
    if not new_rules:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one rate limit rule is required",
        )
    RateLimitMiddleware.set_rules(new_rules)
    _log.info("ratelimit.rules_updated", extra={"rules": new_rules})
    rules = [RateLimitRuleResponse(path_prefix=p, max_requests=m, window_s=w) for p, m, w in RateLimitMiddleware.RULES]
    return RateLimitStatusResponse(
        mode="redis" if redis_available else "in_memory",
        rules=rules,
    )
