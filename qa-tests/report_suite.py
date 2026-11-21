"""Tests for the attendance report download (month / custom range) for dept admin, tenant admin, super admin.
Self-contained: run against a freshly created qa_test schema (qa_setup.py) with the QA backend on :8001.
Do NOT run api_suite.py before this on the same schema (both bootstrap the super admin)."""
import csv
import io
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import api_suite_lib as L  # noqa: E402  (helpers copied from api_suite)

_req = L.req


def req(method, path, *a, **k):
    """report downloads default to the xlsx matrix; the long-format tests below ask for csv explicitly."""
    if "/download" in path and "format=" not in path:
        path += ("&" if "?" in path else "?") + "format=csv"
    return _req(method, path, *a, **k)


check, expect, login, qa_sql, S = L.check, L.expect, L.login, L.qa_sql, L.S

SA_EMAIL, SA_PW = "rep.super@example.com", "RepSuper#12345"
EMP_PW = "Emp#Pass123"

S("setup")
expect("super admin bootstrap", req("POST", "/api/auth/setup/super-admin",
       body={"name": "Rep Super", "email": SA_EMAIL, "password": SA_PW}), 200)
SA, _ = login(SA_EMAIL, SA_PW)
tenants = {}
for name, mail in (("Rep Alpha", "rep.alpha@example.com"), ("Rep Beta", "rep.beta@example.com")):
    b = expect(f"create tenant {name}", req("POST", "/api/super-admin/tenants", SA, body={
        "organization_name": name, "manager_email": mail, "manager_password": "Mgr#Pass12345"}), 200)
    tenants[name] = b["tenant"]["id"]
A, B = tenants["Rep Alpha"], tenants["Rep Beta"]
AM, _ = login("rep.alpha@example.com", "Mgr#Pass12345")
BM, _ = login("rep.beta@example.com", "Mgr#Pass12345")
ENG = req("POST", "/api/tenant/departments", AM, body={"department_name": "Engineering"})[1]["department"]["department_id"]
HR = req("POST", "/api/tenant/departments", AM, body={"department_name": "HR"})[1]["department"]["department_id"]
BETA_D = req("POST", "/api/tenant/departments", BM, body={"department_name": "Ops"})[1]["department"]["department_id"]
EA = login("eng.admin@example.com", req("POST", "/api/tenant/org-admins", AM, body={
    "name": "Eng Admin", "email": "eng.admin@example.com", "dept_id": ENG})[1]["credentials"]["password"])[0]
HA = login("hr.admin@example.com", req("POST", "/api/tenant/org-admins", AM, body={
    "name": "HR Admin", "email": "hr.admin@example.com", "dept_id": HR})[1]["credentials"]["password"])[0]
check("org admins can log in", EA and HA)


def mk(token, code, name, fid):
    b = req("POST", "/api/org-admin/employees", token, body={"name": name, "employee_code": code, "password": EMP_PW, "finger_id": fid})
    return b[1]
for code, name, fid in (("E1", "Alice One", 1), ("E2", "Bob Two", 2), ("E3", "Cara Three", 3), ("E4", "=HYPERLINK(\"x\")", 4)):
    mk(EA, code, name, fid)
mk(HA, "H1", "Hank HR", 11)
mk(EA, "E5", "Eve Five", 5)
uid = {r[0]: r[1] for r in qa_sql("select employee_code, id from qa_test.users where employee_code is not null")}
ET, _ = login("E1", EMP_PW)


def punch(code, rtype, iso_utc, tenant=A, n=[0]):
    n[0] += 1
    qa_sql("insert into qa_test.attendance_logs (tenant_id, device_id, user_id, finger_id, record_type, timestamp) "
           "values (%s,'D1',%s,%s,%s,%s)", (tenant, uid[code], 100 + n[0], rtype, iso_utc))


# IST = UTC+5:30.  E1: 03 Mar 09:00-18:00 IST (9h) and 31 Mar 10:00-14:30 IST (4h30)  -> 13:30, 2 days in March
punch("E1", "IN", "2025-03-03T03:30:00Z"); punch("E1", "OUT", "2025-03-03T12:30:00Z")
punch("E1", "IN", "2025-03-31T04:30:00Z"); punch("E1", "OUT", "2025-03-31T09:00:00Z")
punch("E1", "IN", "2025-03-03T04:00:00Z")           # a later duplicate IN must not change check-in (MIN)
punch("E1", "IN", "2025-02-28T03:30:00Z"); punch("E1", "IN", "2025-04-01T03:30:00Z")  # outside March
# E2: IN only (no check-out)
punch("E2", "IN", "2025-03-05T04:00:00Z")
# E3: 23:59 IST on 31 Mar counts for March; 00:00 IST on 1 Apr (= 18:30Z) belongs to April
punch("E3", "IN", "2025-03-31T18:29:00Z"); punch("E3", "IN", "2025-03-31T18:30:00Z")
punch("E4", "IN", "2025-03-10T03:30:00Z"); punch("E4", "OUT", "2025-03-10T04:30:00Z")
punch("H1", "IN", "2025-03-03T03:30:00Z"); punch("H1", "OUT", "2025-03-03T11:30:00Z")


def get_csv(path, token):
    s, b = req("GET", path, token, raw=True)
    rows = list(csv.reader(io.StringIO(b.lstrip("﻿")))) if s == 200 else None
    return s, b, rows


ORG = "/api/org-admin/reports/download"
TEN = "/api/tenant/reports/attendance/download"
SUP = "/api/super-admin/reports/attendance/download"

S("department admin")
s, raw, rows = get_csv(f"{ORG}?month=2025-03", EA)
check("month report downloads", s == 200 and rows[0][:3] == ["Employee Code", "Name", "Department"], str(raw)[:120])
check("header has check-in, check-out, total time", rows and rows[0][4:] == ["Check-in (IST)", "Check-out (IST)", "Total time in office (HH:MM)"])
body = {tuple(r[:1] + r[3:4]): r for r in rows[1:]}
check("E1 03 Mar: 09:00:00 -> 18:00:00 = 09:00 (check-in is the first IN, IST)", body.get(("E1", "2025-03-03"), [0] * 7)[4:] == ["09:00:00", "18:00:00", "09:00"], str(body.get(("E1", "2025-03-03"))))
check("E1 31 Mar: 10:00 -> 14:30 = 04:30", body.get(("E1", "2025-03-31"), [0] * 7)[6] == "04:30")
check("E1 total row = 13:30 over 2 days", any(r[0] == "E1" and r[3].startswith("TOTAL (2 days") and r[6] == "13:30" for r in rows))
check("days outside the month are excluded (28 Feb / 1 Apr)", ("E1", "2025-02-28") not in body and ("E1", "2025-04-01") not in body)
check("no check-out is flagged", body.get(("E2", "2025-03-05"), [0] * 7)[6] == "No check-out", str(body.get(("E2", "2025-03-05"))))
check("23:59 IST on the last day is included", ("E3", "2025-03-31") in body)
check("00:00 IST next day (18:30Z) is NOT in March", sum(1 for r in rows if r[0] == "E3" and r[3] == "2025-04-01") == 0)
check("only own department (HR employee absent)", not any(r[0] == "H1" for r in rows))
check("spreadsheet formula in a name is neutralised", any(r[0] == "E4" and r[1].startswith("'=") for r in rows))
s, _, rows2 = get_csv(f"{ORG}?start_date=2025-03-03&end_date=2025-03-05", EA)
check("custom range 3-5 Mar: E1 + E2 only, 03 Mar & 05 Mar", s == 200 and {(r[0], r[3]) for r in rows2[1:] if not r[3].startswith("TOTAL")} == {("E1", "2025-03-03"), ("E2", "2025-03-05")}, str(rows2))
s, _, rows3 = get_csv(f"{ORG}?start_date=2025-03-03&end_date=2025-03-03", EA)
check("single-day custom range works", s == 200 and any(r[0] == "E1" for r in rows3[1:]))
s, _, rows4 = get_csv(f"{ORG}?month=2025-01", EA)
check("empty month gives a header-only file", s == 200 and len(rows4) == 1)
r = req("GET", "/api/org-admin/reports/download?month=2024-02", EA, raw=True)
expect("leap-year Feb ends on the 29th", req("GET", f"{ORG}?month=2024-02", EA, raw=True), 200)
import urllib.request  # filename check needs headers
rq = urllib.request.Request(L.BASE + f"{ORG}?month=2024-02", headers={"Authorization": f"Bearer {EA}"})
with urllib.request.urlopen(rq) as resp:
    cd = resp.headers.get("Content-Disposition", "")
check("filename carries 1st..last day", "2024-02-01_to_2024-02-29" in cd and "attachment" in cd, cd)
expect("HR admin sees only HR", req("GET", f"{ORG}?month=2025-03", HA, raw=True), 200, lambda b: "H1" in b and "E1" not in b)
for name, qs, code in (("bad month 2025-13", "month=2025-13", 400), ("bad month text", "month=March", 400),
                       ("month + dates together", "month=2025-03&start_date=2025-03-01&end_date=2025-03-02", 400),
                       ("nothing given", "", 400), ("only start_date", "start_date=2025-03-01", 400),
                       ("start after end", "start_date=2025-03-09&end_date=2025-03-01", 400),
                       ("range over 366 days", "start_date=2024-01-01&end_date=2025-06-01", 400),
                       ("garbage date", "start_date=abc&end_date=2025-03-01", 422)):
    expect(f"validation: {name}", req("GET", f"{ORG}?{qs}", EA), code)
expect("no token -> 401", req("GET", f"{ORG}?month=2025-03"), 401)
expect("employee -> 403", req("GET", f"{ORG}?month=2025-03", ET), 403)
expect("tenant admin -> 403 on dept-admin route", req("GET", f"{ORG}?month=2025-03", AM), 403)
expect("super admin -> 403 on dept-admin route", req("GET", f"{ORG}?month=2025-03", SA), 403)

S("tenant (organisation) admin")
expect("all departments: E1 and H1 present", req("GET", f"{TEN}?month=2025-03", AM, raw=True), 200, lambda b: "E1" in b and "H1" in b)
expect("filter by department", req("GET", f"{TEN}?month=2025-03&dept_id={HR}", AM, raw=True), 200, lambda b: "H1" in b and "E1" not in b)
expect("custom dates", req("GET", f"{TEN}?start_date=2025-03-01&end_date=2025-03-31", AM, raw=True), 200, lambda b: "E1" in b)
expect("other tenant's department -> 404", req("GET", f"{TEN}?month=2025-03&dept_id={BETA_D}", AM), 404)
expect("validation: nothing given", req("GET", TEN, AM), 400)
expect("dept admin -> 403", req("GET", f"{TEN}?month=2025-03", EA), 403)
expect("super admin -> 403", req("GET", f"{TEN}?month=2025-03", SA), 403)
expect("no token -> 401", req("GET", f"{TEN}?month=2025-03"), 401)
expect("Beta manager sees none of Alpha's staff", req("GET", f"{TEN}?month=2025-03", BM, raw=True), 200, lambda b: "E1" not in b and "H1" not in b)

S("super admin")
expect("any tenant, all departments", req("GET", f"{SUP}?tenant_id={A}&month=2025-03", SA, raw=True), 200, lambda b: "E1" in b and "H1" in b)
expect("tenant + department", req("GET", f"{SUP}?tenant_id={A}&month=2025-03&dept_id={HR}", SA, raw=True), 200, lambda b: "H1" in b and "E1" not in b)
expect("custom dates", req("GET", f"{SUP}?tenant_id={A}&start_date=2025-03-03&end_date=2025-03-05", SA, raw=True), 200, lambda b: "E1" in b and "E2" in b)
expect("Beta tenant has no Alpha data", req("GET", f"{SUP}?tenant_id={B}&month=2025-03", SA, raw=True), 200, lambda b: "E1" not in b)
expect("department of another tenant -> 404", req("GET", f"{SUP}?tenant_id={A}&month=2025-03&dept_id={BETA_D}", SA), 404)
expect("unknown tenant -> 404", req("GET", f"{SUP}?tenant_id=999999&month=2025-03", SA), 404)
expect("tenant_id is required -> 422", req("GET", f"{SUP}?month=2025-03", SA), 422)
expect("tenant admin -> 403", req("GET", f"{SUP}?tenant_id={A}&month=2025-03", AM), 403)
expect("dept admin -> 403", req("GET", f"{SUP}?tenant_id={A}&month=2025-03", EA), 403)
expect("no token -> 401", req("GET", f"{SUP}?tenant_id={A}&month=2025-03"), 401)
expect("departments list for the picker", req("GET", f"/api/super-admin/tenants/{A}/departments", SA), 200,
       lambda b: {d["name"] for d in b} == {"Engineering", "HR"})
expect("departments list: unknown tenant -> 404", req("GET", "/api/super-admin/tenants/999999/departments", SA), 404)
expect("departments list: tenant admin -> 403", req("GET", f"/api/super-admin/tenants/{A}/departments", AM), 403)

S("department summary (single call replaces per-day Analyze Records)")
SUM = "/api/org-admin/reports/summary"
sm = expect("summary for March", req("GET", f"{SUM}?start_date=2025-03-01&end_date=2025-03-31", EA), 200)
byc = {e["employee_code"]: e for e in sm["employees"]} if sm else {}
check("department name + 31 days", sm and sm["department"] == "Engineering" and sm["total_days"] == 31)
check("only own department's active employees (E1..E5, no HR)", set(byc) == {"E1", "E2", "E3", "E4", "E5"}, str(set(byc)))
check("Alice: 2 days present, 29 absent", byc["E1"]["present"] + byc["E1"]["late"] == 2 and byc["E1"]["absent"] == 29, str(byc.get("E1")))
check("employee without punches: 0 present, 31 absent, 0%", byc["E5"]["absent"] == 31 and byc["E5"]["attendance_rate"] == 0)
check("late uses tenant settings (default start 10:00 + 15 min): Alice on time both days, Cara 23:59 late", byc["E1"]["late"] == 0 and byc["E1"]["present"] == 2 and byc["E3"]["late"] == 1 and byc["E3"]["present"] == 0, str((byc["E1"], byc["E3"])))
expect("summary: start after end -> 400", req("GET", f"{SUM}?start_date=2025-03-09&end_date=2025-03-01", EA), 400)
expect("summary: missing dates -> 422", req("GET", SUM, EA), 422)
expect("summary: tenant admin -> 403", req("GET", f"{SUM}?start_date=2025-03-01&end_date=2025-03-02", AM), 403)
expect("summary: no token -> 401", req("GET", f"{SUM}?start_date=2025-03-01&end_date=2025-03-02"), 401)

S("excel matrix format")
import io as _io
import urllib.request as _ur
from datetime import time as _t
from openpyxl import load_workbook


def _hours(v):  # [h]:mm cells come back from openpyxl as timedelta (or a day-fraction float)
    return round((v.total_seconds() / 3600) if hasattr(v, "total_seconds") else v * 24, 4)


def xlsx(path, token):
    rq = _ur.Request(L.BASE + path, headers={"Authorization": f"Bearer {token}"})
    with _ur.urlopen(rq) as resp:
        return resp.headers.get("Content-Type", ""), resp.headers.get("Content-Disposition", ""), resp.read()


ctype, cd, data = xlsx(f"{ORG}?month=2025-03&format=xlsx", EA)
check("xlsx content type + filename", "spreadsheetml" in ctype and cd.endswith('_2025-03-01_to_2025-03-31.xlsx"'), f"{ctype} {cd}")
ws = load_workbook(_io.BytesIO(data)).active
names = {ws.cell(1, c).value: c for c in range(2, ws.max_column + 1, 2)}
check("row 1 = employee names, one column pair each (incl. employee with no punches)",
      set(names) == {"Alice One", "Bob Two", "Cara Three", "'=HYPERLINK(\"x\")", "Eve Five"}, str(set(names)))
c = names["Alice One"]
check("row 2 = employee code, row 3 = In / Out", ws.cell(2, c).value == "E1" and ws.cell(3, c).value == "In" and ws.cell(3, c + 1).value == "Out")
check("A4 'Date' label and one row per day (31)", ws["A4"].value == "Date" and ws["A5"].value.day == 1 and ws["A35"].value.day == 31)
r3 = 5 + 2  # 03 Mar
check("03 Mar In/Out = 09:00:00 / 18:00:00 (IST)", ws.cell(r3, c).value == _t(9, 0) and ws.cell(r3, c + 1).value == _t(18, 0), f"{ws.cell(r3, c).value} {ws.cell(r3, c + 1).value}")
check("days without punches are blank", ws.cell(5, c).value is None and ws.cell(5, c + 1).value is None)
check("Days present row = 2 for Alice", ws.cell(36, c).value == 2, str(ws.cell(36, c).value))
check("Total time row = 13:30 for Alice", _hours(ws.cell(37, c).value) == 13.5, str(ws.cell(37, c).value))
check("labels for the bottom rows", ws["A36"].value == "Days present" and ws["A37"].value.startswith("Total time"))
b = names["Bob Two"]
check("single punch: In filled, Out blank, 1 day present", ws.cell(5 + 4, b).value == _t(9, 30) and ws.cell(5 + 4, b + 1).value is None and ws.cell(36, b).value == 1)
e5 = names["Eve Five"]
check("employee with no punches: 0 days, 00:00", ws.cell(36, e5).value == 0 and _hours(ws.cell(37, e5).value) == 0)
check("last-day 23:59 IST punch is in March for Cara", ws.cell(35, names["Cara Three"]).value == _t(23, 59))
_, _, d2 = xlsx(f"{ORG}?start_date=2025-03-03&end_date=2025-03-05&format=xlsx", EA)
w2 = load_workbook(_io.BytesIO(d2)).active
check("custom range: only 3 date rows + 2 summary rows", w2["A5"].value.day == 3 and w2["A7"].value.day == 5 and w2["A8"].value == "Days present")
_, _, dt = xlsx(f"{TEN}?month=2025-03&format=xlsx", AM)
wt = load_workbook(_io.BytesIO(dt)).active
check("tenant admin xlsx has the HR employee too", "Hank HR" in {wt.cell(1, k).value for k in range(2, wt.max_column + 1, 2)})
_, _, dsu = xlsx(f"{SUP}?tenant_id={A}&month=2025-03&dept_id={HR}&format=xlsx", SA)
wsu = load_workbook(_io.BytesIO(dsu)).active
check("super admin xlsx filtered to HR", [wsu.cell(1, k).value for k in range(2, wsu.max_column + 1, 2)] == ["Hank HR"])
expect("unknown format -> 400", req("GET", f"{ORG}?month=2025-03&format=pdf", EA), 400)
check("xlsx file is a real zip/xlsx (PK magic)", data[:2] == b"PK")

fails = [r for r in L.results if not r[2]]
print("\n" + "=" * 70)
print(f"TOTAL {len(L.results)} | PASS {len(L.results) - len(fails)} | FAIL {len(fails)}")
for sec, name, ok, d in fails:
    print(f"  FAIL [{sec}] {name}\n      {d}")
sys.exit(1 if fails else 0)
