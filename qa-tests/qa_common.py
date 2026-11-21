"""Shared helpers for the isolated QA environment (schema qa_test in the Neon database).

The QA backend is pinned to schema qa_test via a startup search_path on the DIRECT (non-pooled)
Neon endpoint, with NO fallback to schema public -> it cannot read or write the real tables.
"""
import hashlib
import os
import socket
import urllib.parse

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../attendance-old-backend
ENV_PATH = os.path.join(BACKEND, ".env")
QA_SCHEMA = "qa_test"

# libpq/asyncpg would try Neon's IPv6 addresses first (they time out here) -> prefer IPv4
_orig = socket.getaddrinfo


def prefer_ipv4():
    socket.getaddrinfo = lambda *a, **k: ([r for r in _orig(*a, **k) if r[0] == socket.AF_INET] or _orig(*a, **k))


def read_env():
    out = {}
    for line in open(ENV_PATH, encoding="utf8"):
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.rstrip("\n").split("=", 1)
            out[k.strip()] = v
    return out


def real_url():
    return read_env()["DATABASE_URL"]


def direct_url():
    """asyncpg URL for the direct (non-pooler) endpoint - needed for startup search_path."""
    u = real_url()
    return u.replace("-pooler.", ".")


def psycopg_kwargs(direct=True):
    u = urllib.parse.urlparse(real_url().replace("postgresql+asyncpg://", "postgresql://"))
    host = u.hostname.replace("-pooler.", ".") if direct else u.hostname
    ipv4 = next(r[4][0] for r in socket.getaddrinfo(host, 5432, socket.AF_INET, socket.SOCK_STREAM))
    return dict(host=host, hostaddr=ipv4, port=u.port or 5432, dbname=u.path.lstrip("/"),
                user=urllib.parse.unquote(u.username), password=urllib.parse.unquote(u.password),
                sslmode="require", connect_timeout=20)


def fingerprint_public(conn):
    """Read-only: per-table row count + md5 of all rows in schema public."""
    cur = conn.cursor()
    cur.execute("select tablename from pg_tables where schemaname='public' order by 1")
    out = {}
    for (t,) in cur.fetchall():
        cur.execute(f'select count(*) from public."{t}"')
        n = cur.fetchone()[0]
        cur.execute(f'select coalesce(md5(string_agg(x::text, \'|\' order by x::text)), \'\') from public."{t}" x')
        out[t] = (n, cur.fetchone()[0])
    return out


def install_qa_engine_patch():
    """Pin every SQLAlchemy async engine created afterwards to schema qa_test (NO fallback to public).

    The SET is executed on the raw asyncpg connection, OUTSIDE any transaction, so a ROLLBACK (which every
    read-only request ends with) can never undo it - and it is re-applied on every pool checkout as well.
    Only valid on the DIRECT Neon endpoint (a session-level SET would be shared through the pooler).
    """
    prefer_ipv4()
    os.environ["DATABASE_URL"] = direct_url()
    assert "-pooler" not in os.environ["DATABASE_URL"], "refusing to use the pooled endpoint for the QA schema"
    import sqlalchemy.ext.asyncio as sa_async
    from sqlalchemy import event
    original = sa_async.create_async_engine

    def patched(url, **kw):
        engine = original(url, **kw)

        from sqlalchemy.util import await_only

        def _pin(dbapi_conn, _record, *_):
            # raw asyncpg connection, no transaction -> not undone by any later ROLLBACK
            await_only(dbapi_conn._connection.execute(f"SET search_path TO {QA_SCHEMA}"))

        event.listen(engine.sync_engine, "connect", _pin)
        event.listen(engine.sync_engine, "checkout", _pin)
        return engine

    sa_async.create_async_engine = patched


async def assert_pinned(engine, rounds=8):
    """Abort unless the engine is pinned to the QA schema on EVERY connection cycle, including after rollbacks."""
    from sqlalchemy import text
    seen = []
    for _ in range(rounds):
        async with engine.connect() as c:  # read-only use -> ends with ROLLBACK, the case that broke the first attempt
            sp = (await c.execute(text("show search_path"))).scalar()
            cs = (await c.execute(text("select current_schema()"))).scalar()
            seen.append((sp.strip('"'), cs))
    bad = [x for x in seen if x != (QA_SCHEMA, QA_SCHEMA)]
    if bad:
        raise SystemExit(f"ISOLATION CHECK FAILED ({len(bad)}/{rounds} cycles not pinned): {bad[:3]} - aborting")
    await engine.dispose()  # connections are bound to this event loop; let the real app open its own
    print(f"isolation verified: {rounds}/{rounds} connect-rollback cycles pinned to {QA_SCHEMA}", flush=True)
