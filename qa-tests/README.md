# QA tests (isolated from real data)

These tests run the **real backend code** against a **separate schema (`qa_test`) in the same Neon
database**, so nothing in your real tables (`public`) is read or written.

How the isolation works (see `qa_common.py`):
* the QA backend uses the **direct** (non-pooled) Neon endpoint and runs `SET search_path TO qa_test` on the raw
  connection, outside any transaction (a rollback can't undo it), re-applied on every pool checkout;
* there is **no fallback to `public`** - a missing table is an error, never a read of real data;
* `qa_setup.py` / `run_api_qa.py` abort unless `assert_pinned()` proves 8 connect/rollback cycles are pinned to `qa_test`;
* `fingerprint.py save|check` stores/compares a row-count + md5 fingerprint of every `public` table, so you can
  prove the real data is byte-for-byte unchanged after a run.

## Run (from this folder, using the backend virtualenv)

```powershell
..\.venv\Scripts\python fingerprint.py save      # 1. fingerprint the real data (read-only)
..\.venv\Scripts\python qa_setup.py              # 2. (re)create schema qa_test with the model tables
..\.venv\Scripts\python run_api_qa.py            # 3. QA backend on http://localhost:8001   (keep running)
..\.venv\Scripts\python api_suite.py             # 4. ~200 checks: auth, every role, device punches, leaves, isolation, sweep
..\.venv\Scripts\python security_suite.py        #    security/edge cases (needs api_suite.py to have run first)
..\.venv\Scripts\python fingerprint.py check     # 5. real data unchanged?
```

`QA_SKIP_SWEEP=1` skips the slow "every endpoint x every role" sweep in `api_suite.py`.
Cleanup: `DROP SCHEMA qa_test CASCADE;`
