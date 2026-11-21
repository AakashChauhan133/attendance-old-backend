"""Attendance report (check-in / check-out / total time in office) shared by the
department-admin, tenant-admin and super-admin download endpoints.

Days are IST calendar days. For each employee and day: check-in = first punch of the day,
check-out = last punch of the day (blank when there is only one punch), total time = check-out - check-in.
"""
import calendar
import csv
import io
import re
from datetime import date, datetime, timedelta
from typing import Optional, Tuple

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

import pytz
from fastapi import HTTPException, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

IST = pytz.timezone("Asia/Kolkata")
MAX_DAYS = 366
_DAY_SQL = "(a.timestamp AT TIME ZONE 'Asia/Kolkata')::date"


def resolve_range(month: Optional[str], start_date: Optional[date], end_date: Optional[date]) -> Tuple[date, date]:
    """Option 1: month=YYYY-MM -> 1st..last day. Option 2: start_date + end_date (inclusive)."""
    if month:
        if start_date or end_date:
            raise HTTPException(400, "Use either month or start_date/end_date, not both")
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise HTTPException(400, "month must be YYYY-MM")
        y, m = int(month[:4]), int(month[5:])
        if y < 2000 or y > 2100:
            raise HTTPException(400, "month out of range")
        return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
    if not (start_date and end_date):
        raise HTTPException(400, "Provide month=YYYY-MM, or both start_date and end_date")
    if start_date > end_date:
        raise HTTPException(400, "start_date must not be after end_date")
    if (end_date - start_date).days + 1 > MAX_DAYS:
        raise HTTPException(400, f"Date range too long (max {MAX_DAYS} days)")
    return start_date, end_date


def _safe(s):  # neutralise spreadsheet formula injection from user-controlled names
    s = str(s)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _hhmm(td: Optional[timedelta]) -> str:
    if td is None:
        return ""
    mins = int(td.total_seconds() // 60)
    return f"{mins // 60:02d}:{mins % 60:02d}"


def _fmt_time(ts: Optional[datetime]) -> str:
    return ts.astimezone(IST).strftime("%H:%M:%S") if ts else ""


async def build_report(db: AsyncSession, tenant_id: int, start: date, end: date, dept_id: Optional[int] = None):
    """Returns (employees, rows). rows: one per employee-day with at least one punch."""
    # plain timestamp bounds (IST day edges, with a day of slack each side) let Postgres use the
    # (tenant_id, timestamp) index; the exact IST-date filter below still decides membership
    params = {"tenant_id": tenant_id, "start": start, "end": end,
              "ts_from": IST.localize(datetime.combine(start, datetime.min.time())),
              "ts_to": IST.localize(datetime.combine(end + timedelta(days=1), datetime.min.time()))}
    dept_sql = ""
    if dept_id is not None:
        dept_sql = "AND u.dept_id = :dept_id"
        params["dept_id"] = dept_id
    res = await db.execute(text(f"""
        SELECT u.id, u.name, u.employee_code, d.department_name,
               {_DAY_SQL} AS day,
               MIN(a.timestamp) AS check_in,
               MAX(a.timestamp) AS check_out
        FROM users u
        LEFT JOIN departments d ON d.department_id = u.dept_id
        JOIN attendance_logs a ON a.user_id = u.id AND a.tenant_id = u.tenant_id
             AND a.timestamp >= :ts_from AND a.timestamp < :ts_to
             AND {_DAY_SQL} BETWEEN :start AND :end
        WHERE u.tenant_id = :tenant_id AND u.role = 'employee' {dept_sql}
        GROUP BY u.id, u.name, u.employee_code, d.department_name, {_DAY_SQL}
        ORDER BY u.name, u.id, day
    """), params)
    rows = []
    for r in res.mappings().all():
        cin, cout = r["check_in"], r["check_out"]
        if cin and cout and cout <= cin:
            cout = None
        total = (cout - cin) if (cin and cout) else None
        rows.append({
            "user_id": r["id"], "name": r["name"], "employee_code": r["employee_code"] or "",
            "department": r["department_name"] or "", "date": r["day"],
            "check_in": cin, "check_out": cout, "total": total,
        })
    return rows


def to_csv_response(rows, start: date, end: date, filename_prefix: str) -> Response:
    buf = io.StringIO()
    buf.write("﻿")  # BOM so Excel reads UTF-8 names correctly
    w = csv.writer(buf)
    w.writerow(["Employee Code", "Name", "Department", "Date", "Check-in (IST)", "Check-out (IST)",
                "Total time in office (HH:MM)"])

    def flush(group):
        if not group:
            return
        days = len(group)
        total = sum((g["total"] for g in group if g["total"]), timedelta())
        g0 = group[0]
        w.writerow([_safe(g0["employee_code"]), _safe(g0["name"]), _safe(g0["department"]),
                    f"TOTAL ({days} day{'s' if days != 1 else ''} present)", "", "", _hhmm(total)])

    group, cur = [], None
    for r in rows:
        if cur is not None and r["user_id"] != cur:
            flush(group)
            group = []
        cur = r["user_id"]
        group.append(r)
        w.writerow([
            _safe(r["employee_code"]), _safe(r["name"]), _safe(r["department"]),
            r["date"].isoformat(), _fmt_time(r["check_in"]), _fmt_time(r["check_out"]),
            _hhmm(r["total"]) if r["total"] is not None else ("No check-out" if r["check_in"] else ""),
        ])
    flush(group)
    name = f"{filename_prefix}_{start.isoformat()}_to_{end.isoformat()}.csv"
    return Response(content=buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


async def _employees(db: AsyncSession, tenant_id: int, dept_id: Optional[int]):
    params = {"t": tenant_id}
    dsql = ""
    if dept_id is not None:
        dsql = "AND dept_id = :d"
        params["d"] = dept_id
    res = await db.execute(text(
        f"SELECT id, name, employee_code FROM users WHERE tenant_id = :t AND role = 'employee' "
        f"AND is_active = true {dsql} ORDER BY name, id"), params)
    return [dict(r) for r in res.mappings().all()]


def to_xlsx_response(employees, rows, start: date, end: date, filename_prefix: str) -> Response:
    """Matrix layout: one In/Out column pair per employee, one row per calendar day, plus
    'Days present' and 'Total time' rows at the bottom."""
    by_user = {}
    for r in rows:
        by_user.setdefault(r["user_id"], {})[r["date"]] = r
    known = {e["id"] for e in employees}
    for r in rows:  # employees deactivated since, but with punches in the range
        if r["user_id"] not in known:
            employees.append({"id": r["user_id"], "name": r["name"], "employee_code": r["employee_code"]})
            known.add(r["user_id"])

    wb = Workbook()
    ws = wb.active
    ws.title = "Attendance"
    thin = Side(style="thin", color="000000")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center")
    bold = Font(bold=True)
    head_fill = PatternFill("solid", fgColor="D9E1F2")

    ws["A4"] = "Date"
    ws["A4"].font = bold
    ws["A4"].border = box
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    first = 5
    last = first + len(days) - 1
    for i, d in enumerate(days):
        c = ws.cell(row=first + i, column=1, value=d)
        c.number_format = "dd-mmm-yy"
        c.font = bold
        c.border = box
    ws.cell(row=last + 1, column=1, value="Days present").font = bold
    ws.cell(row=last + 2, column=1, value="Total time (HH:MM)").font = bold
    for rr in (last + 1, last + 2):
        ws.cell(row=rr, column=1).border = box

    for k, e in enumerate(employees):
        c1 = 2 + 2 * k
        c2 = c1 + 1
        for row_i, val in ((1, e["name"]), (2, e["employee_code"] or "")):
            ws.merge_cells(start_row=row_i, start_column=c1, end_row=row_i, end_column=c2)
            cell = ws.cell(row=row_i, column=c1, value=_safe(val))
            cell.alignment = center
            cell.font = bold if row_i == 2 else Font()
            for cc in (c1, c2):
                ws.cell(row=row_i, column=cc).border = box
        for cc, label in ((c1, "In"), (c2, "Out")):
            cell = ws.cell(row=3, column=cc, value=label)
            cell.font = bold
            cell.alignment = center
            cell.fill = head_fill
            cell.border = box
        present, total = 0, timedelta()
        for i, d in enumerate(days):
            rec = by_user.get(e["id"], {}).get(d)
            for cc in (c1, c2):
                ws.cell(row=first + i, column=cc).border = box
                ws.cell(row=first + i, column=cc).alignment = center
            if not rec:
                continue
            present += 1
            if rec["check_in"]:
                c = ws.cell(row=first + i, column=c1, value=rec["check_in"].astimezone(IST).time().replace(microsecond=0))
                c.number_format = "hh:mm:ss"
            if rec["check_out"]:
                c = ws.cell(row=first + i, column=c2, value=rec["check_out"].astimezone(IST).time().replace(microsecond=0))
                c.number_format = "hh:mm:ss"
            if rec["total"]:
                total += rec["total"]
        for rr, val, fmt in ((last + 1, present, "0"), (last + 2, total.total_seconds() / 86400, "[h]:mm")):
            ws.merge_cells(start_row=rr, start_column=c1, end_row=rr, end_column=c2)
            cell = ws.cell(row=rr, column=c1, value=val)
            cell.number_format = fmt
            cell.font = bold
            cell.alignment = center
            for cc in (c1, c2):
                ws.cell(row=rr, column=cc).border = box
        ws.column_dimensions[get_column_letter(c1)].width = 11
        ws.column_dimensions[get_column_letter(c2)].width = 11
    ws.column_dimensions["A"].width = 20
    ws.freeze_panes = "B5"

    out = io.BytesIO()
    wb.save(out)
    name = f"{filename_prefix}_{start.isoformat()}_to_{end.isoformat()}.xlsx"
    return Response(
        content=out.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'})


async def respond(db: AsyncSession, tenant_id: int, start: date, end: date, dept_id: Optional[int],
                  fmt: str, filename_prefix: str = "attendance_report") -> Response:
    """fmt 'xlsx' (matrix, default) or 'csv' (one row per employee-day)."""
    if fmt not in ("xlsx", "csv"):
        raise HTTPException(400, "format must be 'xlsx' or 'csv'")
    rows = await build_report(db, tenant_id, start, end, dept_id)
    if fmt == "csv":
        return to_csv_response(rows, start, end, filename_prefix)
    return to_xlsx_response(await _employees(db, tenant_id, dept_id), rows, start, end, filename_prefix)
