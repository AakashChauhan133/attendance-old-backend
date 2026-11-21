"""
Tenant Activity Routes
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.api.dependencies import require_tenant_admin
from app.models.domain import Tenant
from app.services.activity_service import get_activity_feed

router = APIRouter()


@router.get("/activity")
async def tenant_activity(
    dept_id: int | None = Query(None, description="Filter by department ID"),
    limit: int = Query(50, ge=1, le=200),
    tenant: Tenant = Depends(require_tenant_admin),
    db: AsyncSession = Depends(get_db),
):
    """Tenant Manager: Recent activity (leaves, attendance, employees) across the organization"""
    return await get_activity_feed(db, tenant.id, dept_id=dept_id, limit=limit)
