"""
Roles and permissions management API.

Only the Root account (username="admin") can create/edit/delete roles.
All authenticated users can read roles and permissions (for UI display).
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

import auth
from audit import log_audit
from database import get_db, AsyncSessionLocal
from models import Role, Permission, User, role_permissions

router = APIRouter(prefix="/api/roles", tags=["roles"])


# ── Read endpoints (any authenticated user) ────────────────────────────────────

@router.get("/permissions")
async def list_permissions(_user: dict = Depends(auth.require_auth), db: AsyncSession = Depends(get_db)):
    """Return all available permissions."""
    result = await db.execute(select(Permission).order_by(Permission.name))
    return [
        {"id": p.id, "name": p.name, "description": p.description}
        for p in result.scalars().all()
    ]


@router.get("")
async def list_roles(_user: dict = Depends(auth.require_auth), db: AsyncSession = Depends(get_db)):
    """Return all roles with their assigned permissions."""
    result = await db.execute(select(Role).order_by(Role.created_at))
    roles = result.scalars().all()
    out = []
    for r in roles:
        perm_res = await db.execute(
            select(Permission)
            .join(role_permissions, Permission.id == role_permissions.c.permission_id)
            .where(role_permissions.c.role_id == r.id)
            .order_by(Permission.name)
        )
        perms = perm_res.scalars().all()
        out.append({
            "id": r.id,
            "name": r.name,
            "description": r.description,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "permissions": [{"id": p.id, "name": p.name, "description": p.description} for p in perms],
        })
    return out


@router.get("/{role_id}")
async def get_role(role_id: int, _user: dict = Depends(auth.require_auth), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Role).where(Role.id == role_id))
    role = result.scalar_one_or_none()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    perm_res = await db.execute(
        select(Permission)
        .join(role_permissions, Permission.id == role_permissions.c.permission_id)
        .where(role_permissions.c.role_id == role.id)
        .order_by(Permission.name)
    )
    perms = perm_res.scalars().all()
    return {
        "id": role.id,
        "name": role.name,
        "description": role.description,
        "created_at": role.created_at.isoformat() if role.created_at else None,
        "permissions": [{"id": p.id, "name": p.name, "description": p.description} for p in perms],
    }


# ── Write endpoints (superadmin only) ─────────────────────────────────────────

class CreateRoleRequest(BaseModel):
    name: str
    description: Optional[str] = None
    permission_ids: list[int] = []


@router.post("")
async def create_role(
    req: CreateRoleRequest,
    current_user: dict = Depends(auth.require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    name = req.name.strip()
    if len(name) < 2:
        raise HTTPException(status_code=400, detail="Role name must be at least 2 characters")
    if name in ("Administrator", "Viewer"):
        raise HTTPException(status_code=400, detail="Cannot create a role named 'Administrator' or 'Viewer' (reserved)")
    existing = await db.execute(select(Role).where(Role.name == name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Role name already exists")

    role = Role(name=name, description=req.description)
    db.add(role)
    await db.flush()

    for pid in req.permission_ids:
        perm_res = await db.execute(select(Permission).where(Permission.id == pid))
        if not perm_res.scalar_one_or_none():
            raise HTTPException(status_code=400, detail=f"Permission {pid} not found")
        await db.execute(role_permissions.insert().values(role_id=role.id, permission_id=pid))

    await log_audit(db, "role.create", actor=current_user["username"], detail={"role": name, "permissions": req.permission_ids})
    await db.commit()
    await db.refresh(role)
    return {"id": role.id, "name": role.name, "description": role.description}


class UpdateRoleRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    permission_ids: Optional[list[int]] = None


@router.put("/{role_id}")
async def update_role(
    role_id: int,
    req: UpdateRoleRequest,
    current_user: dict = Depends(auth.require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Role).where(Role.id == role_id))
    role = result.scalar_one_or_none()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")

    detail: dict = {"role_id": role_id, "role": role.name}

    if req.name is not None:
        new_name = req.name.strip()
        if len(new_name) < 2:
            raise HTTPException(status_code=400, detail="Role name must be at least 2 characters")
        if role.name in ("Administrator", "Viewer") and new_name != role.name:
            raise HTTPException(status_code=400, detail="Cannot rename built-in roles 'Administrator' or 'Viewer'")
        existing = await db.execute(select(Role).where(Role.name == new_name, Role.id != role_id))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Role name already exists")
        role.name = new_name
        detail["new_name"] = new_name

    if req.description is not None:
        role.description = req.description

    if req.permission_ids is not None:
        if role.name == "Administrator":
            raise HTTPException(status_code=400, detail="Cannot change permissions of the built-in 'Administrator' role")
        # Replace all permissions
        await db.execute(delete(role_permissions).where(role_permissions.c.role_id == role_id))
        for pid in req.permission_ids:
            perm_res = await db.execute(select(Permission).where(Permission.id == pid))
            if not perm_res.scalar_one_or_none():
                raise HTTPException(status_code=400, detail=f"Permission {pid} not found")
            await db.execute(role_permissions.insert().values(role_id=role_id, permission_id=pid))
        detail["permissions"] = req.permission_ids

    await log_audit(db, "role.update", actor=current_user["username"], detail=detail)
    await db.commit()
    return {"id": role.id, "name": role.name, "description": role.description}


@router.delete("/{role_id}")
async def delete_role(
    role_id: int,
    current_user: dict = Depends(auth.require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Role).where(Role.id == role_id))
    role = result.scalar_one_or_none()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    if role.name in ("Administrator", "Viewer"):
        raise HTTPException(status_code=400, detail="Cannot delete built-in roles 'Administrator' or 'Viewer'")

    # Check if any users have this role
    users_res = await db.execute(select(User).where(User.role_id == role_id))
    if users_res.scalars().first():
        raise HTTPException(status_code=400, detail="Cannot delete a role that is assigned to users")

    await log_audit(db, "role.delete", actor=current_user["username"], detail={"role": role.name})
    await db.delete(role)
    await db.commit()
    return {"ok": True}
