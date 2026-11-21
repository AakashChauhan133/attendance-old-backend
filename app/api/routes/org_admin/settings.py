"""
Org Admin Settings Routes

Read and update the attendance settings (office hours, late threshold, working days,
minimum working hours) of the organization the org admin belongs to.
"""
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.api.dependencies import require_role
from app.models.domain import User
from app.api.routes.tenant.settings import invalidate_settings_cache as invalidate_tenant_route_cache
from app.services.tenant_settings_service import (
    get_tenant_settings,
    invalidate_tenant_settings_cache,
)

router = APIRouter()

_TIME_RE = re.compile(r"^([0-1][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]$")


class OrgSettingsUpdate(BaseModel):
    office_start_time: Optional[str] = None
    office_end_time: Optional[str] = None
    late_threshold_minutes: Optional[int] = None
    working_days: Optional[str] = None
    min_working_hours: Optional[float] = None

    @field_validator("office_start_time", "office_end_time")
    @classmethod
    def _time(cls, v):
        if v is not None and not _TIME_RE.match(v):
            raise ValueError(f"Invalid time format: {v}. Use HH:MM:SS")
        return v

    @field_validator("late_threshold_minutes")
    @classmethod
    def _late(cls, v):
        if v is not None and not 0 <= v <= 120:
            raise ValueError("Late threshold must be between 0 and 120 minutes")
        return v

    @field_validator("min_working_hours")
    @classmethod
    def _hours(cls, v):
        if v is not None and not 0.5 <= v <= 24:
            raise ValueError("Minimum working hours must be between 0.5 and 24 hours")
        return v

    @field_validator("working_days")
    @classmethod
    def _days(cls, v):
        if v is None:
            return v
        parts = [p.strip() for p in v.split(",") if p.strip()]
        if not parts or any(not p.isdigit() or not 1 <= int(p) <= 7 for p in parts):
            raise ValueError("working_days must be comma-separated numbers 1-7 (Mon=1)")
        return ",".join(sorted(set(parts), key=int))


def _public(settings: dict) -> dict:
    return {
        "office_start_time": settings["office_start_time"],
        "office_end_time": settings["office_end_time"],
        "late_threshold_minutes": settings["late_threshold_minutes"],
        "working_days": settings["working_days"],
        "min_working_hours": settings["min_working_hours"],
        "updated_at": settings.get("updated_at"),
    }


@router.get("/settings")
async def get_org_settings(
    current_user: User = Depends(require_role("org_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Org Admin: Get the organization's attendance settings"""
    return _public(await get_tenant_settings(current_user.tenant_id, db))


@router.put("/settings")
async def update_org_settings(
    data: OrgSettingsUpdate,
    current_user: User = Depends(require_role("org_admin")),
    db: AsyncSession = Depends(get_db),
):
    """Org Admin: Update the organization's attendance settings (only the fields sent)"""
    changes = data.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(400, "No settings provided")

    current = await get_tenant_settings(current_user.tenant_id, db, force_refresh=True)
    merged = {
        "office_start_time": current["office_start_time"],
        "office_end_time": current["office_end_time"],
        "late_threshold_minutes": current["late_threshold_minutes"],
        "working_days": current["working_days"],
        "min_working_hours": current["min_working_hours"],
        **changes,
    }
    if merged["office_start_time"] >= merged["office_end_time"]:
        raise HTTPException(400, "office_end_time must be after office_start_time")

    try:
        await db.execute(
            text(
                """
                INSERT INTO settings (tenant_id, office_start_time, office_end_time,
                    late_threshold_minutes, working_days, min_working_hours, updated_at)
                VALUES (:tenant_id, :office_start_time, :office_end_time,
                    :late_threshold_minutes, :working_days, :min_working_hours, NOW())
                ON CONFLICT (tenant_id) DO UPDATE SET
                    office_start_time = EXCLUDED.office_start_time,
                    office_end_time = EXCLUDED.office_end_time,
                    late_threshold_minutes = EXCLUDED.late_threshold_minutes,
                    working_days = EXCLUDED.working_days,
                    min_working_hours = EXCLUDED.min_working_hours,
                    updated_at = EXCLUDED.updated_at
                """
            ),
            {"tenant_id": current_user.tenant_id, **merged},
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(500, f"Failed to update settings: {exc}")

    invalidate_tenant_settings_cache(current_user.tenant_id)
    invalidate_tenant_route_cache(current_user.tenant_id)
    return {"message": "Settings updated successfully", "settings": merged}
