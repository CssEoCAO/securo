"""Trusted session bridge for a reverse proxy that already authenticated the user.

Used when Securo runs behind the Ratoeira gateway: the proxy checks the
signed identity Ratoeira sends (email + role), then calls this endpoint with
a shared secret to get a Securo session for that person. There is no
password and no login screen on that path.

Off unless `RATOEIRA_BRIDGE_SECRET` is set. The endpoint must only be
reachable by the proxy (the stack listens on 127.0.0.1); the secret is the
second lock.

Every person the proxy sends lands in one shared workspace
(`RATOEIRA_WORKSPACE_NAME`), with the workspace role mapped from the
proxy's role. The role is re-applied on every call, so a change made in
Ratoeira wins the next time the person opens the app.
"""
from __future__ import annotations

import hmac
import secrets
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import UserManager, get_jwt_strategy, get_user_manager
from app.core.config import get_settings
from app.core.database import get_async_session
from app.models.account import Account
from app.models.user import User
from app.models.workspace import Workspace

router = APIRouter(prefix="/api/ratoeira", tags=["ratoeira"])

# Ratoeira role -> (superuser, workspace role)
ROLE_MAP: dict[str, tuple[bool, str]] = {
    "admin": (True, "owner"),
    "financeiro": (False, "editor"),
    "leitura": (False, "viewer"),
}


class SessionRequest(BaseModel):
    email: EmailStr
    name: str = Field(default="", max_length=100)
    role: Literal["admin", "financeiro", "leitura"]


class SessionResponse(BaseModel):
    access_token: str
    mcp_token: str
    user_id: str
    workspace_id: str
    workspace_role: str
    expires_in: int


def _check_secret(given: str | None) -> None:
    expected = get_settings().ratoeira_bridge_secret.get_secret_value()
    if not expected:
        raise HTTPException(status_code=404, detail="Not Found")
    if not given or not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Unauthorized")


async def _shared_workspace(session: AsyncSession, creator: User) -> Workspace:
    from app.services.workspace_service import create_workspace

    name = get_settings().ratoeira_workspace_name
    found = (
        await session.execute(
            select(Workspace)
            .where(Workspace.name == name, Workspace.is_archived.is_(False))
            .order_by(Workspace.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if found:
        return found
    ws = await create_workspace(
        session,
        name=name,
        creator=creator,
        kind="personal",
        default_currency="BRL",
        locale="pt-BR",
        self_membership=False,
        seed_defaults=True,
    )
    # Access comes only from membership rows, which follow the Ratoeira role.
    ws.managed_by_user_id = None
    session.add(
        Account(
            user_id=creator.id,
            workspace_id=ws.id,
            name="Carteira",
            type="checking",
            balance=Decimal("0.00"),
            currency="BRL",
        )
    )
    await session.flush()
    return ws


@router.post("/session", response_model=SessionResponse)
async def bridge_session(
    body: SessionRequest,
    x_ratoeira_bridge: str | None = Header(default=None),
    session: AsyncSession = Depends(get_async_session),
    user_manager: UserManager = Depends(get_user_manager),
):
    _check_secret(x_ratoeira_bridge)
    from fastapi_users import schemas

    from app.agents.mcp.auth import mint_token
    from app.models.workspace import WorkspaceMember

    superuser, ws_role = ROLE_MAP[body.role]
    email = body.email.lower()

    user = (
        await session.execute(select(User).where(func.lower(User.email) == email))
    ).scalar_one_or_none()
    if user is None:
        created = await user_manager.create(
            schemas.BaseUserCreate(
                email=email,
                password=secrets.token_urlsafe(32),
                is_superuser=superuser,
                is_verified=True,
            )
        )
        user = await session.get(User, created.id)
        assert user is not None
        user.preferences = {
            "language": "pt-BR",
            "date_format": "DD/MM/YYYY",
            "currency_display": "BRL",
            "onboarding_completed": True,
        }

    prefs = dict(user.preferences or {})
    if body.name and prefs.get("display_name") != body.name:
        prefs["display_name"] = body.name
        user.preferences = prefs
    user.is_superuser = superuser
    user.is_active = True

    ws = await _shared_workspace(session, user)
    member = (
        await session.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == ws.id,
                WorkspaceMember.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if member is None:
        session.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=ws_role))
    elif member.role != ws_role:
        member.role = ws_role
    await session.commit()
    await session.refresh(user)

    settings = get_settings()
    access = await get_jwt_strategy().write_token(user)
    ttl = settings.access_token_expire_minutes * 60
    mcp = mint_token(user_id=user.id, workspace_id=ws.id, external=True, ttl_seconds=ttl)
    return SessionResponse(
        access_token=access,
        mcp_token=mcp,
        user_id=str(user.id),
        workspace_id=str(ws.id),
        workspace_role=ws_role,
        expires_in=ttl,
    )
