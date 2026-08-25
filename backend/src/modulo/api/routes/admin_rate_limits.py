import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from modulo.api.dependencies import require_feature, require_system_permission
from modulo.api.middleware.rate_limiter import RateLimitMiddleware, redis_available
from modulo.auth.jwt import TenantPrincipal
from modulo.core.rate_limiter import RateLimitRule

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


def _require_admin(principal: TenantPrincipal) -> None:
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can manage rate limits",
        )


@router.get("", dependencies=[require_feature("rate_limits")])
async def get_rate_limits(
    _current_user: TenantPrincipal = require_system_permission("system.config.manage"),  # type: ignore[assignment]
) -> RateLimitStatusResponse:
    rules = [
        RateLimitRuleResponse(path_prefix=r.path_prefix, max_requests=r.max_requests, window_s=r.window_s)
        for r in RateLimitMiddleware.RULES
    ]
    return RateLimitStatusResponse(
        mode="redis" if redis_available else "in_memory",
        rules=rules,
    )


@router.put("", dependencies=[require_feature("rate_limits")])
async def update_rate_limits(
    req: RateLimitUpdateRequest,
    _current_user: TenantPrincipal = require_system_permission("system.config.manage"),  # type: ignore[assignment]
) -> RateLimitStatusResponse:
    new_rules = [
        RateLimitRule(path_prefix=r.path_prefix, max_requests=r.max_requests, window_s=r.window_s) for r in req.rules
    ]
    if not new_rules:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one rate limit rule is required",
        )
    RateLimitMiddleware.set_rules(new_rules)
    _log.info("ratelimit.rules_updated", extra={"rules": new_rules})
    rules = [
        RateLimitRuleResponse(path_prefix=r.path_prefix, max_requests=r.max_requests, window_s=r.window_s)
        for r in RateLimitMiddleware.RULES
    ]
    return RateLimitStatusResponse(
        mode="redis" if redis_available else "in_memory",
        rules=rules,
    )
