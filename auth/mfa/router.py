"""TOTP MFA routes (spec 4.3), behind ZUGACORE_ENTERPRISE=1.

    GET  /api/auth/mfa/status           enrolled / confirmed / required / this session verified
    POST /api/auth/mfa/enrol            -> otpauth URI, secret, 10 recovery codes (shown once)
    POST /api/auth/mfa/verify           {code}  TOTP or recovery code; marks this session
    POST /api/auth/mfa/disable          {code}  turn MFA off
    POST /api/auth/mfa/recovery-codes   {code}  replace the recovery codes

These routes are addressed to no org, so the per-org MFA requirement never
applies to them: a person who must verify can always reach them. The rules
(what counts as a valid code, throttling, why a recovery code is useless
before the authenticator is confirmed, the encrypted secret) live in
core.auth.mfa.service.
"""

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from core.auth.enterprise import require_enterprise
from core.auth.mfa import service
from core.auth.middleware import get_current_user
from core.auth.models import CurrentUser

router = APIRouter(
    prefix="/api/auth/mfa",
    tags=["mfa"],
    dependencies=[Depends(require_enterprise)],
)


class CodeBody(BaseModel):
    code: str = Field(min_length=1, max_length=64)


class EnrolResponse(BaseModel):
    otpauth_uri: str
    secret: str
    recovery_codes: list[str]


class StatusResponse(BaseModel):
    enrolled: bool
    confirmed: bool
    required: bool
    session_verified: bool
    recovery_codes_left: int


class VerifyResponse(BaseModel):
    verified: bool
    method: str
    session_verified: bool


class RecoveryCodesResponse(BaseModel):
    recovery_codes: list[str]


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.get("/status", response_model=StatusResponse)
async def mfa_status(user: CurrentUser = Depends(get_current_user)) -> StatusResponse:
    return StatusResponse(**await service.status(user))


@router.post("/enrol", response_model=EnrolResponse)
async def mfa_enrol(user: CurrentUser = Depends(get_current_user)) -> EnrolResponse:
    return EnrolResponse(**await service.enrol(user))


@router.post("/verify", response_model=VerifyResponse)
async def mfa_verify(body: CodeBody, request: Request, user: CurrentUser = Depends(get_current_user)) -> VerifyResponse:
    return VerifyResponse(**await service.verify(user, body.code, _ip(request)))


@router.post("/disable", status_code=204, response_class=Response)
async def mfa_disable(body: CodeBody, request: Request, user: CurrentUser = Depends(get_current_user)) -> Response:
    await service.disable(user, body.code, _ip(request))
    return Response(status_code=204)


@router.post("/recovery-codes", response_model=RecoveryCodesResponse)
async def mfa_recovery_codes(
    body: CodeBody, request: Request, user: CurrentUser = Depends(get_current_user)
) -> RecoveryCodesResponse:
    return RecoveryCodesResponse(recovery_codes=await service.regenerate_recovery_codes(user, body.code, _ip(request)))
