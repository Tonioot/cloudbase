import json

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import AuditLog

router = APIRouter(prefix="/api/audit-log", tags=["audit"])


@router.get("")
async def get_audit_log(
    limit: int = Query(100, ge=1, le=500),
    app_id: int = Query(None),
    db: AsyncSession = Depends(get_db),
):
    query = select(AuditLog).order_by(desc(AuditLog.timestamp)).limit(limit)
    if app_id is not None:
        query = query.where(AuditLog.app_id == app_id)
    result = await db.execute(query)
    entries = result.scalars().all()
    return [
        {
            "id": e.id,
            "timestamp": e.timestamp.isoformat() + "Z",
            "action": e.action,
            "actor": e.actor,
            "app_id": e.app_id,
            "detail": json.loads(e.detail) if e.detail else None,
        }
        for e in entries
    ]
