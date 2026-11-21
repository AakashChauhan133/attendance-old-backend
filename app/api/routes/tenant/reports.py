from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from typing import Optional
from datetime import date
from app.db.session import get_db
from app.api.dependencies import require_tenant_admin
from app.models.domain import Tenant

router = APIRouter()


@router.get("/reports/attendance")
async def attendance_report(
    start_date: date,
    end_date: date,
    dept_id: Optional[int] = None,
    tenant: Tenant = Depends(require_tenant_admin),
    db: AsyncSession = Depends(get_db)
):
    """Tenant Manager: Generate attendance report for date range"""
    
    query = """
        SELECT 
            u.id,
            u.name,
            u.employee_code,
            d.department_name,
            COUNT(DISTINCT DATE(a.timestamp)) FILTER (WHERE a.record_type = 'IN') as days_present,
            COUNT(DISTINCT DATE(a.timestamp)) FILTER (
                WHERE a.record_type = 'IN' 
                AND a.timestamp::time > '09:15:00'
            ) as days_late
        FROM users u
        LEFT JOIN departments d ON u.dept_id = d.department_id
        LEFT JOIN attendance_logs a ON u.id = a.user_id 
            AND DATE(a.timestamp) BETWEEN :start_date AND :end_date
        WHERE u.tenant_id = :tenant_id
        AND u.role = 'employee'
        AND u.is_active = true
    """
    
    params = {
        "tenant_id": tenant.id,
        "start_date": start_date,
        "end_date": end_date
    }
    
    if dept_id:
        query += " AND u.dept_id = :dept_id"
        params["dept_id"] = dept_id
    
    query += " GROUP BY u.id, d.department_name ORDER BY u.name"
    
    result = await db.execute(text(query), params)
    return result.mappings().all()


@router.get("/reports/department-summary")
async def department_summary(
    month: int,
    year: int,
    tenant: Tenant = Depends(require_tenant_admin),
    db: AsyncSession = Depends(get_db)
):
    """Tenant Manager: Get department-wise attendance summary"""
    
    result = await db.execute(text("""
        SELECT 
            d.department_id,
            d.department_name,
            COUNT(DISTINCT u.id) as total_employees,
            AVG(att.days_present) as avg_attendance_percentage
        FROM departments d
        LEFT JOIN users u ON d.department_id = u.dept_id 
            AND u.role = 'employee' 
            AND u.is_active = true
        LEFT JOIN (
            SELECT 
                user_id,
                COUNT(DISTINCT DATE(timestamp)) * 100.0 / 
                (SELECT COUNT(DISTINCT DATE(timestamp)) 
                 FROM attendance_logs 
                 WHERE EXTRACT(MONTH FROM timestamp) = :month
                 AND EXTRACT(YEAR FROM timestamp) = :year) as days_present
            FROM attendance_logs
            WHERE EXTRACT(MONTH FROM timestamp) = :month
            AND EXTRACT(YEAR FROM timestamp) = :year
            AND record_type = 'IN'
            GROUP BY user_id
        ) att ON u.id = att.user_id
        WHERE d.tenant_id = :tenant_id
        AND d.is_active = true
        GROUP BY d.department_id, d.department_name
        ORDER BY d.department_name
    """), {
        "tenant_id": tenant.id,
        "month": month,
        "year": year
    })
    
    return result.mappings().all()


@router.get("/reports/export")
async def export_report(
    report_type: str,
    start_date: date,
    end_date: date,
    tenant: Tenant = Depends(require_tenant_admin),
    db: AsyncSession = Depends(get_db)
):
    """Tenant Manager: Export report data"""
    
    # This is a placeholder - you can implement CSV generation here
    return {
        "message": f"Export ready for {report_type}",
        "format": "csv",
        "date_range": {
            "start": start_date,
            "end": end_date
        }
    }

@router.get("/reports/attendance/download")
async def download_attendance_report(
    month: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    dept_id: Optional[int] = None,
    format: str = "xlsx",
    tenant: Tenant = Depends(require_tenant_admin),
    db: AsyncSession = Depends(get_db)
):
    """Tenant (organisation) admin: CSV of check-in / check-out / total time per employee per day.
    ?month=YYYY-MM (1st..last day) OR ?start_date=&end_date= (custom); optional ?dept_id=."""
    from app.services.attendance_report_service import resolve_range, respond
    start, end = resolve_range(month, start_date, end_date)
    if dept_id is not None:
        ok = await db.execute(text("SELECT 1 FROM departments WHERE department_id = :d AND tenant_id = :t"),
                              {"d": dept_id, "t": tenant.id})
        if not ok.first():
            raise HTTPException(404, "Department not found")
    return await respond(db, tenant.id, start, end, dept_id, format)
