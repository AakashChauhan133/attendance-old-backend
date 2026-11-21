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


