"""fingerprint.py save|check  - read-only fingerprint of schema public (the real data)."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import qa_common as q  # noqa: E402

q.prefer_ipv4()
import psycopg  # noqa: E402

FILE = os.path.join(os.path.dirname(__file__), "public_fingerprint.json")
mode = sys.argv[1]
with psycopg.connect(**q.psycopg_kwargs(direct=False), autocommit=False) as conn:
    conn.read_only = True
    fp = q.fingerprint_public(conn)

if mode == "save":
    json.dump(fp, open(FILE, "w"))
    print("saved fingerprint of", len(fp), "public tables:")
    for t, (n, h) in fp.items():
        print(f"  {t:22} rows={n:<6} md5={h[:10]}")
else:
    before = json.load(open(FILE))
    bad = [t for t in set(before) | set(fp) if tuple(before.get(t, [])) != tuple(fp.get(t, []))]
    print("public tables compared:", len(fp))
    print("CHANGED:", bad if bad else "none - real data is byte-for-byte identical")
    sys.exit(1 if bad else 0)
