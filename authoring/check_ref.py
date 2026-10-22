#!/usr/bin/env python3
"""Run a decoder binary over a generated case set; report per-category results."""
import hashlib, json, os, subprocess, sys, tempfile
from concurrent.futures import ThreadPoolExecutor
binary, root = sys.argv[1], sys.argv[2]
cases = json.load(open(f"{root}/manifest.json"))["cases"]
tmp = tempfile.mkdtemp()
def one(c):
    out = f"{tmp}/{c['id']}.out"
    args = [binary] + (["-D", f"{root}/dicts/{c['dict']}"] if c["dict"] else []) + [f"{root}/cases/{c['id']}.zst", out]
    try:
        p = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
    except subprocess.TimeoutExpired:
        return c, False, "timeout"
    if c["expect"] == "error":
        ok = p.returncode != 0
    else:
        ok = p.returncode == 0 and os.path.exists(out) and hashlib.sha256(open(out, "rb").read()).hexdigest() == c["sha256"]
    if os.path.exists(out): os.remove(out)
    return c, ok, p.stderr.decode()[-200:]
res = {}
fails = []
with ThreadPoolExecutor(8) as ex:
    for c, ok, err in ex.map(one, cases):
        r = res.setdefault(c["category"], [0, 0]); r[1] += 1; r[0] += ok
        if not ok: fails.append((c["id"], c["category"], c["note"], err.strip()))
for k, (a, b) in res.items(): print(f"{k:12s} {a}/{b}")
for f in fails[:40]: print("FAIL", *f)
print("total fails", len(fails))
