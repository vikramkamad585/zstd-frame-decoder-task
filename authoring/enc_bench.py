#!/usr/bin/env python3
"""Encoder bench, run in the zstd-gen image. For every input: compress with
the decoder-under-test's `-z`, decode the result with zstd 1.5.6 and with the
binary itself, and compare the size against zstd levels.

usage: enc_bench.py ZSTDR_BINARY [SEED]
"""
import glob
import os
import random
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import content  # noqa: E402

ZSTDR = sys.argv[1]
rng = random.Random(int(sys.argv[2]) if len(sys.argv) > 2 else 5)
TMP = tempfile.mkdtemp()


def inputs():
    for g in content.GENS + [content.mixed]:
        if g is content.noise:
            continue
        for size in (100, 3000, 60000, 400000, 1500000):
            yield f"{g.__name__}-{size}", g(random.Random(rng.getrandbits(64)), size)
    real = sorted(glob.glob("/tmp/zstd-1.5.6/lib/*/*.c") + glob.glob("/tmp/zstd-1.5.6/doc/*.md"))
    for p in rng.sample(real, 8):
        yield "real-" + os.path.basename(p), open(p, "rb").read()
    for p in ["/usr/local/bin/zstd", "/usr/lib/x86_64-linux-gnu/libc.so.6", "/usr/lib/aarch64-linux-gnu/libc.so.6"]:
        if os.path.exists(p):
            yield "elf-" + os.path.basename(p), open(p, "rb").read()[:2_000_000]
    base = content.text(random.Random(9), 300000)
    yield "longrange-4M", base + content.noise(random.Random(3), 3_000_000) + base


def zstd_size(path, level):
    out = subprocess.run(["zstd", "-q", "-c", "--no-check", f"-{level}", path],
                         stdout=subprocess.PIPE, check=True).stdout
    return len(out)


def main():
    bad = 0
    tot = {"ours": 0, "z3": 0, "z9": 0, "z19": 0, "raw": 0}
    print(f"{'input':28s} {'raw':>9s} {'ours':>9s} {'zstd-3':>9s} {'zstd-9':>9s} {'zstd-19':>9s} {'secs':>6s}")
    for name, data in inputs():
        src = f"{TMP}/in"
        open(src, "wb").write(data)
        t0 = time.time()
        p = subprocess.run([ZSTDR, "-z", src, f"{TMP}/out.zst"], stderr=subprocess.PIPE)
        secs = time.time() - t0
        ok = p.returncode == 0
        note = p.stderr.decode().strip()
        if ok:
            z = subprocess.run(["zstd", "-q", "-d", "-c", f"{TMP}/out.zst"], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
            ok = z.returncode == 0 and z.stdout == data
            if not ok:
                note += f" zstd-decode: rc={z.returncode} {z.stderr.decode()[:100]}"
            r = subprocess.run([ZSTDR, f"{TMP}/out.zst", f"{TMP}/back"])
            if r.returncode != 0 or open(f"{TMP}/back", "rb").read() != data:
                ok = False
                note += " self-decode mismatch"
        size = os.path.getsize(f"{TMP}/out.zst") if os.path.exists(f"{TMP}/out.zst") else -1
        row = [len(data), size, zstd_size(src, 3), zstd_size(src, 9), zstd_size(src, 19)]
        for k, v in zip(["raw", "ours", "z3", "z9", "z19"], row):
            tot[k] += v
        bad += not ok
        print(f"{name:28s} " + " ".join(f"{v:9d}" for v in row) + f" {secs:6.2f}"
              + ("" if ok else "  FAIL") + (f"  [{note}]" if note else ""))
    print(f"{'TOTAL':28s} {tot['raw']:9d} {tot['ours']:9d} {tot['z3']:9d} {tot['z9']:9d} {tot['z19']:9d}")
    print(f"failures: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
