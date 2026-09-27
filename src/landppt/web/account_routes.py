"""Authenticated export and deletion of the current user's account."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth.middleware import get_current_user_required
from ..database.database import AsyncSessionLocal
from ..database.models import User
from ..services.account_data_service import delete_user_account, export_user_data

router = APIRouter(prefix="/api/account", tags=["account"])


class DeleteAccountRequest(BaseModel):
    password: str


@router.get("/export")
async def export_account(user: User = Depends(get_current_user_required)):
    """Export only the authenticated user's data."""
    async with AsyncSessionLocal() as session:
        return await export_user_data(session, user.id)


@router.post("/delete")
async def delete_account(
    body: DeleteAccountRequest,
    user: User = Depends(get_current_user_required),
):
    """Delete the authenticated user's content after password confirmation."""
    async with AsyncSessionLocal() as session:
        deleted = await delete_user_account(session, user.id, body.password)
    if not deleted:
        raise HTTPException(status_code=403, detail="Password confirmation failed")
    return {"deleted": True}
