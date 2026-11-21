"""
Tenant Attendance Routes
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import date, datetime
from typing import Optional, List
from pydantic import BaseModel

from app.db.session import get_db
from app.api.dependencies import require_tenant_admin
from app.models.domain import Tenant
from app.services.attendance_service import get_today_summary, get_attendance_history
from app.services.tenant_settings_service import get_tenant_settings, calculate_valid_working_hours, calculate_late_status

router = APIRouter()


class EmployeeAttendanceResponse(BaseModel):
    id: int
    name: str
    employee_code: Optional[str]
    department_name: Optional[str]
    check_in: Optional[datetime]
    check_out: Optional[datetime]
    hours_worked: float
    met_min_hours: bool
    is_late: bool


class AttendanceSummaryResponse(BaseModel):
    date: date
    summary: dict
    employees: List[EmployeeAttendanceResponse]


@router.get("/attendance/today", response_model=AttendanceSummaryResponse)
async def get_today_attendance(
    dept_id: Optional[int] = Query(None, description="Filter by department ID"),
    tenant: Tenant = Depends(require_tenant_admin),
    db: AsyncSession = Depends(get_db)
):
    """Tenant Manager: Get today's attendance with working hours compliance"""
    result = await get_today_summary(tenant.id, db, dept_id)
    return result


@router.get("/attendance/date/{attendance_date}")
async def attendance_by_date(
    attendance_date: date,
    dept_id: Optional[int] = None,
    tenant: Tenant = Depends(require_tenant_admin),
    db: AsyncSession = Depends(get_db)
):
    """Tenant Manager: Get attendance for a specific date with working hours"""
    
    from sqlalchemy import text
    
    settings = await get_tenant_settings(tenant.id, db)
    
    query = """
        SELECT 
            u.id as user_id,
            u.name as user_name,
            u.employee_code,
            d.department_name,
            MIN(CASE WHEN a.record_type = 'IN' THEN a.timestamp END) as check_in,
            MAX(CASE WHEN a.record_type = 'OUT' THEN a.timestamp END) as check_out
        FROM users u
        LEFT JOIN departments d ON u.dept_id = d.department_id
        LEFT JOIN attendance_logs a ON u.id = a.user_id 
            AND DATE(a.timestamp) = :attendance_date
        WHERE u.tenant_id = :tenant_id
        AND u.role = 'employee'
        AND u.is_active = true
    """
    
    params = {
        "tenant_id": tenant.id,
        "attendance_date": attendance_date
    }
    
    if dept_id:
        query += " AND u.dept_id = :dept_id"
        params["dept_id"] = dept_id
    
    query += " GROUP BY u.id, u.name, u.employee_code, d.department_name ORDER BY u.name"
    
    result = await db.execute(text(query), params)
    rows = result.mappings().all()
    
    enriched_rows = []
    for row in rows:
        valid_hours, actual_duration, lost_hours, met_min_hours, status_msg = calculate_valid_working_hours(
            row["check_in"],
            row["check_out"],
            settings
        )
        
        is_late = False
        if row["check_in"]:
            is_late, _ = calculate_late_status(row["check_in"], settings)
        
        enriched_rows.append({
            "id": row["user_id"],
            "name": row["user_name"],
            "employee_code": row["employee_code"],
            "department_name": row["department_name"],
            "check_in": row["check_in"],
            "check_out": row["check_out"],
            "hours_worked": round(valid_hours, 2),
            "actual_duration": round(actual_duration, 2),
            "lost_hours": round(lost_hours, 2),
            "met_min_hours": met_min_hours,
            "is_late": is_late,
            "status_message": status_msg,
            "status": "present" if row["check_in"] else "absent"
        })
    
    total = len(enriched_rows)
    present = len([r for r in enriched_rows if r["check_in"]])
    late = len([r for r in enriched_rows if r["is_late"]])
    met_min = len([r for r in enriched_rows if r["met_min_hours"]])
    total_valid_hours = sum([r["hours_worked"] for r in enriched_rows])
    total_lost_hours = sum([r["lost_hours"] for r in enriched_rows])
    
    return {
        "date": attendance_date,
        "summary": {
            "total_employees": total,
            "present": present,
            "absent": total - present,
            "late_checkins": late,
            "on_time_checkins": present - late,
            "met_minimum_hours": met_min,
            "attendance_rate": round((present / total * 100), 1) if total > 0 else 0,
            "compliance_rate": round((met_min / present * 100), 1) if present > 0 else 0,
            "total_valid_hours": round(total_valid_hours, 1),
            "total_lost_hours": round(total_lost_hours, 1)
        },
        "employees": enriched_rows
    }


@router.get("/attendance/recent")
async def recent_attendance(
    limit: int = Query(20, ge=1, le=200),
    dept_id: Optional[int] = Query(None, description="Filter by department ID"),
    tenant: Tenant = Depends(require_tenant_admin),
    db: AsyncSession = Depends(get_db)
):
    """Tenant Manager: Most recent check-in / check-out events"""

    from sqlalchemy import text

    result = await db.execute(text("""
        SELECT
            a.id,
            a.user_id,
            u.name AS name,
            u.employee_code,
            d.department_name,
            a.device_id,
            a.record_type,
            a.timestamp
        FROM attendance_logs a
        LEFT JOIN users u ON u.id = a.user_id
        LEFT JOIN departments d ON d.department_id = u.dept_id
        WHERE a.tenant_id = :tenant_id
          AND (CAST(:dept_id AS INTEGER) IS NULL OR u.dept_id = CAST(:dept_id AS INTEGER))
        ORDER BY a.timestamp DESC
        LIMIT :limit
    """), {"tenant_id": tenant.id, "dept_id": dept_id, "limit": limit})

    return result.mappings().all()


@router.get("/attendance/stats")
async def attendance_stats(
    days: int = Query(7, ge=1, le=90, description="Trend window in days (incl. today)"),
    dept_id: Optional[int] = Query(None, description="Filter by department ID"),
    tenant: Tenant = Depends(require_tenant_admin),
    db: AsyncSession = Depends(get_db)
):
    """Tenant Manager: Attendance headline numbers for today plus a per-day trend"""

    from sqlalchemy import text
    from datetime import timedelta

    params = {"tenant_id": tenant.id, "dept_id": dept_id, "days": days}

    totals = await db.execute(text("""
        SELECT COUNT(*) AS total_employees
        FROM users u
        WHERE u.tenant_id = :tenant_id
          AND u.role = 'employee'
          AND u.is_active = true
          AND (CAST(:dept_id AS INTEGER) IS NULL OR u.dept_id = CAST(:dept_id AS INTEGER))
    """), params)
    total_employees = totals.scalar() or 0

    trend_rows = await db.execute(text("""
        SELECT
            (a.timestamp AT TIME ZONE 'Asia/Kolkata')::date AS day,
            COUNT(DISTINCT a.user_id) AS present
        FROM attendance_logs a
        JOIN users u ON u.id = a.user_id
        WHERE a.tenant_id = :tenant_id
          AND a.record_type = 'IN'
          AND u.role = 'employee'
          AND (CAST(:dept_id AS INTEGER) IS NULL OR u.dept_id = CAST(:dept_id AS INTEGER))
          AND (a.timestamp AT TIME ZONE 'Asia/Kolkata')::date
              >= (NOW() AT TIME ZONE 'Asia/Kolkata')::date - (CAST(:days AS INTEGER) - 1)
        GROUP BY 1
    """), params)
    present_by_day = {r["day"]: r["present"] for r in trend_rows.mappings().all()}

    today = (await db.execute(text("SELECT (NOW() AT TIME ZONE 'Asia/Kolkata')::date"))).scalar()
    trend = []
    for offset in range(days - 1, -1, -1):
        d = today - timedelta(days=offset)
        present = present_by_day.get(d, 0)
        trend.append({
            "date": d,
            "present": present,
            "absent": max(total_employees - present, 0),
            "attendance_rate": round(present / total_employees * 100, 1) if total_employees else 0
        })

    present_today = present_by_day.get(today, 0)
    return {
        "date": today,
        "total_employees": total_employees,
        "present_today": present_today,
        "absent_today": max(total_employees - present_today, 0),
        "attendance_rate_today": round(present_today / total_employees * 100, 1) if total_employees else 0,
        "average_attendance_rate": round(sum(t["attendance_rate"] for t in trend) / len(trend), 1) if trend else 0,
        "trend": trend
    }


@router.get("/attendance/settings")
async def get_attendance_settings(
    tenant: Tenant = Depends(require_tenant_admin),
    db: AsyncSession = Depends(get_db)
):
    """Get attendance-related settings for the tenant"""
    
    settings = await get_tenant_settings(tenant.id, db)
    
    return {
        "office_start_time": settings["office_start_time"],
        "office_start": settings["office_start"].strftime("%H:%M"),
        "office_end_time": settings["office_end_time"],
        "office_end": settings["office_end"].strftime("%H:%M"),
        "late_threshold_minutes": settings["late_threshold_minutes"],
        "min_working_hours": settings["min_working_hours"],
        "working_days": settings["working_days"]
    }