"""(Re)create schema qa_test with the tables of the current models. Never touches schema public."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import qa_common as q  # noqa: E402

q.install_qa_engine_patch()
os.chdir(q.BACKEND)
sys.path.insert(0, q.BACKEND)
import logging  # noqa: E402
from sqlalchemy import text  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.models.domain import Base  # noqa: E402

engine.echo = False
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def ensure_empty_schema():
    """current_schema() is NULL while the schema does not exist, which would make the pin check fail on a fresh
    run. Create the (empty, literal-name) QA schema first; the pin check below still has to pass before anything
    else happens."""
    import psycopg
    q.prefer_ipv4()
    with psycopg.connect(**q.psycopg_kwargs(direct=True), autocommit=True) as c:
        c.execute(f"CREATE SCHEMA IF NOT EXISTS {q.QA_SCHEMA}")


async def main():
    ensure_empty_schema()
    await q.assert_pinned(engine)  # schema must not even be created unless the pin works (disposes the pool afterwards)
    async with engine.begin() as c:
        await c.execute(text(f"DROP SCHEMA IF EXISTS {q.QA_SCHEMA} CASCADE"))
        await c.execute(text(f"CREATE SCHEMA {q.QA_SCHEMA}"))
    async with engine.begin() as c:  # new connection picks up the schema in search_path
        print("current_schema:", (await c.execute(text("select current_schema()"))).scalar(),
              "| search_path:", (await c.execute(text("show search_path"))).scalar())
        await c.run_sync(Base.metadata.create_all)
        n = (await c.execute(text("select count(*) from pg_tables where schemaname=:s"), {"s": q.QA_SCHEMA})).scalar()
        users = (await c.execute(text("select count(*) from users"))).scalar()
        print(f"{n} tables created in {q.QA_SCHEMA}; users in this schema: {users}")
        try:
            await c.execute(text("select count(*) from public.users"))
            print("(public is reachable only when schema-qualified, as expected)")
        except Exception as e:
            print("public.users:", e)

asyncio.run(main())
