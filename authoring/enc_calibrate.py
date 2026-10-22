#!/usr/bin/env python3
"""Encoder reward calibration, run in the zstd-gen image. Builds variants of
the reference encoder with features switched off and reports, per content
category, the credit each would earn against the reference under candidate
credit functions.

usage: enc_calibrate.py REF_SRC_DIR [SEED]
"""
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import content  # noqa: E402

SRC = sys.argv[1]
rng = random.Random(int(sys.argv[2]) if len(sys.argv) > 2 else 11)
TMP = tempfile.mkdtemp()
BASE = "greedy-full"  # the routine-encoder anchor

BASIC = {"OPTIMAL_PARSE": "false", "HUFFMAN_LITERALS": "false", "CUSTOM_TABLES": "false", "REP_SEARCH": "false",
         "LAZY_STEPS": "0", "CHAIN_DEPTH": "8"}
VARIANTS = {
    "lz-only": BASIC,
    "lz+huff": {**BASIC, "HUFFMAN_LITERALS": "true"},
    "lz+huff+tables": {**BASIC, "HUFFMAN_LITERALS": "true", "CUSTOM_TABLES": "true"},
    "greedy-full": {"OPTIMAL_PARSE": "false", "LAZY_STEPS": "0", "CHAIN_DEPTH": "16"},
    "lazy-full": {"OPTIMAL_PARSE": "false"},
    "lazy-deep": {"OPTIMAL_PARSE": "false", "CHAIN_DEPTH": "256", "GOOD_ENOUGH": "1024"},
    "ref": {},
    "opt-1pass": {"PASSES": "1"},
    "opt-basic": {"PASSES": "1", "SHORT_MATCH_MAX_OFFSET": "0"},
}


def build(name, consts):
    d = f"{TMP}/{name}"
    shutil.copytree(SRC, d, ignore=shutil.ignore_patterns("target"))
    for root, _, files in os.walk(f"{d}/src/enc"):
        for fn in files:
            p = os.path.join(root, fn)
            s = open(p).read()
            for k, v in consts.items():
                s = re.sub(rf"(const {k}: [a-z0-9]+ = )[^;]+;", rf"\g<1>{v};", s)
            open(p, "w").write(s)
    subprocess.run(["cargo", "build", "--release", "--offline", "-q"], cwd=d, check=True)
    return f"{d}/target/release/zstdr"


def cases():
    for g in [content.text, content.json_logs, content.source, content.records, content.dna,
              content.csv, content.mixed]:
        for _ in range(6):
            yield g.__name__, g(random.Random(rng.getrandbits(64)), int(2 ** rng.uniform(11, 20.5)))
    import glob
    real = sorted(glob.glob("/tmp/zstd-1.5.6/lib/*/*.[ch]") + glob.glob("/tmp/zstd-1.5.6/doc/*.md"))
    for p in rng.sample(real, 10):
        yield "realsrc", open(p, "rb").read()
    blob = open("/usr/local/bin/zstd", "rb").read()
    for _ in range(4):
        a = rng.randint(0, len(blob) - 300000)
        yield "binary", blob[a:a + rng.randint(20000, 300000)]


def size_of(binary, data):
    open(f"{TMP}/in", "wb").write(data)
    subprocess.run([binary, "-z", f"{TMP}/in", f"{TMP}/out"], check=True)
    return os.path.getsize(f"{TMP}/out")


def main():
    import math
    bins = {n: build(n, c) for n, c in VARIANTS.items()}
    rows = []
    for cat, data in cases():
        sizes = {n: size_of(b, data) for n, b in bins.items()}
        stored = len(data) + 9 + 3 * max(1, -(-len(data) // 131072))
        rows.append((cat, len(data), stored, sizes))
    cats = sorted({r[0] for r in rows})

    def clamp(v):
        return max(0.0, min(1.0, v))

    def credit(kind, stored, ref, cand, base):
        if kind == "linear":
            return clamp((stored - cand) / max(1, stored - ref))
        if kind == "log":
            return clamp(math.log(stored / cand) / math.log(stored / ref)) if stored > ref else 0.0
        # twoanchor: 0.3 for stored -> baseline, 0.7 for baseline -> reference (log sizes)
        a = clamp(math.log(stored / cand) / math.log(stored / base)) if stored > base else 1.0
        b = clamp(math.log(base / cand) / math.log(base / ref)) if base > ref else (1.0 if cand <= ref else 0.0)
        return 0.3 * a + 0.7 * b

    for kind in ("log", "twoanchor"):
        print(f"\n== credit: {kind}  (mean over categories, then per category)")
        print(f"{'variant':16s} {'MEAN':>6s} " + " ".join(f"{c[:8]:>8s}" for c in cats))
        for n in VARIANTS:
            per = {c: [credit(kind, st, sz['ref'], sz[n], sz[BASE]) for cat, _, st, sz in rows if cat == c] for c in cats}
            means = {c: sum(v) / len(v) for c, v in per.items()}
            overall = sum(means.values()) / len(means)
            print(f"{n:16s} {overall:6.3f} " + " ".join(f"{means[c]:8.3f}" for c in cats))
    tot = {n: sum(r[3][n] for r in rows) for n in VARIANTS}
    print("\ntotal bytes:", {n: tot[n] for n in VARIANTS}, "stored:", sum(r[2] for r in rows))


if __name__ == "__main__":
    main()
