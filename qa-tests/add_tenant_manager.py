"""Add a tenant_admin (tenant manager) login to ONE existing tenant, directly in the database.

    ..\\.venv\\Scripts\\python add_tenant_manager.py "<tenant name>" <email> ["<display name>"]

Used when no super-admin session is available (the normal way is the Super Admin console at /admin/tenants).
Refuses unless the tenant name matches exactly one tenant and the email is unused (case-insensitive). Inserts a
single row; changes nothing else. The generated password is stored in .env as TENANT_MANAGER_<n>_EMAIL/_PASSWORD.
"""
import os
import secrets
import string
import sys

sys.path.insert(0, os.path.dirname(__file__))
import qa_common as q  # noqa: E402

q.prefer_ipv4()
sys.path.insert(0, q.BACKEND)
import psycopg  # noqa: E402
from app.core.security import hash_password, validate_password_strength  # noqa: E402

tenant_name, email = sys.argv[1], sys.argv[2].strip().lower()
display = sys.argv[3] if len(sys.argv) > 3 else f"{tenant_name} Admin"


def new_password(n=14):
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(n))
        if validate_password_strength(pw)[0] and any(c in "!@#$%^&*" for c in pw):
            return pw


password = new_password()
with psycopg.connect(**q.psycopg_kwargs(direct=False), autocommit=False) as conn:
    cur = conn.cursor()
    cur.execute("select id, name from public.tenants where lower(name) = lower(%s)", (tenant_name,))
    rows = cur.fetchall()
    if len(rows) != 1:
        raise SystemExit(f"expected exactly one tenant named {tenant_name!r}, found {rows}")
    tid, tname = rows[0]
    cur.execute("select 1 from public.users where lower(email) = %s", (email,))
    if cur.fetchone():
        raise SystemExit(f"email {email} is already used by another account - nothing changed")
    cur.execute(
        "insert into public.users (tenant_id, name, email, password_hash, role, is_active) "
        "values (%s, %s, %s, %s, 'tenant_admin', true) returning id",
        (tid, display, email, hash_password(password)),
    )
    uid = cur.fetchone()[0]
    conn.commit()
print(f"created tenant_admin id={uid} for tenant {tid} ({tname}) with email {email}")

env = open(q.ENV_PATH, encoding="utf8").read()
n = 1
while f"TENANT_MANAGER_{n}_EMAIL=" in env:
    n += 1
with open(q.ENV_PATH, "a", encoding="utf8") as f:
    f.write(f"\n# tenant manager for {tname} (created {__import__('datetime').date.today()})\nTENANT_MANAGER_{n}_EMAIL={email}\nTENANT_MANAGER_{n}_PASSWORD={password}\n")
print(f"credentials saved to .env as TENANT_MANAGER_{n}_EMAIL / TENANT_MANAGER_{n}_PASSWORD")
