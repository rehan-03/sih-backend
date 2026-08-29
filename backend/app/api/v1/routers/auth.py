"""
app/api/v1/routers/auth.py — JWT auth endpoints.

Phase 0: shape-only implementation.
- /auth/login: accepts email+password, returns access + refresh tokens.
  In Phase 0 there's no user DB, so it accepts a single hard-coded dev credential
  (from env vars) so /docs testing works. Real user lookup in Phase 2.
- /auth/refresh: accepts a refresh token, returns a new access token.

Rules: router never touches SQLAlchemy directly — delegates to auth_service (stub).
"""
import logging

from fastapi import APIRouter, HTTPException, status

from app.core.config import get_settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    verify_refresh_token,
)
from app.schemas.auth import LoginRequest, LoginResponse, RefreshRequest, RefreshResponse
from app.schemas.common import ErrorEnvelope, UserRole

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/login",
    response_model=LoginResponse,
    responses={
        401: {"model": ErrorEnvelope, "description": "Invalid credentials"},
    },
    summary="Obtain JWT access + refresh tokens",
)
async def login(body: LoginRequest) -> LoginResponse:
    """
    Phase 0: authenticates against a single dev-user env var (DEV_USER_EMAIL /
    DEV_USER_PASSWORD) so the /docs UI is immediately testable.

    Phase 2 will replace this stub with real DB lookup + bcrypt verify.
    """
    # ── Stub: dev-only credential check ──────────────────────────────────────
    dev_email = "investigator@i4c.gov.in"
    dev_password = "devpass"   # intentionally weak — dev only; replaced in Phase 2
    dev_role = UserRole.investigator

    if body.email != dev_email or body.password != dev_password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "code": "INVALID_CREDENTIALS",
                    "message": "Email or password is incorrect.",
                    "details": {},
                }
            },
        )

    access_token = create_access_token(subject=body.email, role=dev_role.value)
    refresh_token = create_refresh_token(subject=body.email, role=dev_role.value)

    logger.info("login_success", extra={"email": body.email, "role": dev_role.value})

    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        role=dev_role,
    )


@router.post(
    "/refresh",
    response_model=RefreshResponse,
    responses={
        401: {"model": ErrorEnvelope, "description": "Invalid or expired refresh token"},
    },
    summary="Exchange a refresh token for a new access token",
)
async def refresh(body: RefreshRequest) -> RefreshResponse:
    exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "error": {
                "code": "UNAUTHORIZED",
                "message": "Invalid or expired refresh token.",
                "details": {},
            }
        },
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = verify_refresh_token(body.refresh_token)
    except Exception:
        raise exc

    sub = payload.get("sub")
    role = payload.get("role")
    if not sub or not role:
        raise exc

    new_access_token = create_access_token(subject=sub, role=role)
    return RefreshResponse(access_token=new_access_token)
