from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from datetime import datetime, date
from typing import Optional
from app.db.session import get_db
from app.api.dependencies import get_current_user
from app.models.domain import User
from app.services.tenant_settings_service import (
    get_tenant_settings, 
    calculate_valid_working_hours, 
    calculate_late_status
)

router = APIRouter()


@router.get("/attendance/today")
async def get_today_attendance(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Employee: Get today's attendance with working hours"""
    
    # Get tenant settings
    settings = await get_tenant_settings(current_user.tenant_id, db)
    
    query = text("""
        SELECT
            MIN(timestamp) FILTER (WHERE record_type = 'IN') as check_in,
            MAX(timestamp) FILTER (WHERE record_type = 'OUT') as check_out
        FROM attendance_logs
        WHERE user_id = :user_id
        AND DATE(timestamp) = CURRENT_DATE
    """)
    
    result = await db.execute(query, {"user_id": current_user.id})
    data = result.mappings().first()
    
    if not data or not data["check_in"]:
        return {
            "check_in": None,
            "check_out": None,
            "hours_worked": 0,
            "met_min_hours": False,
            "status": "absent",
            "is_late": False
        }
    
    valid_hours, actual_duration, lost_hours, met_min_hours, status_msg = calculate_valid_working_hours(
        data["check_in"],
        data["check_out"],
        settings
    )
    
    is_late, _ = calculate_late_status(data["check_in"], settings)
    
    return {
        "check_in": data["check_in"],
        "check_out": data["check_out"],
        "hours_worked": round(valid_hours, 2),
        "actual_duration": round(actual_duration, 2),
        "met_min_hours": met_min_hours,
        "is_late": is_late,
        "status": "present" if data["check_in"] else "absent"
    }


@router.get("/attendance")
async def get_attendance_history(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = 90
):
    """Employee: Get personal attendance history with working hours"""
    
    # Get tenant settings
    settings = await get_tenant_settings(current_user.tenant_id, db)
    
    query = text("""
        SELECT
            DATE(timestamp) as attendance_date,
            MIN(timestamp) FILTER (WHERE record_type = 'IN') as check_in,
            MAX(timestamp) FILTER (WHERE record_type = 'OUT') as check_out
        FROM attendance_logs
        WHERE user_id = :user_id
        GROUP BY DATE(timestamp)
        ORDER BY attendance_date DESC
        LIMIT :limit
    """)
    
    result = await db.execute(query, {
        "user_id": current_user.id,
        "limit": limit
    })
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
            "date": str(row["attendance_date"]),
            "check_in": row["check_in"],
            "check_out": row["check_out"],
            "hours_worked": round(valid_hours, 2),
            "met_min_hours": met_min_hours,
            "is_late": is_late,
            "status": "present" if row["check_in"] else "absent",
            "punctuality": "late" if is_late else "on_time"
        })
    
    return enriched_rows


@router.get("/attendance/stats")
async def get_attendance_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Employee: Get overall attendance statistics with working hours compliance"""
    
    # Get tenant settings
    settings = await get_tenant_settings(current_user.tenant_id, db)
    
    # Get all attendance records for the employee
    query = text("""
        SELECT
            DATE(timestamp) as attendance_date,
            MIN(timestamp) FILTER (WHERE record_type = 'IN') as check_in,
            MAX(timestamp) FILTER (WHERE record_type = 'OUT') as check_out
        FROM attendance_logs
        WHERE user_id = :user_id
        GROUP BY DATE(timestamp)
        ORDER BY attendance_date DESC
    """)
    
    result = await db.execute(query, {"user_id": current_user.id})
    rows = result.mappings().all()
    
    total_days = len(rows)
    days_with_checkin = 0
    days_met_min_hours = 0
    days_late = 0
    total_valid_hours = 0
    
    for row in rows:
        if row["check_in"]:
            days_with_checkin += 1
            
            valid_hours, _, _, met_min, _ = calculate_valid_working_hours(
                row["check_in"],
                row["check_out"],
                settings
            )
            total_valid_hours += valid_hours
            
            if met_min:
                days_met_min_hours += 1
            
            is_late, _ = calculate_late_status(row["check_in"], settings)
            if is_late:
                days_late += 1
    
    return {
        "total_days_worked": days_with_checkin,
        "total_valid_hours": round(total_valid_hours, 2),
        "days_met_min_hours": days_met_min_hours,
        "days_late": days_late,
        "compliance_rate": round((days_met_min_hours / days_with_checkin * 100), 1) if days_with_checkin > 0 else 0,
        "average_hours": round(total_valid_hours / days_with_checkin, 1) if days_with_checkin > 0 else 0
    }


@router.get("/attendance/stats/monthly")
async def get_monthly_stats(
    month: int,
    year: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Employee: Get monthly attendance statistics with working hours compliance"""
    
    # Get tenant settings
    settings = await get_tenant_settings(current_user.tenant_id, db)
    
    # Get all attendance records for the month
    query = text("""
        SELECT
            DATE(timestamp) as attendance_date,
            MIN(timestamp) FILTER (WHERE record_type = 'IN') as check_in,
            MAX(timestamp) FILTER (WHERE record_type = 'OUT') as check_out
        FROM attendance_logs
        WHERE user_id = :user_id
        AND EXTRACT(MONTH FROM timestamp) = :month
        AND EXTRACT(YEAR FROM timestamp) = :year
        GROUP BY DATE(timestamp)
    """)
    
    result = await db.execute(query, {
        "user_id": current_user.id,
        "month": month,
        "year": year
    })
    rows = result.mappings().all()
    
    # Calculate statistics
    present_days = 0
    days_met_min_hours = 0
    late_days = 0
    on_time_days = 0
    total_valid_hours = 0
    
    for row in rows:
        if row["check_in"]:
            present_days += 1
            
            valid_hours, _, _, met_min, _ = calculate_valid_working_hours(
                row["check_in"],
                row["check_out"],
                settings
            )
            total_valid_hours += valid_hours
            
            if met_min:
                days_met_min_hours += 1
            
            is_late, _ = calculate_late_status(row["check_in"], settings)
            if is_late:
                late_days += 1
            else:
                on_time_days += 1
    
    # Calculate working days in month (Mon-Fri)
    from calendar import monthrange
    import datetime as dt
    
    first_day = dt.date(year, month, 1)
    last_day = dt.date(year, month, monthrange(year, month)[1])
    
    working_days = 0
    current = first_day
    while current <= last_day:
        if current.weekday() < 5:  # Monday to Friday
            working_days += 1
        current += dt.timedelta(days=1)
    
    absent_days = max(0, working_days - present_days)
    attendance_percentage = round((present_days / working_days * 100), 1) if working_days > 0 else 0
    compliance_rate = round((days_met_min_hours / present_days * 100), 1) if present_days > 0 else 0
    avg_hours = round(total_valid_hours / present_days, 1) if present_days > 0 else 0
    
    return {
        "present_days": present_days,
        "days_met_min_hours": days_met_min_hours,
        "total_hours": round(total_valid_hours, 1),
        "attendance_percentage": attendance_percentage,
        "total_days_in_month": monthrange(year, month)[1],
        "estimated_working_days": working_days,
        "absent_days": absent_days,
        "late_days": late_days,
        "on_time_days": on_time_days,
        "overtime_hours": 0,
        "average_hours_per_day": avg_hours,
        "compliance_rate": compliance_rate
    }


# NOTE: the fixed-path routes below (calendar, export, hours/summary) must stay above
# "/attendance/{date_str}" so they are matched before the catch-all date route.

_IST_SQL = "(timestamp AT TIME ZONE 'Asia/Kolkata')::date"


def _month_bounds(month: Optional[int], year: Optional[int]):
    """Validate month/year (defaults to the current IST month) and return (month, year, first, last)."""
    import pytz
    from calendar import monthrange

    now = datetime.now(pytz.timezone("Asia/Kolkata"))
    month = month or now.month
    year = year or now.year
    if not 1 <= month <= 12:
        raise HTTPException(400, "month must be between 1 and 12")
    if not 2000 <= year <= 2100:
        raise HTTPException(400, "year must be between 2000 and 2100")
    return month, year, date(year, month, 1), date(year, month, monthrange(year, month)[1])


async def _daily_records(db: AsyncSession, user_id: int, start: date, end: date):
    """One row per IST date with first IN and last OUT for a user in [start, end]."""
    result = await db.execute(text(f"""
        SELECT
            {_IST_SQL} AS attendance_date,
            MIN(timestamp) FILTER (WHERE record_type = 'IN') AS check_in,
            MAX(timestamp) FILTER (WHERE record_type = 'OUT') AS check_out
        FROM attendance_logs
        WHERE user_id = :user_id
          AND {_IST_SQL} BETWEEN :start AND :end
        GROUP BY 1
        ORDER BY 1
    """), {"user_id": user_id, "start": start, "end": end})
    return result.mappings().all()


@router.get("/attendance/hours/summary")
async def get_hours_summary(
    month: Optional[int] = None,
    year: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Employee: Working-hours summary for a month (defaults to the current month)"""

    month, year, first, last = _month_bounds(month, year)
    settings = await get_tenant_settings(current_user.tenant_id, db)
    rows = await _daily_records(db, current_user.id, first, last)

    days = []
    total_hours = 0.0
    total_lost = 0.0
    met_min_days = 0
    for row in rows:
        if not row["check_in"]:
            continue
        valid, actual, lost, met_min, status_msg = calculate_valid_working_hours(
            row["check_in"], row["check_out"], settings
        )
        is_late, _ = calculate_late_status(row["check_in"], settings)
        total_hours += valid
        total_lost += lost
        met_min_days += 1 if met_min else 0
        days.append({
            "date": row["attendance_date"],
            "check_in": row["check_in"],
            "check_out": row["check_out"],
            "hours_worked": round(valid, 2),
            "actual_duration": round(actual, 2),
            "lost_hours": round(lost, 2),
            "met_min_hours": met_min,
            "is_late": is_late,
            "status_message": status_msg
        })

    present = len(days)
    return {
        "month": month,
        "year": year,
        "min_working_hours": settings["min_working_hours"],
        "days_present": present,
        "days_met_min_hours": met_min_days,
        "total_hours": round(total_hours, 2),
        "total_lost_hours": round(total_lost, 2),
        "average_hours_per_day": round(total_hours / present, 2) if present else 0,
        "days": days
    }


@router.get("/attendance/calendar")
async def get_attendance_calendar(
    month: Optional[int] = None,
    year: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Employee: Month calendar with a status per day
    (present | late | absent | leave | holiday | weekend | upcoming)"""

    import pytz
    from datetime import timedelta
    from app.services.tenant_settings_service import is_working_day

    month, year, first, last = _month_bounds(month, year)
    settings = await get_tenant_settings(current_user.tenant_id, db)
    today = datetime.now(pytz.timezone("Asia/Kolkata")).date()

    records = {r["attendance_date"]: r for r in await _daily_records(db, current_user.id, first, last)}

    holiday_rows = await db.execute(text("""
        SELECT holiday_date::date AS day, name
        FROM holidays
        WHERE tenant_id = :tenant_id AND holiday_date::date BETWEEN :start AND :end
    """), {"tenant_id": current_user.tenant_id, "start": first, "end": last})
    holidays = {r["day"]: r["name"] for r in holiday_rows.mappings().all()}

    leave_rows = await db.execute(text("""
        SELECT start_date::date AS start_day, end_date::date AS end_day, leave_type
        FROM leaves
        WHERE employee_id = :user_id
          AND status = 'approved'
          AND end_date::date >= :start AND start_date::date <= :end
    """), {"user_id": current_user.id, "start": first, "end": last})
    leaves = leave_rows.mappings().all()

    def leave_on(d):
        for lv in leaves:
            if lv["start_day"] <= d <= lv["end_day"]:
                return lv["leave_type"]
        return None

    days = []
    summary = {"present": 0, "late": 0, "absent": 0, "leave": 0, "holiday": 0, "weekend": 0}
    d = first
    while d <= last:
        rec = records.get(d)
        entry = {"date": d, "day": d.day, "weekday": d.strftime("%a"),
                 "check_in": None, "check_out": None, "hours_worked": 0, "note": None}

        if rec and rec["check_in"]:
            valid, _, _, met_min, _ = calculate_valid_working_hours(rec["check_in"], rec["check_out"], settings)
            is_late, _ = calculate_late_status(rec["check_in"], settings)
            entry.update({"check_in": rec["check_in"], "check_out": rec["check_out"],
                          "hours_worked": round(valid, 2), "met_min_hours": met_min})
            entry["status"] = "late" if is_late else "present"
        elif d in holidays:
            entry["status"] = "holiday"
            entry["note"] = holidays[d]
        elif leave_on(d):
            entry["status"] = "leave"
            entry["note"] = leave_on(d)
        elif not is_working_day(d, settings):
            entry["status"] = "weekend"
        elif d > today:
            entry["status"] = "upcoming"
        elif d == today:
            entry["status"] = "upcoming"  # day not finished and no check-in yet
        else:
            entry["status"] = "absent"

        if entry["status"] in summary:
            summary[entry["status"]] += 1
        # a late day is still a day present
        if entry["status"] == "late":
            summary["present"] += 1
        days.append(entry)
        d += timedelta(days=1)

    return {"month": month, "year": year, "summary": summary, "days": days}


@router.get("/attendance/export")
async def export_attendance(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    format: str = "json",
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Employee: Export own attendance (JSON by default, ?format=csv for a CSV download).
    Optional ?start_date= and ?end_date= (YYYY-MM-DD); defaults to all records."""

    from fastapi import Response

    if format not in ("json", "csv"):
        raise HTTPException(400, "format must be 'json' or 'csv'")
    if start_date and end_date and start_date > end_date:
        raise HTTPException(400, "start_date must not be after end_date")

    settings = await get_tenant_settings(current_user.tenant_id, db)
    result = await db.execute(text(f"""
        SELECT
            {_IST_SQL} AS attendance_date,
            MIN(timestamp) FILTER (WHERE record_type = 'IN') AS check_in,
            MAX(timestamp) FILTER (WHERE record_type = 'OUT') AS check_out
        FROM attendance_logs
        WHERE user_id = :user_id
          AND (CAST(:start AS DATE) IS NULL OR {_IST_SQL} >= CAST(:start AS DATE))
          AND (CAST(:end AS DATE) IS NULL OR {_IST_SQL} <= CAST(:end AS DATE))
        GROUP BY 1
        ORDER BY 1 DESC
    """), {"user_id": current_user.id, "start": start_date, "end": end_date})

    records = []
    for row in result.mappings().all():
        if row["check_in"]:
            valid, _, _, met_min, _ = calculate_valid_working_hours(row["check_in"], row["check_out"], settings)
            is_late, _ = calculate_late_status(row["check_in"], settings)
        else:
            valid, met_min, is_late = 0, False, False
        records.append({
            "date": row["attendance_date"],
            "check_in": row["check_in"],
            "check_out": row["check_out"],
            "hours_worked": round(valid, 2),
            "met_min_hours": met_min,
            "is_late": is_late,
            "status": "present" if row["check_in"] else "absent"
        })

    if format == "csv":
        import csv
        import io
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["date", "check_in", "check_out", "hours_worked", "met_min_hours", "is_late", "status"])
        for r in records:
            writer.writerow([r["date"], r["check_in"], r["check_out"], r["hours_worked"],
                             r["met_min_hours"], r["is_late"], r["status"]])
        return Response(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="attendance.csv"'}
        )

    return {
        "employee": {
            "id": current_user.id,
            "name": current_user.name,
            "employee_code": current_user.employee_code
        },
        "total_records": len(records),
        "records": records
    }


@router.get("/attendance/{date_str}")
async def get_attendance_by_date(
    date_str: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Employee: Get attendance for a specific date (YYYY-MM-DD)"""
    
    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "Invalid date format. Use YYYY-MM-DD")
    
    # Get tenant settings
    settings = await get_tenant_settings(current_user.tenant_id, db)
    
    query = text("""
        SELECT
            MIN(timestamp) FILTER (WHERE record_type = 'IN') as check_in,
            MAX(timestamp) FILTER (WHERE record_type = 'OUT') as check_out
        FROM attendance_logs
        WHERE user_id = :user_id AND DATE(timestamp) = :target_date
    """)
    
    result = await db.execute(query, {
        "user_id": current_user.id,
        "target_date": target_date
    })
    data = result.mappings().first()
    
    if not data or not data["check_in"]:
        return {
            "date": date_str,
            "check_in": None,
            "check_out": None,
            "hours_worked": 0,
            "met_min_hours": False,
            "status": "absent",
            "is_late": False
        }
    
    valid_hours, actual_duration, lost_hours, met_min_hours, status_msg = calculate_valid_working_hours(
        data["check_in"],
        data["check_out"],
        settings
    )
    
    is_late, _ = calculate_late_status(data["check_in"], settings)
    
    return {
        "date": date_str,
        "check_in": data["check_in"],
        "check_out": data["check_out"],
        "hours_worked": round(valid_hours, 2),
        "met_min_hours": met_min_hours,
        "is_late": is_late,
        "status": "present" if data["check_in"] else "absent"
    }