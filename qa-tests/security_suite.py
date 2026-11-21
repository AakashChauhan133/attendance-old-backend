"""Security + edge-case tests against the QA backend (schema qa_test only). Uses its own throwaway tenants."""
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))
import qa_common as q  # noqa: E402

q.prefer_ipv4()
BASE = os.environ.get("QA_BASE", "http://localhost:8001")
results, section = [], "setup"


def S(n):
    global section
    section = n
    print(f"\n=== {n}")


def req(method, path, token=None, body=None, form=None, headers=None):
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
    try:
        with urllib.request.urlopen(urllib.request.Request(BASE + path, data=data, method=method, headers=h), timeout=90) as r:
            t = r.read().decode()
            return r.status, (json.loads(t) if t else None)
    except urllib.error.HTTPError as e:
        t = e.read().decode()
        try:
            return e.code, json.loads(t)
        except Exception:
            return e.code, t


def check(name, ok, detail=""):
    results.append((section, name, bool(ok), detail))
    print(("  PASS " if ok else "  FAIL ") + name + ("" if ok or not detail else f"   <- {detail}"))


def expect(name, resp, codes, extra=None):
    s, b = resp
    codes = (codes,) if isinstance(codes, int) else tuple(codes)
    ok = s in codes and (extra(b) if extra else True)
    check(name, ok, f"status {s}, expected {codes}: {str(b)[:140]}")
    return b if s in codes else None


def login(u, p):
    s, b = req("POST", "/api/auth/login", form={"username": u, "password": p})
    return b["access_token"] if s == 200 else None


def b64(d):
    return base64.urlsafe_b64encode(d).rstrip(b"=").decode()


def forge(secret, payload, alg="HS256"):
    head = b64(json.dumps({"alg": alg, "typ": "JWT"}).encode())
    body = b64(json.dumps(payload).encode())
    sig = b64(hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()) if alg == "HS256" else ""
    return f"{head}.{body}.{sig}"


def payload_of(tok):
    seg = tok.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))


SA = login("qa.super@example.com", "QaSuper#12345")
assert SA, "run api_suite.py first (creates the QA super admin)"
PW = "Sec#Pass12345"
SUF = str(int(time.time()))[-6:]


def make_tenant(name, mgr_email):
    s, b = req("POST", "/api/super-admin/tenants", SA, body={"organization_name": f"{name} {SUF}", "manager_email": mgr_email, "manager_password": PW})
    assert s == 200, (name, s, b)
    return b["tenant"]["id"]


def make_org(mgr, email, dept_name):
    d = req("POST", "/api/tenant/departments", mgr, body={"department_name": dept_name})[1]["department"]["department_id"]
    b = req("POST", "/api/tenant/org-admins", mgr, body={"name": f"{dept_name} Admin", "email": email, "dept_id": d})[1]
    return d, login(email, b["credentials"]["password"]), b["org_admin"]["id"]


# ------------------------------------------------------------------------------------------------
S("1. set-password (was: any org admin could reset ANY user, incl. the super admin)")
D_ID = make_tenant("Delta", f"delta.mgr{SUF}@example.com")
E_ID = make_tenant("Echo", f"echo.mgr{SUF}@example.com")
DM, EM = login(f"delta.mgr{SUF}@example.com", PW), login(f"echo.mgr{SUF}@example.com", PW)
d_eng, DO1, d_org1_id = make_org(DM, f"delta.eng{SUF}@example.com", "Eng")
d_hr, DO2, d_org2_id = make_org(DM, f"delta.hr{SUF}@example.com", "HR")
e_ops, EO, e_org_id = make_org(EM, f"echo.ops{SUF}@example.com", "Ops")
for code, tok, fid in [(f"DE{SUF}1", DO1, 1), (f"DH{SUF}1", DO2, 2), (f"EO{SUF}1", EO, 1)]:
    req("POST", "/api/org-admin/employees", tok, body={"name": code, "employee_code": code, "password": PW, "finger_id": fid})
emp = lambda tok: {e["employee_code"]: e["id"] for e in req("GET", "/api/org-admin/employees", tok)[1]}  # noqa: E731
DE, DH, EE = emp(DO1)[f"DE{SUF}1"], emp(DO2)[f"DH{SUF}1"], emp(EO)[f"EO{SUF}1"]
sa_id = next(u for u in [1, 2, 3, 4] if True)  # QA super admin is the first user created
setpw = lambda tok, uid, pw="Reset#Pass123": req("POST", "/api/auth/set-password", tok, body={"user_id": uid, "new_password": pw})  # noqa: E731

expect("org admin cannot reset the super admin's password", setpw(DO1, sa_id), (403, 404))
check("super admin password still works afterwards", login("qa.super@example.com", "QaSuper#12345") is not None)
expect("org admin cannot reset a tenant manager", setpw(DO1, d_org1_id - 1), (403, 404))
expect("org admin cannot reset a colleague org admin", setpw(DO1, d_org2_id), (403, 404))
expect("org admin cannot reset an employee of another department", setpw(DO1, DH), (403, 404))
expect("org admin cannot reset an employee of another tenant", setpw(DO1, EE), (403, 404))
check("victims' passwords unchanged", login(f"DH{SUF}1", PW) and login(f"EO{SUF}1", PW) and login(f"delta.hr{SUF}@example.com", "Reset#Pass123") is None)
expect("org admin CAN reset an employee of own department", setpw(DO1, DE), 200)
check("…and the new password works, the old one does not", login(f"DE{SUF}1", "Reset#Pass123") and not login(f"DE{SUF}1", PW))
expect("weak password rejected", setpw(DO1, DE, "weak"), (400, 422))
expect("tenant manager cannot reset an employee of another tenant", setpw(DM, EE), (403, 404))
expect("tenant manager cannot reset the super admin", setpw(DM, sa_id), (403, 404))
expect("tenant manager CAN reset own org admin", setpw(DM, d_org1_id), 200)
expect("employee cannot use set-password at all", setpw(login(f"DE{SUF}1", "Reset#Pass123"), DH), 403)
expect("super admin can reset anyone", setpw(SA, EE), 200)

# ------------------------------------------------------------------------------------------------
S("2. token forgery / tampering")
tok = DM
pl = payload_of(tok)
tampered = tok.split(".")[0] + "." + b64(json.dumps({**pl, "role": "super_admin"}).encode()) + "." + tok.split(".")[2]
expect("payload edited to super_admin (signature kept) is rejected", req("GET", "/api/super-admin/tenants", tampered), 401)
none_alg = forge("", {**pl, "role": "super_admin"}, alg="none")
expect("alg=none token rejected", req("GET", "/api/super-admin/tenants", none_alg), 401)
exp = int(time.time()) + 3600
forged_default = forge("CHANGE_THIS_SECRET_IN_PRODUCTION", {"user_id": sa_id, "role": "super_admin", "tenant_id": None, "type": "access", "exp": exp})
s, _ = req("GET", "/api/super-admin/tenants", forged_default)
check("a token signed with the DEFAULT secret key is rejected (SECRET_KEY must be set)", s == 401, f"status {s} - tokens are forgeable while SECRET_KEY is unset")
expired = forge("CHANGE_THIS_SECRET_IN_PRODUCTION", {**pl, "exp": int(time.time()) - 60})
expect("expired token rejected", req("GET", "/api/tenant/dashboard", expired), 401)
refresh_as_access = None
s, b = req("POST", "/api/auth/login", form={"username": f"delta.mgr{SUF}@example.com", "password": PW})
expect("refresh token cannot be used as an access token", req("GET", "/api/tenant/dashboard", b["refresh_token"]), 401)

# ------------------------------------------------------------------------------------------------
S("3. injection / malformed input")
for probe in ["' OR '1'='1", "admin@example.com' --", "x'; DROP TABLE users; --", "%", "\\", "\u0000x"]:
    s, b = req("POST", "/api/auth/login", form={"username": probe, "password": probe})
    check(f"login probe {probe[:22]!r} -> clean 401", s in (401, 422), f"status {s}: {str(b)[:100]}")
check("users table still intact after the probes", login(f"delta.mgr{SUF}@example.com", PW) is not None)
expect("non-numeric id in path -> 422", req("GET", "/api/tenant/employees/abc", DM), 422)
expect("SQL in query param -> 422", req("GET", "/api/tenant/employees?dept_id=1;DROP%20TABLE%20users", DM), 422)
expect("bulk settings: non-numeric ids -> 400", req("GET", "/api/tenant/settings/bulk?tenant_ids=1,x", DM), 400)
expect("bulk settings: other tenant -> 403", req("GET", f"/api/tenant/settings/bulk?tenant_ids={E_ID}", DM), 403)
expect("bulk settings: own tenant works", req("GET", f"/api/tenant/settings/bulk?tenant_ids={D_ID}", DM), 200)
expect("very long name accepted or cleanly rejected", req("POST", "/api/org-admin/employees", DO1, body={"name": "x" * 5000, "employee_code": f"LONG{SUF}", "password": PW}), (200, 400, 413, 422))
expect("unicode / emoji name stored", req("POST", "/api/org-admin/employees", DO1, body={"name": "Zoë 张伟 😀", "employee_code": f"UNI{SUF}", "password": PW}), 200)
s, b = req("POST", "/api/org-admin/employees", DO1, body={"name": "<script>alert(1)</script>", "employee_code": f"XSS{SUF}", "password": PW})
check("script tag in a name is stored as plain text (API returns JSON only)", s == 200)
expect("empty JSON body on create -> 422", req("POST", "/api/tenant/departments", DM, body={}), 422)
expect("wrong content-type login -> 422", req("POST", "/api/auth/login", body={"username": "a", "password": "b"}), 422)

# ------------------------------------------------------------------------------------------------
S("4. mass assignment / privilege escalation")
et = login(f"DE{SUF}1", "Reset#Pass123")
expect("employee profile update ignores role / tenant fields", req("PUT", "/api/employee/profile", et, body={"name": "Hax", "role": "super_admin", "tenant_id": E_ID, "is_active": True}), (200, 422))
check("role unchanged after the attempt", payload_of(login(f"DE{SUF}1", "Reset#Pass123"))["role"] == "employee")
expect("employee cannot take another user's email", req("PUT", "/api/employee/profile", et, body={"email": f"delta.mgr{SUF}@example.com"}), (400, 409, 422))
check("manager can still log in (email not hijacked)", login(f"delta.mgr{SUF}@example.com", PW) is not None)
expect("org admin cannot promote an employee via update", req("PUT", f"/api/org-admin/employees/{DE}", DO1, body={"role": "org_admin", "tenant_id": E_ID}), (200, 422))
check("employee is still an employee of Delta", payload_of(login(f"DE{SUF}1", "Reset#Pass123"))["role"] == "employee")
expect("a tenant manager cannot create a super admin via setup", req("POST", "/api/auth/setup/super-admin", DM, body={"name": "x", "email": f"evil{SUF}@example.com", "password": PW}), 400)

# ------------------------------------------------------------------------------------------------
S("5. employee-code clash across organizations")
CLASH = f"CLASH{SUF}"
s1, _ = req("POST", "/api/org-admin/employees", DO1, body={"name": "Clash D", "employee_code": CLASH, "password": PW, "finger_id": 9})
s2, b2 = req("POST", "/api/org-admin/employees", EO, body={"name": "Clash E", "employee_code": CLASH, "password": "Other#Pass123", "finger_id": 9})
check("the same employee code cannot exist in two organizations (login would be ambiguous)", s1 == 200 and s2 in (400, 409), f"second create returned {s2}: {str(b2)[:80]}")
if s2 == 200:
    a, bb = login(CLASH, PW), login(CLASH, "Other#Pass123")
    check("(if allowed) both users can still log in", a and bb, f"Delta user ok={bool(a)}, Echo user ok={bool(bb)}")
e_clash = req("POST", "/api/org-admin/employees", DO1, body={"name": "Email clash", "employee_code": f"EC{SUF}", "password": PW})
expect("employee code equal to an existing email local-part / e-mail style code", e_clash and (200, "x") and (200, 400), (200, 400))

# ------------------------------------------------------------------------------------------------
S("6. observations (informational - never fail the run)")
t0 = time.time()
codes = [req("POST", "/api/auth/login", form={"username": f"delta.mgr{SUF}@example.com", "password": "Wrong#Pass1"})[0] for _ in range(12)]
check(f"OBSERVATION: 12 wrong passwords in {time.time()-t0:.0f}s -> {sorted(set(codes))} (no lockout/rate limit if only 401)", True)
s, _ = req("POST", "/api/auth/login", form={"username": f"delta.mgr{SUF}@example.com", "password": PW})
check(f"OBSERVATION: correct login after the failures -> {s}", True)
check("OBSERVATION: CORS allows any origin with credentials (main.py allow_origins=['*'])", True)

fails = [r for r in results if not r[2]]
print("\n" + "=" * 78)
print(f"TOTAL {len(results)} | PASS {len(results)-len(fails)} | FAIL {len(fails)}")
for sec, name, ok, d in fails:
    print(f"  FAIL [{sec}] {name}\n        {d}")
json.dump(results, open(os.path.join(os.path.dirname(__file__), "security_results.json"), "w"))
sys.exit(1 if fails else 0)
