"""QA backend on :8001, pinned to schema qa_test (cannot see schema public)."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import qa_common as q  # noqa: E402

q.install_qa_engine_patch()
os.chdir(q.BACKEND)
sys.path.insert(0, q.BACKEND)
import asyncio  # noqa: E402
import uvicorn  # noqa: E402
from app.db.session import engine  # noqa: E402

asyncio.run(q.assert_pinned(engine))  # abort unless pinned to qa_test
print('QA backend pinned to schema', q.QA_SCHEMA, flush=True)

uvicorn.run("main:app", host="127.0.0.1", port=8001)
