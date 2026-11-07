"""``/api/auth/*`` — a thin proxy to OpenNVR's login; the user shape the UI
reads is derived from core's ``/me``."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import schemas
from ..auth import public_user
from ..gate import current_user, get_gate

router = APIRouter()


async def _login_body(request: Request) -> Dict[str, Any]:
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    form = await request.form()
    return {k: form.get(k) for k in ("username", "password", "code")}


@router.post("/login")
@router.post("/login-json")
async def login(request: Request):
    """JSON ``{username, password, code?}`` (or the OAuth2 form the old UI
    sent). Returns core's token pair; 202 when a TOTP code is needed."""
    gate = get_gate()
    if gate.auth_mode == "none":
        return {"access_token": "dev", "refresh_token": "dev", "token_type": "bearer"}
    body = await _login_body(request)
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    code = str(body.get("code") or "").strip() or None
    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password are required")
    status, data = gate.auth.login(username, password, code, device_token=request.headers.get("x-device-token"),
                                   user_agent=request.headers.get("user-agent"))
    return JSONResponse(status_code=status, content=data)


@router.post("/refresh")
async def refresh(payload: schemas.RefreshRequest):
    gate = get_gate()
    if gate.auth_mode == "none":
        return {"access_token": "dev", "refresh_token": "dev", "token_type": "bearer"}
    status, data = gate.auth.refresh(payload.refresh_token)
    return JSONResponse(status_code=status, content=data)


@router.get("/me", response_model=schemas.UserOut)
async def me(user: Dict[str, Any] = Depends(current_user)):
    return schemas.UserOut(**public_user(user))


@router.post("/logout")
async def logout(request: Request):
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        get_gate().forget(auth[7:].strip())
    return {"ok": True}
