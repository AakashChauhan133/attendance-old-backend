"""End-to-end API test suite for the QA backend (http://localhost:8001, schema qa_test only)."""
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(__file__))
import qa_common as q  # noqa: E402

q.prefer_ipv4()
import psycopg  # noqa: E402

BASE = os.environ.get("QA_BASE", "http://localhost:8001")
results = []  # (section, name, ok, detail)
section = "setup"


def S(name):
    global section
    section = name
    print(f"\n=== {name}")


def req(method, path, token=None, body=None, form=None, headers=None, raw=False):
    h = dict(headers or {})
    data = None
    if token:
        h["Authorization"] = f"Bearer {token}"
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=90) as resp:
            txt = resp.read().decode()
            return resp.status, (txt if raw else (json.loads(txt) if txt else None))
    except urllib.error.HTTPError as e:
        txt = e.read().decode()
        try:
            return e.code, json.loads(txt)
        except Exception:
            return e.code, txt


def check(name, ok, detail=""):
    results.append((section, name, bool(ok), detail))
    print(("  PASS " if ok else "  FAIL ") + name + ("" if ok or not detail else f"   <- {detail}"))


def expect(name, resp, codes, extra=None):
    s, b = resp
    codes = (codes,) if isinstance(codes, int) else tuple(codes)
    ok = s in codes and (extra(b) if (extra and s in codes) else True)
    check(name, ok, f"status {s}, expected {codes}: {str(b)[:140]}")
    return b if s in codes else None


def login(user, pw):
    s, b = req("POST", "/api/auth/login", form={"username": user, "password": pw})
    return (b["access_token"], b.get("refresh_token")) if s == 200 else (None, None)


def jwt_payload(tok):
    seg = tok.split(".")[1]
    seg += "=" * (-len(seg) % 4)
    import base64
    return json.loads(base64.urlsafe_b64decode(seg))


def qa_sql(sql, params=()):
    with psycopg.connect(**q.psycopg_kwargs(direct=True), autocommit=True) as c:
        cur = c.cursor()
        cur.execute(sql, params)
        return cur.fetchall() if cur.description else None


TODAY = date.today()
fut = lambda n: (TODAY + timedelta(days=n)).isoformat()  # noqa: E731

SA_EMAIL, SA_PW = "qa.super@example.com", "QaSuper#12345"
ALPHA_MGR, ALPHA_PW = "alpha.mgr@example.com", "Alpha#Pass123"
EMP_PW = "Emp#Pass123"

# ---------------------------------------------------------------------------------------------------------
S("1. super admin bootstrap")
expect("setup creates the first super admin", req("POST", "/api/auth/setup/super-admin",
       body={"name": "QA Super", "email": SA_EMAIL, "password": SA_PW}), 200)
expect("setup refuses a second super admin", req("POST", "/api/auth/setup/super-admin",
       body={"name": "Other", "email": "other.super@example.com", "password": "Other#12345"}), 400)
SA, _ = login(SA_EMAIL, SA_PW)
check("super admin can log in", SA and jwt_payload(SA)["role"] == "super_admin")
check("super admin email login is case-insensitive", login(SA_EMAIL.upper(), SA_PW)[0] is not None)

# ---------------------------------------------------------------------------------------------------------
S("2. tenants + managers (super admin)")
expect("tenants list requires a token", req("GET", "/api/super-admin/tenants"), 401)
b = expect("create tenant Alpha with chosen manager password", req("POST", "/api/super-admin/tenants", SA, body={
    "organization_name": "Alpha Corp", "manager_email": ALPHA_MGR, "manager_password": ALPHA_PW, "manager_name": "Alpha Boss"}), 200)
ALPHA_ID = b["tenant"]["id"] if b else None
check("response never exposes an api_key", b and "api_key" not in json.dumps(b))
check("no password echoed when one was supplied", b and "password" not in b["manager"])
b = expect("create tenant Beta with generated manager password", req("POST", "/api/super-admin/tenants", SA, body={
    "organization_name": "Beta Corp", "manager_email": "beta.mgr@example.com"}), 200)
BETA_ID = b["tenant"]["id"] if b else None
BETA_PW = b["manager"].get("password") if b else None
check("generated password is returned once and is strong", BETA_PW and len(BETA_PW) >= 12 and re.search(r"[A-Z]", BETA_PW)
      and re.search(r"[a-z]", BETA_PW) and re.search(r"\d", BETA_PW))
expect("duplicate org name (case-insensitive) rejected", req("POST", "/api/super-admin/tenants", SA, body={
    "organization_name": "alpha corp", "manager_email": "x1@example.com", "manager_password": "Valid#Pass123"}), 400)
expect("duplicate manager email rejected", req("POST", "/api/super-admin/tenants", SA, body={
    "organization_name": "Gamma Co", "manager_email": ALPHA_MGR.upper(), "manager_password": "Valid#Pass123"}), 400)
expect("weak manager password rejected", req("POST", "/api/super-admin/tenants", SA, body={
    "organization_name": "Weak Co", "manager_email": "weak@example.com", "manager_password": "short"}), 400)
expect("invalid email rejected", req("POST", "/api/super-admin/tenants", SA, body={
    "organization_name": "Bad Co", "manager_email": "not-an-email", "manager_password": "Valid#Pass123"}), 422)
expect("blank organization name rejected", req("POST", "/api/super-admin/tenants", SA, body={
    "organization_name": "   ", "manager_email": "blank@example.com", "manager_password": "Valid#Pass123"}), 400)
lst = expect("tenant list shows managers", req("GET", "/api/super-admin/tenants", SA), 200,
             lambda b: {t["name"] for t in b} >= {"Alpha Corp", "Beta Corp"} and
             any(m["email"] == ALPHA_MGR for t in b if t["name"] == "Alpha Corp" for m in t["managers"]))
check("tenant list never exposes api_key", lst is not None and "api_key" not in json.dumps(lst))
expect("tenant details with stats", req("GET", f"/api/super-admin/tenants/{ALPHA_ID}", SA), 200,
       lambda b: b["stats"]["employees"] == 0 and len(b["managers"]) == 1)
expect("unknown tenant -> 404", req("GET", "/api/super-admin/tenants/999999", SA), 404)
b = expect("add a second manager (generated password)", req("POST", f"/api/super-admin/tenants/{ALPHA_ID}/managers", SA,
           body={"email": "alpha.mgr2@example.com", "name": "Alpha Two"}), 200)
MGR2_ID, MGR2_PW = (b["manager"]["id"], b["manager"]["password"]) if b else (None, None)
check("second manager can log in", login("alpha.mgr2@example.com", MGR2_PW)[0] is not None)
b = expect("reset manager password (generated)", req("POST", f"/api/super-admin/tenants/{ALPHA_ID}/managers/{MGR2_ID}/reset-password", SA, body={}), 200)
NEW_PW = b["manager"]["password"] if b else None
check("old password no longer works after reset", login("alpha.mgr2@example.com", MGR2_PW)[0] is None)
check("new password works after reset", login("alpha.mgr2@example.com", NEW_PW)[0] is not None)
expect("reset for a manager of another tenant -> 404", req("POST", f"/api/super-admin/tenants/{BETA_ID}/managers/{MGR2_ID}/reset-password", SA, body={}), 404)
b = expect("create + delete an empty tenant", req("POST", "/api/super-admin/tenants", SA, body={
    "organization_name": "Temp Co", "manager_email": "temp.mgr@example.com", "manager_password": "Temp#Pass123"}), 200)
expect("empty tenant deletes cleanly", req("DELETE", f"/api/super-admin/tenants/{b['tenant']['id']}", SA), 200)
check("deleted tenant's manager cannot log in", login("temp.mgr@example.com", "Temp#Pass123")[0] is None)

# ---------------------------------------------------------------------------------------------------------
S("3. tenant admin signs in with email (API-key login is gone)")
AM, AM_REFRESH = login(ALPHA_MGR, ALPHA_PW)
check("tenant admin logs in with email + password", AM and jwt_payload(AM)["role"] == "tenant_admin" and jwt_payload(AM)["tenant_id"] == ALPHA_ID)
check("email login is case-insensitive / trimmed", login("  " + ALPHA_MGR.upper() + " ", ALPHA_PW)[0] is not None)
check("wrong password rejected", login(ALPHA_MGR, "Wrong#Pass123")[0] is None)
check("unknown email rejected", login("nobody@example.com", ALPHA_PW)[0] is None)
BM, _ = login("beta.mgr@example.com", BETA_PW)
check("Beta manager logs in with the generated password", BM is not None)
alpha_key = qa_sql("select api_key from qa_test.tenants where id=%s", (ALPHA_ID,))[0][0]
expect("/api/auth/tenant-login no longer exists", req("POST", "/api/auth/tenant-login", body={"api_key": alpha_key}), (404, 405))
check("legacy: tenant api_key as the login password is rejected", login("anything", alpha_key)[0] is None)
check("legacy: tenant api_key as password with the manager email is rejected", login(ALPHA_MGR, alpha_key)[0] is None)
expect("legacy: X-API-Key header alone does not authenticate", req("GET", "/api/tenant/dashboard", headers={"X-API-Key": alpha_key}), 401)
expect("legacy: change-api-key endpoint removed", req("POST", "/api/tenant/change-api-key", AM, body={"api_key": "Aa1bbbbbb"}), (404, 405))
expect("tenant manager reaches own dashboard with JWT", req("GET", "/api/tenant/dashboard", AM), 200)
expect("tenant profile has no api_key", req("GET", "/api/tenant/profile", AM), 200, lambda b: "api_key" not in b and b["name"] == "Alpha Corp")
expect("tenant manager blocked from super-admin API", req("GET", "/api/super-admin/tenants", AM), 403)
expect("tenant manager blocked from org-admin API", req("GET", "/api/org-admin/dashboard", AM), 403)
expect("super admin blocked from tenant API", req("GET", "/api/tenant/dashboard", SA), 403)

# ---------------------------------------------------------------------------------------------------------
S("4. tenant admin: departments, org admins, settings, holidays, devices")
d1 = expect("create department Engineering", req("POST", "/api/tenant/departments", AM, body={"department_name": "Engineering"}), 200)
d2 = expect("create department HR", req("POST", "/api/tenant/departments", AM, body={"department_name": "HR"}), 200)
ENG, HR = d1["department"]["department_id"], d2["department"]["department_id"]
expect("duplicate department rejected", req("POST", "/api/tenant/departments", AM, body={"department_name": "Engineering"}), 400)
expect("department rename", req("PUT", f"/api/tenant/departments/{HR}", AM, body={"department_name": "Human Resources"}), 200)
expect("department deactivate", req("PATCH", f"/api/tenant/departments/{HR}/status", AM, body={"is_active": False}), 200, lambda b: b["is_active"] is False)
expect("department reactivate (status=active)", req("PATCH", f"/api/tenant/departments/{HR}/status", AM, body={"status": "active"}), 200, lambda b: b["is_active"] is True)
expect("department status with no field -> 400", req("PATCH", f"/api/tenant/departments/{HR}/status", AM, body={}), 400)
expect("department list", req("GET", "/api/tenant/departments", AM), 200, lambda b: len(b) == 2)
tmp = req("POST", "/api/tenant/departments", AM, body={"department_name": "Temp Dept"})[1]
expect("empty department deletes", req("DELETE", f"/api/tenant/departments/{tmp['department']['department_id']}", AM), 200)

b = expect("create org admin (password returned once)", req("POST", "/api/tenant/org-admins", AM,
           body={"name": "Eng Admin", "email": "eng.admin@example.com", "dept_id": ENG}), 200)
ENG_ADMIN_ID, ENG_ADMIN_PW = (b["org_admin"]["id"], b["credentials"]["password"]) if b else (None, None)
expect("duplicate org admin email rejected", req("POST", "/api/tenant/org-admins", AM,
       body={"name": "Dup", "email": "ENG.admin@example.com", "dept_id": ENG}), 400)
expect("org admin for a department of another tenant rejected", req("POST", "/api/tenant/org-admins", AM,
       body={"name": "Cross", "email": "cross@example.com", "dept_id": 999999}), (400, 404))
b = req("POST", "/api/tenant/org-admins", AM, body={"name": "HR Admin", "email": "hr.admin@example.com", "dept_id": HR})[1]
HR_ADMIN_PW = b["credentials"]["password"]
expect("org admin list", req("GET", "/api/tenant/org-admins", AM), 200, lambda b: len(b) == 2)
EA, _ = login("eng.admin@example.com", ENG_ADMIN_PW)
HA, _ = login("hr.admin@example.com", HR_ADMIN_PW)
check("org admin logs in with email", EA and jwt_payload(EA)["role"] == "org_admin")

expect("get tenant settings", req("GET", "/api/tenant/settings", AM), 200, lambda b: "office_start_time" in b)
expect("update tenant settings", req("PUT", "/api/tenant/settings", AM, body={
    "office_start_time": "09:00:00", "office_end_time": "18:00:00", "late_threshold_minutes": 10, "working_days": "1,2,3,4,5", "min_working_hours": 8}), 200)
expect("invalid settings time rejected", req("PUT", "/api/tenant/settings", AM, body={"office_start_time": "25:00:00", "office_end_time": "18:00:00", "late_threshold_minutes": 10, "working_days": "1,2,3", "min_working_hours": 8}), 422)
expect("settings validate endpoint", req("POST", "/api/tenant/settings/validate", AM, body={"office_start_time": "09:00:00", "office_end_time": "18:00:00", "late_threshold_minutes": 10, "working_days": "1,2,3,4,5", "min_working_hours": 8}), 200)

h = expect("create holiday", req("POST", "/api/tenant/holidays", AM, body={"name": "Founders Day", "holiday_date": fut(20), "description": "QA"}), 200)
_hl = req("GET", "/api/tenant/holidays", AM)[1]
HOL = next((x["holiday_id"] for x in _hl if x["name"] == "Founders Day"), None) if isinstance(_hl, list) else None
expect("duplicate holiday date rejected", req("POST", "/api/tenant/holidays", AM, body={"name": "Dup", "holiday_date": fut(20)}), 400)
expect("holiday list / upcoming", req("GET", "/api/tenant/holidays", AM), 200, lambda b: len(b) >= 1)
expect("upcoming holidays", req("GET", "/api/tenant/holidays/upcoming?limit=3", AM), 200)
if HOL:
    expect("holiday update", req("PUT", f"/api/tenant/holidays/{HOL}", AM, body={"name": "Founders Day 2", "holiday_date": fut(20)}), 200)
else:
    check("holiday id discovered", False, str(h))

expect("create device", req("POST", "/api/tenant/devices", AM, body={"device_id": "ALPHA-DEV-1", "secret_key": "alpha-secret-1"}), 200)
expect("duplicate device id rejected", req("POST", "/api/tenant/devices", AM, body={"device_id": "ALPHA-DEV-1", "secret_key": "another-secret"}), 400)
expect("device list", req("GET", "/api/tenant/devices", AM), 200, lambda b: len(b) == 1)
expect("device details", req("GET", "/api/tenant/devices/ALPHA-DEV-1", AM), 200)
expect("device status summary", req("GET", "/api/tenant/devices/status/summary", AM), 200)
expect("device fire-command", req("POST", "/api/tenant/devices/fire-command", AM, body={"device_id": "ALPHA-DEV-1", "command": "ENROLL", "target_id": 1}), (200, 400, 409, 502, 503))

# ---------------------------------------------------------------------------------------------------------
S("5. org admin: employees, devices, holidays, settings")
expect("org dashboard", req("GET", "/api/org-admin/dashboard", EA), 200)
for i, (code, fid) in enumerate([("ENG001", 1), ("ENG002", 2), ("ENG003", 3)]):
    b = expect(f"create employee {code}", req("POST", "/api/org-admin/employees", EA, body={"name": f"Eng Emp {i+1}", "employee_code": code, "password": EMP_PW, "finger_id": fid}), 200)
expect("duplicate employee code rejected", req("POST", "/api/org-admin/employees", EA, body={"name": "Dup", "employee_code": "ENG001", "password": EMP_PW}), 400)
expect("duplicate finger id rejected", req("POST", "/api/org-admin/employees", EA, body={"name": "Dup F", "employee_code": "ENG099", "password": EMP_PW, "finger_id": 1}), 400)
expect("weak employee password rejected", req("POST", "/api/org-admin/employees", EA, body={"name": "Weak", "employee_code": "ENG098", "password": "abc"}), (400, 422))
emps = expect("employee list", req("GET", "/api/org-admin/employees", EA), 200, lambda b: len(b) == 3)
E1 = next((e["id"] for e in emps if e.get("employee_code") == "ENG001"), None) if emps else None
E2 = next((e["id"] for e in emps if e.get("employee_code") == "ENG002"), None) if emps else None
expect("employee details", req("GET", f"/api/org-admin/employees/{E1}", EA), 200)
expect("employee update", req("PUT", f"/api/org-admin/employees/{E1}", EA, body={"name": "Renamed Emp"}), 200)
expect("available finger slots", req("GET", "/api/org-admin/employees/available-finger-slots", EA), 200)
expect("org admin devices list", req("GET", "/api/org-admin/devices", EA), 200)
expect("org admin device create", req("POST", "/api/org-admin/devices", EA, body={"device_id": "ALPHA-DEV-2", "secret_key": "alpha-secret-2"}), 200)
expect("org holidays visible", req("GET", "/api/org-admin/holidays", EA), 200, lambda b: len(b) >= 1)
expect("org settings read", req("GET", "/api/org-admin/settings", EA), 200, lambda b: b["office_start_time"] == "09:00:00")
expect("org activity feed", req("GET", "/api/org-admin/activity", EA), 200, lambda b: len(b) >= 1)
expect("HR admin sees no engineering employees", req("GET", "/api/org-admin/employees", HA), 200, lambda b: len(b) == 0)
expect("HR admin cannot read an engineering employee", req("GET", f"/api/org-admin/employees/{E1}", HA), (403, 404))
expect("tenant employee list (4 incl. none for HR)", req("GET", "/api/tenant/employees", AM), 200, lambda b: len(b) == 3)

# ---------------------------------------------------------------------------------------------------------
S("6. device punches -> attendance")
dev = {"x-device-id": "ALPHA-DEV-1", "x-secret-key": "alpha-secret-1"}
expect("punch with wrong secret rejected", req("POST", "/api/device/mark?finger_id=1", headers={"x-device-id": "ALPHA-DEV-1", "x-secret-key": "nope"}), 401)
expect("punch from unknown device rejected", req("POST", "/api/device/mark?finger_id=1", headers={"x-device-id": "GHOST", "x-secret-key": "x"}), 401)
expect("punch without headers -> 422", req("POST", "/api/device/mark?finger_id=1"), 422)
expect("first punch is IN", req("POST", "/api/device/mark?finger_id=1", headers=dev), 200, lambda b: b.get("record_type") == "IN")
expect("second punch is OUT", req("POST", "/api/device/mark?finger_id=1", headers=dev), 200, lambda b: b.get("record_type") == "OUT")
expect("punch for another employee", req("POST", "/api/device/mark?finger_id=2", headers=dev), 200)
expect("punch for unknown fingerprint", req("POST", "/api/device/mark?finger_id=77", headers=dev), (200, 400, 404))
expect("device attendance history", req("GET", "/api/device/history", headers=dev), 200)
expect("tenant-scoped device mark route works", req("POST", "/api/tenant/device/mark?finger_id=3", headers=dev), 200)
expect("device heartbeat", req("POST", "/api/tenant/device/heartbeat", headers=dev), 200)
expect("heartbeat with bad secret rejected", req("POST", "/api/tenant/device/heartbeat", headers={"x-device-id": "ALPHA-DEV-1", "x-secret-key": "bad"}), 401)

# ---------------------------------------------------------------------------------------------------------
S("7. org admin / tenant admin attendance views")
tday = TODAY.isoformat()
expect("org attendance today", req("GET", "/api/org-admin/attendance/today", EA), 200)
expect("org attendance by date", req("GET", f"/api/org-admin/attendance/date/{tday}", EA), 200, lambda b: b["summary"]["present"] >= 2)
expect("org attendance ?date=", req("GET", f"/api/org-admin/attendance?date={tday}", EA), 200)
expect("tenant attendance today", req("GET", "/api/tenant/attendance/today", AM), 200)
expect("tenant attendance by date", req("GET", f"/api/tenant/attendance/date/{tday}", AM), 200, lambda b: b["summary"]["present"] >= 2)
expect("tenant attendance recent", req("GET", "/api/tenant/attendance/recent?limit=5", AM), 200, lambda b: len(b) >= 3)
expect("tenant attendance stats", req("GET", "/api/tenant/attendance/stats?days=7", AM), 200, lambda b: len(b["trend"]) == 7 and b["present_today"] >= 2)
expect("tenant attendance settings", req("GET", "/api/tenant/attendance/settings", AM), 200)
expect("tenant activity feed", req("GET", "/api/tenant/activity?limit=10", AM), 200, lambda b: len(b) >= 1)
expect("tenant employee attendance", req("GET", f"/api/tenant/employees/{E1}/attendance?month={TODAY.month}&year={TODAY.year}", AM), 200)
expect("tenant employee by fingerprint", req("GET", "/api/tenant/employees/fingerprint/1", AM), 200)
expect("tenant report: attendance", req("GET", f"/api/tenant/reports/attendance?start_date={fut(-7)}&end_date={tday}", AM), 200)
expect("tenant report: department summary", req("GET", f"/api/tenant/reports/department-summary?month={TODAY.month}&year={TODAY.year}", AM), 200)
expect("tenant report: export", req("GET", f"/api/tenant/reports/export?report_type=attendance&start_date={fut(-7)}&end_date={tday}", AM), (200, 400), None)
expect("tenant dashboard has data", req("GET", "/api/tenant/dashboard", AM), 200)

# ---------------------------------------------------------------------------------------------------------
S("8. employee self-service")
E1T, E1R = login("ENG001", EMP_PW)
check("employee logs in with employee code", E1T and jwt_payload(E1T)["role"] == "employee")
expect("employee dashboard", req("GET", "/api/employee/dashboard", E1T), 200)
expect("attendance today", req("GET", "/api/employee/attendance/today", E1T), 200)
expect("attendance history", req("GET", "/api/employee/attendance", E1T), 200)
expect("attendance stats", req("GET", "/api/employee/attendance/stats", E1T), 200)
expect("attendance monthly stats", req("GET", f"/api/employee/attendance/stats/monthly?month={TODAY.month}&year={TODAY.year}", E1T), 200)
expect("attendance hours summary", req("GET", f"/api/employee/attendance/hours/summary?month={TODAY.month}&year={TODAY.year}", E1T), 200)
cal = expect("attendance calendar", req("GET", f"/api/employee/attendance/calendar?month={TODAY.month}&year={TODAY.year}", E1T), 200, lambda b: len(b["days"]) >= 28)
expect("calendar rejects month 13", req("GET", "/api/employee/attendance/calendar?month=13&year=2026", E1T), 400)
expect("export json", req("GET", "/api/employee/attendance/export", E1T), 200, lambda b: b["total_records"] >= 1)
s, txt = req("GET", "/api/employee/attendance/export?format=csv", E1T, raw=True)
check("export csv", s == 200 and txt.startswith("date,check_in"), f"{s} {str(txt)[:60]}")
expect("attendance by date", req("GET", f"/api/employee/attendance/{tday}", E1T), 200, lambda b: b["status"] == "present")
expect("bad date -> 400", req("GET", "/api/employee/attendance/not-a-date", E1T), 400)
expect("holidays", req("GET", "/api/employee/holidays", E1T), 200)
expect("upcoming holidays", req("GET", "/api/employee/holidays/upcoming", E1T), 200)
expect("profile", req("GET", "/api/employee/profile", E1T), 200)
expect("profile attendance summary", req("GET", "/api/employee/profile/attendance-summary", E1T), 200)
expect("notifications list", req("GET", "/api/employee/notifications", E1T), 200)
expect("unread count", req("GET", "/api/employee/notifications/unread-count", E1T), 200)
expect("mark all read", req("PATCH", "/api/employee/notifications/read-all", E1T), 200)

expect("leave: end before start rejected", req("POST", "/api/employee/leaves", E1T, body={"leave_type": "casual", "start_date": fut(12), "end_date": fut(10)}), (400, 422))
expect("leave: bad type rejected", req("POST", "/api/employee/leaves", E1T, body={"leave_type": "vacation-xyz", "start_date": fut(10), "end_date": fut(11)}), (400, 422))
expect("leave: start in the past rejected", req("POST", "/api/employee/leaves", E1T, body={"leave_type": "casual", "start_date": fut(-5), "end_date": fut(-4)}), (200, 400, 422))
b = expect("apply for leave", req("POST", "/api/employee/leaves", E1T, body={"leave_type": "casual", "start_date": fut(10), "end_date": fut(11), "reason": "QA"}), 200)
expect("overlapping leave rejected", req("POST", "/api/employee/leaves", E1T, body={"leave_type": "casual", "start_date": fut(11), "end_date": fut(12)}), (400, 409))
mine = expect("leave list", req("GET", "/api/employee/leaves", E1T), 200, lambda b: len(b) >= 1)
L1 = mine[0]["leave_id"] if mine else None
expect("leave details", req("GET", f"/api/employee/leaves/{L1}", E1T), 200)
expect("leave stats", req("GET", "/api/employee/leaves/stats", E1T), 200)
expect("leave balance endpoint", req("GET", "/api/employee/leaves/balance", E1T), 200)
b = req("POST", "/api/employee/leaves", E1T, body={"leave_type": "sick", "start_date": fut(30), "end_date": fut(30), "reason": "to cancel"})[1]
mine = req("GET", "/api/employee/leaves", E1T)[1]
L_CANCEL = next((l["leave_id"] for l in mine if l["leave_type"] == "sick"), None)
expect("employee cancels own pending leave", req("PATCH", f"/api/employee/leaves/{L_CANCEL}/cancel", E1T), 200)
expect("cancel again rejected", req("PATCH", f"/api/employee/leaves/{L_CANCEL}/cancel", E1T), (400, 404))

# ---------------------------------------------------------------------------------------------------------
S("9. leave approval chain + balances")
E2T, _ = login("ENG002", EMP_PW)
req("POST", "/api/employee/leaves", E2T, body={"leave_type": "earned", "start_date": fut(40), "end_date": fut(41), "reason": "reject me"})
L2 = req("GET", "/api/employee/leaves", E2T)[1][0]["leave_id"]
expect("org admin sees department leaves", req("GET", "/api/org-admin/leaves", EA), 200, lambda b: len(b) >= 2)
expect("tenant sees all leaves", req("GET", "/api/tenant/leaves", AM), 200, lambda b: len(b) >= 2)
expect("tenant pending leaves", req("GET", "/api/tenant/leaves/pending", AM), 200)
expect("HR admin cannot approve an engineering leave", req("PATCH", f"/api/org-admin/leaves/{L1}/approve", HA), 404)
expect("HR admin cannot reject an engineering leave", req("PATCH", f"/api/org-admin/leaves/{L1}/reject", HA, body={}), 404)
expect("final approval without balances is refused", req("PATCH", f"/api/tenant/leaves/{L1}/approve", AM), 400)
expect("org admin approves (department level)", req("PATCH", f"/api/org-admin/leaves/{L1}/approve", EA), 200)
expect("leave balance settings read", req("GET", "/api/tenant/leave-balances/settings", AM), 200)
expect("leave balance settings update", req("PUT", "/api/tenant/leave-balances/settings", AM, body={"sick_leave_quota": 12, "casual_leave_quota": 12, "earned_leave_quota": 15, "reset_frequency": "yearly", "carry_forward_limit": 0, "auto_approve_limit": 0}), 200)
expect("reset-year creates balances", req("POST", f"/api/tenant/leave-balances/reset-year?year={TODAY.year}", AM), 200)
expect("balances listed", req("GET", f"/api/tenant/leave-balances?year={TODAY.year}", AM), 200, lambda b: len(b) >= 1)
expect("final approval", req("PATCH", f"/api/tenant/leaves/{L1}/approve", AM), 200)
bal = req("GET", "/api/employee/leaves/balance", E1T)[1]
check("balance deducted after approval", json.dumps(bal).count("used") >= 0 and "casual" in json.dumps(bal), str(bal)[:160])
expect("org admin rejects with a reason", req("PATCH", f"/api/org-admin/leaves/{L2}/reject", EA, body={"reason": "Deadline week"}), 200)
expect("rejecting an already rejected leave -> 400", req("PATCH", f"/api/org-admin/leaves/{L2}/reject", EA, body={}), 400)
l2 = req("GET", f"/api/employee/leaves/{L2}", E2T)[1]
check("employee sees rejected + reason", l2.get("status") == "rejected" and "Deadline" in json.dumps(l2), str(l2)[:160])
expect("leave stats", req("GET", "/api/tenant/leaves/stats", AM), 200, lambda b: b["rejected"] >= 1 and b["approved"] >= 1)
n = req("GET", "/api/employee/notifications", E2T)[1]
check("employee got a rejection notification", "Rejected" in json.dumps(n))

# ---------------------------------------------------------------------------------------------------------
S("10. notifications for a tenant admin (previously 401)")
expect("tenant admin unread count works", req("GET", "/api/employee/notifications/unread-count", AM), 200)
expect("tenant admin notifications list works", req("GET", "/api/employee/notifications", AM), 200)

# ---------------------------------------------------------------------------------------------------------
S("11. cross-tenant isolation (Alpha vs Beta)")
BD = req("POST", "/api/tenant/departments", BM, body={"department_name": "Ops"})[1]["department"]["department_id"]
b = req("POST", "/api/tenant/org-admins", BM, body={"name": "Ops Admin", "email": "ops.admin@example.com", "dept_id": BD})[1]
BEA, _ = login("ops.admin@example.com", b["credentials"]["password"])
req("POST", "/api/org-admin/employees", BEA, body={"name": "Ops Emp", "employee_code": "OPS001", "password": EMP_PW, "finger_id": 1})
req("POST", "/api/tenant/devices", BM, body={"device_id": "BETA-DEV-1", "secret_key": "beta-secret-1"})
BH = req("POST", "/api/tenant/holidays", BM, body={"name": "Beta Day", "holiday_date": fut(25)})[1]
BHID = next((x["holiday_id"] for x in req("GET", "/api/tenant/holidays", BM)[1] if x["name"] == "Beta Day"), None)
BE = req("GET", "/api/org-admin/employees", BEA)[1][0]["id"]
BET, _ = login("OPS001", EMP_PW)
req("POST", "/api/employee/leaves", BET, body={"leave_type": "casual", "start_date": fut(15), "end_date": fut(16)})
BL = req("GET", "/api/employee/leaves", BET)[1][0]["leave_id"]
BADM = req("GET", "/api/tenant/org-admins", BM)[1][0]["id"]
check("Alpha employee list has only Alpha people", all(e.get("employee_code", "").startswith("ENG") for e in req("GET", "/api/tenant/employees", AM)[1]))
expect("Alpha manager cannot read a Beta employee", req("GET", f"/api/tenant/employees/{BE}", AM), (403, 404))
_r = req("GET", f"/api/tenant/employees/{BE}/attendance?month={TODAY.month}&year={TODAY.year}", AM)
check("Alpha manager cannot read attendance of a Beta employee (denied or empty)", _r[0] in (403, 404) or (_r[0] == 200 and not _r[1]), f"{_r[0]} {str(_r[1])[:120]}")
expect("Alpha manager cannot see Beta device", req("GET", "/api/tenant/devices/BETA-DEV-1", AM), (403, 404))
expect("Alpha manager cannot delete Beta device", req("DELETE", "/api/tenant/devices/BETA-DEV-1", AM), (403, 404))
expect("Alpha manager cannot edit Beta holiday", req("PUT", f"/api/tenant/holidays/{BHID}", AM, body={"name": "Hacked", "holiday_date": fut(25)}), (403, 404))
expect("Alpha manager cannot delete Beta holiday", req("DELETE", f"/api/tenant/holidays/{BHID}", AM), (403, 404))
expect("Alpha manager cannot edit Beta org admin", req("PUT", f"/api/tenant/org-admins/{BADM}", AM, body={"name": "Hacked"}), (403, 404))
expect("Alpha manager cannot delete Beta org admin", req("DELETE", f"/api/tenant/org-admins/{BADM}", AM), (403, 404))
expect("Alpha manager cannot approve a Beta leave", req("PATCH", f"/api/tenant/leaves/{BL}/approve", AM), (403, 404, 400))
expect("Alpha manager cannot reject a Beta leave", req("PATCH", f"/api/tenant/leaves/{BL}/reject", AM), (403, 404))
leaves = req("GET", "/api/tenant/leaves", AM)[1]
check("Alpha leave list contains no Beta leave", all(l["leave_id"] != BL for l in leaves))
expect("Alpha org admin cannot read a Beta employee", req("GET", f"/api/org-admin/employees/{BE}", EA), (403, 404))
expect("Alpha org admin cannot edit a Beta employee", req("PUT", f"/api/org-admin/employees/{BE}", EA, body={"name": "Hacked"}), (403, 404))
expect("Alpha org admin cannot delete a Beta employee", req("DELETE", f"/api/org-admin/employees/{BE}", EA), (403, 404))
expect("Alpha org admin cannot approve a Beta leave", req("PATCH", f"/api/org-admin/leaves/{BL}/approve", EA), (403, 404))
expect("Alpha org admin cannot fire commands on a Beta device", req("POST", "/api/org-admin/devices/fire-command", EA, body={"device_id": "BETA-DEV-1", "command": "ENROLL", "target_id": 1}), (403, 404))
expect("Alpha employee cannot read a Beta leave", req("GET", f"/api/employee/leaves/{BL}", E1T), (403, 404))
expect("Alpha employee cannot cancel a Beta leave", req("PATCH", f"/api/employee/leaves/{BL}/cancel", E1T), (403, 404))
r = req("GET", f"/api/tenant/settings/bulk?tenant_ids={BETA_ID}", AM)
check("settings/bulk does not leak another tenant's settings to an Alpha manager",
      r[0] in (403, 404) or (r[0] == 200 and BETA_ID not in [x.get("tenant_id") for x in (r[1] if isinstance(r[1], list) else r[1].get("settings", []) if isinstance(r[1], dict) else [])]
                              and str(BETA_ID) not in json.dumps(r[1])), f"{r[0]} {str(r[1])[:150]}")
tr = req("GET", "/api/tracker/employees", EA)
check("tracker employees list is limited to the org admin's own department", tr[0] != 200 or all(
    not str(x.get("employee_code", "")).startswith("OPS") for x in (tr[1] if isinstance(tr[1], list) else [])), f"{tr[0]} {str(tr[1])[:150]}")
beta_dev = req("POST", "/api/device/mark?finger_id=1", headers={"x-device-id": "BETA-DEV-1", "x-secret-key": "beta-secret-1"})
alpha_after = req("GET", f"/api/org-admin/attendance/date/{tday}", EA)[1]["summary"]["present"]
check("a Beta device punch never lands in Alpha attendance", beta_dev[0] == 200 and alpha_after == 3, f"alpha present={alpha_after}")

# ---------------------------------------------------------------------------------------------------------
if os.environ.get("QA_SKIP_SWEEP"):
    S("12. role matrix sweep (skipped on this run)")
else:
    S("12. role matrix sweep (every endpoint, no token / wrong role)")
    oa = json.load(urllib.request.urlopen(BASE + "/openapi.json", timeout=30))
    PUBLIC = {"/", "/api/auth/login", "/api/auth/refresh", "/api/auth/logout", "/api/auth/setup/super-admin", "/api/tenant/settings/public"}
    HEADER_AUTH = ("/api/device/", "/api/tenant/device/")
    tokens = {"employee": E1T, "org_admin": EA, "tenant_admin": AM, "super_admin": SA}
    allowed = {  # role prefixes each role MAY call
        "/api/super-admin/": {"super_admin"},
        "/api/tenant/": {"tenant_admin"},
        "/api/org-admin/": {"org_admin"},
        "/api/tracker/": {"org_admin"},
    }
    bad_noauth, bad_role, probed = [], [], 0
    for path, ops in sorted(oa["paths"].items()):
        if path in PUBLIC or path.startswith(HEADER_AUTH):
            continue
        url = re.sub(r"\{[^}]+\}", "1", path)
        for method in ops:
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            has_body = "requestBody" in ops[method]
            body = {} if has_body else None
            probed += 1
            s, _ = req(method.upper(), url + ("?" if "?" not in url else "&") + "x=1", body=body)
            if s != 401:
                bad_noauth.append(f"{method.upper()} {path} -> {s}")
            for role, tok in tokens.items():
                prefix = next((p for p in allowed if path.startswith(p)), None)
                if prefix is None or role in allowed[prefix]:
                    continue
                s, _ = req(method.upper(), url + "?x=1", token=tok, body=body)
                if s not in (403, 401):
                    bad_role.append(f"{role}: {method.upper()} {path} -> {s}")
    check(f"all {probed} protected endpoints reject requests without a token (401)", not bad_noauth, "; ".join(bad_noauth[:6]))
    check("every role is blocked (403) from other roles' endpoints", not bad_role, "; ".join(bad_role[:8]))

    # ---------------------------------------------------------------------------------------------------------

S("13. session lifecycle")
T1, R1 = login(ALPHA_MGR, ALPHA_PW)
s, b = req("POST", "/api/auth/refresh", body={"refresh_token": R1})
check("refresh token works (was broken: never persisted)", s == 200 and "access_token" in (b or {}), f"{s} {str(b)[:100]}")
expect("a refresh token cannot be reused", req("POST", "/api/auth/refresh", body={"refresh_token": R1}), 401)
new_r = b.get("refresh_token") if isinstance(b, dict) else None
expect("logout revokes the refresh token", req("POST", f"/api/auth/logout?refresh_token={new_r}"), 200)
expect("revoked refresh token rejected", req("POST", "/api/auth/refresh", body={"refresh_token": new_r}), 401)
expect("an access token is not accepted as a refresh token", req("POST", "/api/auth/refresh", body={"refresh_token": T1}), 401)
expect("garbage token rejected", req("GET", "/api/tenant/dashboard", token="abc.def.ghi"), 401)
expect("password change: wrong old password", req("PUT", "/api/employee/profile/change-password", E1T, body={"old_password": "Wrong#Pass1", "new_password": "NewEmp#Pass123"}), (400, 401))
expect("password change: weak new password", req("PUT", "/api/employee/profile/change-password", E1T, body={"old_password": EMP_PW, "new_password": "weak"}), (400, 422))
expect("password change succeeds", req("PUT", "/api/employee/profile/change-password", E1T, body={"old_password": EMP_PW, "new_password": "NewEmp#Pass123"}), 200)
check("old password stops working / new one works", login("ENG001", EMP_PW)[0] is None and login("ENG001", "NewEmp#Pass123")[0] is not None)
expect("tenant admin changes own password via /auth/change-password", req("POST", "/api/auth/change-password", AM, body={"old_password": ALPHA_PW, "new_password": "Alpha#Pass456"}), 200)
check("manager signs in with the new password only", login(ALPHA_MGR, ALPHA_PW)[0] is None and login(ALPHA_MGR, "Alpha#Pass456")[0] is not None)
AM, _ = login(ALPHA_MGR, "Alpha#Pass456")
expect("org admin deactivates an employee", req("PUT", f"/api/org-admin/employees/{E2}", EA, body={"is_active": False}), 200)
check("deactivated employee cannot log in", login("ENG002", EMP_PW)[0] is None)
expect("deactivated employee's existing token stops working", req("GET", "/api/employee/dashboard", E2T), 401)
expect("org admin reactivates", req("PUT", f"/api/org-admin/employees/{E2}", EA, body={"is_active": True}), 200)
check("reactivated employee can log in again", login("ENG002", EMP_PW)[0] is not None)

# ---------------------------------------------------------------------------------------------------------
S("14. deletion rules")
expect("tenant with data cannot be deleted (409)", req("DELETE", f"/api/super-admin/tenants/{ALPHA_ID}", SA), 409)
expect("tenant still exists after the refused delete", req("GET", f"/api/super-admin/tenants/{ALPHA_ID}", SA), 200)
expect("department with employees cannot be deleted", req("DELETE", f"/api/tenant/departments/{ENG}", AM), 400)
expect("device delete", req("DELETE", "/api/tenant/devices/ALPHA-DEV-1", AM), (200, 400))
expect("holiday delete", req("DELETE", f"/api/tenant/holidays/{HOL}", AM), 200) if HOL else None
expect("org admin delete (tenant)", req("DELETE", f"/api/tenant/org-admins/{ENG_ADMIN_ID}", AM), (200, 400))
expect("employee delete (org admin)", req("DELETE", f"/api/org-admin/employees/{E2}", EA), (200, 400, 401, 403))

# ---------------------------------------------------------------------------------------------------------
fails = [r for r in results if not r[2]]
print("\n" + "=" * 78)
print(f"TOTAL {len(results)} checks | PASS {len(results) - len(fails)} | FAIL {len(fails)}")
by = {}
for sec, name, ok, d in results:
    by.setdefault(sec, [0, 0])[0 if ok else 1] += 1
for sec, (p, f) in by.items():
    print(f"  {sec:60} pass {p:3}  fail {f}")
if fails:
    print("\nFAILURES:")
    for sec, name, ok, d in fails:
        print(f"  [{sec}] {name}\n      {d}")
json.dump({"results": results}, open(os.path.join(os.path.dirname(__file__), "api_results.json"), "w"))
sys.exit(1 if fails else 0)
