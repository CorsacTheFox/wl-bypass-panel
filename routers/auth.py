"""Auth routes: login (admin + client), logout, current user."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from security import get_current_user, request_token, revoke_session, create_session
from services import user_service

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str
    password: str


class LoginOut(BaseModel):
    token: str
    role: str
    username: str


@router.post("/login", response_model=LoginOut)
async def login(body: LoginIn):
    user = await user_service.authenticate(body.username, body.password)
    if user is None:
        # identical message for bad user vs bad password
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    token = await create_session(user["id"])
    return LoginOut(token=token, role=user["role"], username=user["username"])


@router.post("/logout")
async def logout(user=Depends(get_current_user), token: str = Depends(request_token)):
    if token:
        await revoke_session(token)
    return {"ok": True}


@router.get("/me")
async def me(user=Depends(get_current_user)):
    return {
        "id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "max_concurrent": user["max_concurrent"],
    }
