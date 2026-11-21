"""
Org Admin Activity Routes
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.api.dependencies import require_role
from app.models.domain import User
from app.services.activity_service import get_activity_feed

router = APIRouter()


@router.get("/activity")
async def department_activity(
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(require_role("org_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Org Admin: Recent activity (leaves, attendance, employees) for own department"""
    return await get_activity_feed(
        db, current_user.tenant_id, dept_id=current_user.dept_id, limit=limit
    )
