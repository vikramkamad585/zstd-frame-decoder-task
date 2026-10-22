#!/usr/bin/env python3
"""Generate encoder cases, run in the zstd-gen image.

usage: enc_corpus.py REF_SRC_DIR OUTDIR SEED PER_CATEGORY MAX_LOG2

Builds the reference encoder and the baseline encoder (the reference's
greedy hash-chain settings), generates inputs per category, and writes
OUTDIR/inputs/NNNNN.bin plus OUTDIR/manifest.json with, per case, the stored
size (one frame of Raw blocks), the baseline size and the reference size.
Every reference output is checked with zstd 1.5.6. Cases where the input
barely compresses, or where the reference does not beat the baseline by at
least 1%, are dropped: they cannot separate encoders.
"""
import glob
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import content  # noqa: E402

SRC, OUT, SEED, PER, MAX_LOG2 = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), float(sys.argv[5])
rng = random.Random(SEED)
TMP = tempfile.mkdtemp()
BASELINE = {"OPTIMAL_PARSE": "false", "LAZY_STEPS": "0", "CHAIN_DEPTH": "16"}


def build(name, consts):
    d = f"{TMP}/{name}"
    shutil.copytree(SRC, d, ignore=shutil.ignore_patterns("target"))
    for p in glob.glob(f"{d}/src/enc/*.rs"):
        s = open(p).read()
        for k, v in consts.items():
            s, n = re.subn(rf"(const {k}: [a-z0-9]+ = )[^;]+;", rf"\g<1>{v};", s)
        open(p, "w").write(s)
    subprocess.run(["cargo", "build", "--release", "--offline", "-q"], cwd=d, check=True)
    return f"{d}/target/release/zstdr"


def stored_size(n):
    fcs = 1 if n < 256 else 2 if n < 65536 + 256 else 4 if n < 1 << 32 else 8
    blocks = max(1, -(-n // 131072))
    return 4 + 1 + fcs + 3 * blocks + n


REAL = sorted(set(glob.glob("/tmp/zstd-1.5.6/lib/*/*.[ch]") + glob.glob("/tmp/zstd-1.5.6/programs/*.[ch]")
                  + glob.glob("/tmp/zstd-1.5.6/doc/*.md") + glob.glob("/tmp/zstd-1.5.6/*.md")))
ELF = sorted(p for p in glob.glob("/usr/bin/*") + glob.glob("/usr/lib/*/*.so*") + ["/usr/local/bin/zstd"]
             if os.path.isfile(p) and not os.path.islink(p) and os.path.getsize(p) > 300_000
             and open(p, "rb").read(4) == b"\x7fELF")


def real(r, size):
    out = bytearray()
    while len(out) < size:
        out += open(r.choice(REAL), "rb").read()
    a = r.randint(0, len(out) - size)
    return bytes(out[a:a + size])


def binary(r, size):
    blob = open(r.choice(ELF), "rb").read()
    size = min(size, len(blob))
    a = r.randint(0, len(blob) - size)
    return blob[a:a + size]


def long_range(r, size):
    gen = r.choice([content.text, content.source, content.json_logs, content.csv])
    base = gen(random.Random(r.getrandbits(64)), size // 4)
    fill = r.choice([content.text, content.dna, content.records, content.mixed])
    middle = fill(random.Random(r.getrandbits(64)), size - 2 * len(base))
    # The repeat comes back with a few small edits.
    again = bytearray(base)
    for _ in range(r.randint(0, 20)):
        i = r.randrange(len(again))
        again[i] = r.randrange(256)
    return base + middle + bytes(again)


CATEGORIES = [
    ("text", content.text), ("logs", content.json_logs), ("source", content.source),
    ("csv", content.csv), ("records", content.records), ("dna", content.dna),
    ("real", real), ("binary", binary), ("long", long_range),
]


def main():
    ref = build("ref", {})
    base = build("baseline", BASELINE)
    os.makedirs(f"{OUT}/inputs", exist_ok=True)
    cases, dropped = [], {}
    for cat, gen in CATEGORIES:
        want = max(1, PER // 2) if cat == "long" else PER
        got = 0
        while got < want:
            if cat == "long":
                size = int(2 ** rng.uniform(21, min(22.3, MAX_LOG2 + 2)))
            else:
                size = int(2 ** rng.uniform(10, MAX_LOG2))
            data = gen(random.Random(rng.getrandbits(64)), size)
            src = f"{TMP}/in"
            open(src, "wb").write(data)
            subprocess.run([ref, "-z", src, f"{TMP}/ref.zst"], check=True)
            subprocess.run([base, "-z", src, f"{TMP}/base.zst"], check=True)
            back = subprocess.run(["zstd", "-q", "-d", "-c", f"{TMP}/ref.zst"], stdout=subprocess.PIPE, check=True).stdout
            assert back == data, f"zstd does not reproduce a {cat} input"
            s, b, rr = stored_size(len(data)), os.path.getsize(f"{TMP}/base.zst"), os.path.getsize(f"{TMP}/ref.zst")
            if b > 0.95 * s or rr > 0.99 * b:
                dropped[cat] = dropped.get(cat, 0) + 1
                continue
            cid = f"{len(cases):05d}"
            shutil.copy(src, f"{OUT}/inputs/{cid}.bin")
            cases.append({"id": cid, "category": cat, "size": len(data), "stored_size": s,
                          "baseline_size": b, "reference_size": rr})
            got += 1
    with open(f"{OUT}/manifest.json", "w") as f:
        json.dump({"cases": cases}, f, indent=0)
    print(f"{len(cases)} cases, {sum(c['size'] for c in cases)} input bytes; dropped {dropped}")


if __name__ == "__main__":
    main()
