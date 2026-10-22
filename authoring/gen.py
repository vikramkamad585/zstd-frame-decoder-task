#!/usr/bin/env python3
"""Generate zstd decoder conformance cases with the pinned zstd v1.5.6 CLI.

Usage: gen.py OUTDIR SEED SCALE KIND
  KIND = hidden  -> manifest stores sha256/size only
  KIND = visible -> manifest plus full expected outputs (*.out)

Every success case is checked by decoding it with zstd itself and comparing
against the original content. Every rejection case is kept only if zstd
rejects it AND the rejection follows from the published contract (checksum
mismatch, truncation, header faults), so any conforming decoder agrees.
"""
import hashlib
import json
import os
import random
import struct
import subprocess
import sys
import tempfile

from tag_compressed import has_compressed

ZSTD = "/usr/local/bin/zstd"
OUT, SEED, SCALE, KIND = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
rng = random.Random(SEED)
TMP = tempfile.mkdtemp()
os.makedirs(f"{OUT}/cases", exist_ok=True)
os.makedirs(f"{OUT}/dicts", exist_ok=True)
manifest = []
stats = {}


def n(count):
    return max(1, int(round(count * SCALE)))


# ---------------------------------------------------------------- content
SYL = ["ka", "to", "ri", "en", "sa", "mu", "lo", "ve", "an", "is", "or", "the", "de", "qu",
       "ph", "st", "ing", "ion", "er", "al", "pre", "con", "ex", "um", "ox", "tr", "zy"]


def vocab(r, k=600):
    return ["".join(r.choice(SYL) for _ in range(r.randint(1, 4))) for _ in range(k)]


def text(r, size):
    v = vocab(r)
    w = [1.0 / (i + 1) for i in range(len(v))]
    out, cur = [], 0
    while cur < size:
        s = " ".join(r.choices(v, w, k=r.randint(4, 18)))
        s = s[0].upper() + s[1:] + r.choice([".", ".", ".", "?", "!", ";"])
        if r.random() < 0.15:
            s += "\n\n"
        else:
            s += " "
        out.append(s)
        cur += len(s)
    return "".join(out).encode()[:size]


def json_logs(r, size):
    svcs = ["auth", "billing", "search", "ingest", "gateway", "scheduler", "cache"]
    lv = ["DEBUG", "INFO", "INFO", "INFO", "WARN", "ERROR"]
    msgs = ["request completed", "cache miss", "retrying upstream", "token refreshed",
            "slow query", "connection reset", "queue drained", "flush complete"]
    ts = 1700000000000 + r.randint(0, 10**9)
    out, cur = [], 0
    while cur < size:
        ts += r.randint(0, 2500)
        line = json.dumps({"ts": ts, "level": r.choice(lv), "svc": r.choice(svcs),
                           "msg": r.choice(msgs), "latency_ms": round(r.expovariate(0.02), 2),
                           "req": "%016x" % r.getrandbits(64), "code": r.choice([200, 200, 204, 404, 500])})
        out.append(line + "\n")
        cur += len(line) + 1
    return "".join(out).encode()[:size]


def source(r, size):
    ids = vocab(r, 120)
    types = ["int", "size_t", "uint32_t", "const char *", "void", "double", "struct node *"]
    out, cur = [], 0
    while cur < size:
        name = r.choice(ids) + "_" + r.choice(ids)
        body = []
        for _ in range(r.randint(2, 12)):
            a, b = r.choice(ids), r.choice(ids)
            body.append(r.choice([
                f"    {a} = {b} + {r.randint(0, 255)};",
                f"    if ({a} < {b}) {{\n        return {a};\n    }}",
                f"    for (int i = 0; i < {b}; i++) {{\n        {a}[i] ^= {b}[i];\n    }}",
                f"    {a}->{b} = {r.choice(ids)}({a}, {b});",
            ]))
        fn = f"static {r.choice(types)} {name}({r.choice(types)} {r.choice(ids)})\n{{\n" + "\n".join(body) + "\n}\n\n"
        out.append(fn)
        cur += len(fn)
    return "".join(out).encode()[:size]


def records(r, size):
    out = bytearray()
    i = r.randint(0, 1000)
    x = r.random() * 100
    while len(out) < size:
        x += r.gauss(0, 1)
        out += struct.pack("<IIdHH", i, i * 7 + 3, x, r.randint(0, 3), 0xBEEF)
        i += 1
    return bytes(out[:size])


def noise(r, size):
    return r.randbytes(size)


def runs(r, size):
    out = bytearray()
    while len(out) < size:
        out += bytes([r.choice([0, 0, 0, 0xFF, r.randint(0, 255)])]) * r.randint(1, 5000)
    return bytes(out[:size])


def dna(r, size):
    motif = "".join(r.choice("ACGT") for _ in range(r.randint(20, 400)))
    out, cur = [], 0
    while cur < size:
        if r.random() < 0.3:
            s = motif
        else:
            s = "".join(r.choices("ACGT", [4, 1, 1, 4], k=r.randint(20, 300)))
        out.append(s)
        cur += len(s)
    return "".join(out).encode()[:size]


def csv(r, size):
    out, cur = ["id,lat,lon,temp,station\n"], 0
    i = 0
    while cur < size:
        line = f"{i},{r.uniform(-90, 90):.5f},{r.uniform(-180, 180):.5f},{r.gauss(15, 8):.2f},ST{r.randint(1, 60):03d}\n"
        out.append(line)
        cur += len(line)
        i += 1
    return "".join(out).encode()[:size]


GENS = [text, json_logs, source, records, noise, runs, dna, csv]


def mixed(r, size):
    out = bytearray()
    while len(out) < size:
        out += r.choice(GENS)(r, r.randint(100, 40000))
    return bytes(out[:size])


ALL_GENS = GENS + [mixed, mixed]


def content(size=None, gen=None):
    if size is None:
        size = int(2 ** rng.uniform(4, 21))
    g = gen or rng.choice(ALL_GENS)
    return g(random.Random(rng.getrandbits(64)), size)


# ---------------------------------------------------------------- zstd helpers
def run(args, data=None):
    p = subprocess.run(args, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return p.returncode, p.stdout


def compress(data, opts, stdin=False):
    src = f"{TMP}/src"
    with open(src, "wb") as f:
        f.write(data)
    if stdin:
        rc, out = run([ZSTD, "-q", "-c", *opts, "-"], data)
    else:
        rc, out = run([ZSTD, "-q", "-c", *opts, src])
    if rc != 0:
        return None
    return out


def zstd_decode(frame, dict_path=None, no_check=False):
    p = f"{TMP}/in.zst"
    with open(p, "wb") as f:
        f.write(frame)
    args = [ZSTD, "-d", "-q", "-c"]
    if dict_path:
        args += ["-D", dict_path]
    if no_check:
        args += ["--no-check"]
    rc, out = run(args + [p])
    return rc, out


def add(cat, frame, expect_ok, original=None, dict_name=None, note=""):
    if frame is None:
        stats["compress_failed"] = stats.get("compress_failed", 0) + 1
        return False
    dpath = f"{OUT}/dicts/{dict_name}" if dict_name else None
    rc, out = zstd_decode(frame, dpath)
    if expect_ok:
        if rc != 0 or (original is not None and out != original):
            stats.setdefault("dropped_" + cat, 0)
            stats["dropped_" + cat] += 1
            return False
    else:
        if rc == 0:
            stats.setdefault("dropped_" + cat, 0)
            stats["dropped_" + cat] += 1
            return False
    cid = f"{len(manifest):05d}"
    with open(f"{OUT}/cases/{cid}.zst", "wb") as f:
        f.write(frame)
    entry = {"id": cid, "category": cat, "dict": dict_name, "expect": "ok" if expect_ok else "error", "note": note}
    if expect_ok:
        entry["sha256"] = hashlib.sha256(out).hexdigest()
        entry["size"] = len(out)
        entry["compressed"] = has_compressed(frame)
        if KIND == "visible":
            with open(f"{OUT}/cases/{cid}.out", "wb") as f:
                f.write(out)
    manifest.append(entry)
    stats[cat] = stats.get(cat, 0) + 1
    return True


# ---------------------------------------------------------------- categories
def cat_levels():
    for lvl in range(1, 20):
        for _ in range(n(6)):
            d = content()
            add("levels", compress(d, [f"-{lvl}"]), True, d, note=f"-{lvl}")
    for lvl in (20, 21, 22):
        for _ in range(n(3)):
            d = content(int(2 ** rng.uniform(10, 21)))
            add("levels", compress(d, ["--ultra", f"-{lvl}"]), True, d, note=f"--ultra -{lvl}")


def cat_fast():
    for f in (1, 2, 3, 4, 5, 6, 7, 10, 50, 200):
        for _ in range(n(5)):
            d = content()
            add("fast", compress(d, [f"--fast={f}"]), True, d, note=f"--fast={f}")


def cat_params():
    made = 0
    while made < n(150):
        strat = rng.randint(1, 9)
        wlog = rng.randint(10, 23)
        p = [f"strategy={strat}", f"wlog={wlog}", f"hlog={rng.randint(6, min(wlog + 1, 22))}",
             f"clog={rng.randint(6, min(wlog + 1, 22))}", f"slog={rng.randint(1, 9)}",
             f"mml={rng.randint(3, 7)}"]
        if strat >= 7:
            p.append(f"tlen={rng.randint(0, 999)}")
        opts = [f"-{rng.randint(1, 19)}", "--zstd=" + ",".join(p)]
        if rng.random() < 0.3:
            opts.append("--no-check")
        d = content()
        frame = compress(d, opts)
        if frame is None:
            continue
        if add("params", frame, True, d, note=" ".join(opts)):
            made += 1


def far_repeat(size, gap_gen=runs):
    r = random.Random(rng.getrandbits(64))
    head = r.randbytes(r.randint(200_000, 1_000_000))
    filler = gap_gen(r, size)
    return head + filler + head


def cat_long():
    for _ in range(n(16)):
        wl = rng.randint(24, 27)
        d = content(int(2 ** rng.uniform(12, 20)))
        add("long", compress(d, [f"--long={wl}", f"-{rng.randint(1, 19)}"], stdin=True), True, d,
            note=f"stdin --long={wl}")
    for _ in range(n(10) if KIND == "hidden" else 0):
        wl = rng.choice([24, 25, 26, 27])
        gap = int(2 ** wl * rng.uniform(0.35, 0.9))
        d = far_repeat(gap)
        opts = [f"--long={wl}", f"-{rng.randint(1, 12)}"]
        if rng.random() < 0.5:
            opts.append("-T2")
        add("long", compress(d, opts), True, d, note=" ".join(opts) + f" size={len(d)}")
    for _ in range(n(6)):
        wlog = rng.randint(21, 27)
        d = content(int(2 ** rng.uniform(14, 21)))
        add("long", compress(d, [f"--zstd=wlog={wlog}", f"-{rng.randint(3, 19)}"], stdin=True), True, d,
            note=f"stdin wlog={wlog}")


def train_dicts(tag, count):
    names = []
    for k in range(count):
        gen = rng.choice([json_logs, text, source, csv, records])
        r = random.Random(rng.getrandbits(64))
        sd = f"{TMP}/train_{tag}_{k}"
        os.makedirs(sd, exist_ok=True)
        for i in range(400):
            with open(f"{sd}/s{i}", "wb") as f:
                f.write(gen(r, r.randint(200, 6000)))
        size = rng.choice([4096, 16384, 32768, 65536, 112640])
        name = f"{tag}{k}.dict"
        dict_id = rng.randint(32768, 2**31 - 1)
        rc, _ = run([ZSTD, "-q", "--train", "-r", sd, f"--maxdict={size}", f"--dictID={dict_id}",
                     "-o", f"{OUT}/dicts/{name}"])
        if rc == 0:
            names.append((name, gen))
    return names


def cat_dict():
    dicts = train_dicts("t", 5)
    made = 0
    while made < n(160):
        name, gen = rng.choice(dicts)
        if rng.random() < 0.8:
            d = gen(random.Random(rng.getrandbits(64)), int(2 ** rng.uniform(6, 13)))
        else:
            d = gen(random.Random(rng.getrandbits(64)), int(2 ** rng.uniform(13, 19)))
        opts = [f"-{rng.randint(1, 19)}", "-D", f"{OUT}/dicts/{name}"]
        note = f"{opts[0]} -D {name}"
        if rng.random() < 0.2:
            opts.append("--no-dictID")
            note += " --no-dictID"
        if rng.random() < 0.2:
            opts.append("--no-check")
        frame = compress(d, opts)
        if frame and add("dict", frame, True, d, dict_name=name, note=note):
            made += 1
    return dicts


def mutate(r, base):
    b = bytearray(base)
    for _ in range(r.randint(1, 40)):
        op = r.random()
        pos = r.randint(0, len(b))
        if op < 0.4:
            b[pos:pos] = r.randbytes(r.randint(1, 500))
        elif op < 0.7:
            del b[pos:pos + r.randint(1, 2000)]
        else:
            seg = r.randbytes(r.randint(1, 200))
            b[pos:pos + len(seg)] = seg
    return bytes(b)


def cat_rawdict():
    made = 0
    while made < n(60):
        r = random.Random(rng.getrandbits(64))
        old = content(int(2 ** rng.uniform(13, 19)), rng.choice([text, source, json_logs, records, csv]))
        name = f"raw{made}.bin"
        with open(f"{OUT}/dicts/{name}", "wb") as f:
            f.write(old)
        if rng.random() < 0.7:
            new = mutate(r, old)
            opts = [f"--patch-from={OUT}/dicts/{name}", f"-{rng.randint(1, 19)}"]
        else:
            new = content(int(2 ** rng.uniform(8, 16)), rng.choice([text, source, json_logs]))
            opts = ["-D", f"{OUT}/dicts/{name}", f"-{rng.randint(1, 19)}"]
        frame = compress(new, opts)
        if frame and add("rawdict", frame, True, new, dict_name=name, note=" ".join(opts).replace(OUT + "/dicts/", "")):
            made += 1
        else:
            os.remove(f"{OUT}/dicts/{name}")


def skippable(r):
    payload = r.randbytes(r.choice([0, 1, 7, r.randint(0, 3000)]))
    return struct.pack("<II", 0x184D2A50 | r.randint(0, 15), len(payload)) + payload


def cat_multiframe():
    for _ in range(n(90)):
        r = random.Random(rng.getrandbits(64))
        parts, original = [], b""
        k = r.randint(2, 6)
        for _ in range(k):
            if r.random() < 0.25:
                parts.append(skippable(r))
                continue
            d = content(int(2 ** rng.uniform(0, 18)))
            opts = [f"-{r.randint(1, 19)}"]
            if r.random() < 0.3:
                opts.append("--no-check")
            parts.append(compress(d, opts, stdin=r.random() < 0.3))
            original += d
        if r.random() < 0.1:
            parts = [skippable(r)]
            original = b""
        add("multiframe", b"".join(parts), True, original, note=f"{len(parts)} parts")


def cat_stream():
    variants = [["-C"], ["--no-check"], ["-T4"], ["--rsyncable", "-T2"], ["--no-content-size"],
                ["-B65536", "-T2"], ["--target-compressed-block-size=1024"],
                ["--target-compressed-block-size=8192"], ["--stream-size=0"]]
    made = 0
    while made < n(110):
        v = list(rng.choice(variants))
        stdin = rng.random() < 0.7
        d = content()
        if v == ["--stream-size=0"]:
            v = [f"--stream-size={len(d)}"]
            stdin = True
        opts = [f"-{rng.randint(1, 19)}"] + v
        frame = compress(d, opts, stdin=stdin)
        if frame is None:
            continue
        if add("stream", frame, True, d, note=("stdin " if stdin else "") + " ".join(opts)):
            made += 1


def raw_frame(r, data, window_log=None, mantissa=0, single=False, fcs_size=None, checksum=None,
              unused_bit=False, fcs_value=None, max_block=None):
    """Hand-assemble a frame from Raw and RLE blocks."""
    if checksum is None:
        checksum = r.random() < 0.5
    if single:
        window = len(data)
    else:
        base = 1 << window_log
        window = base + (base >> 3) * mantissa
    bmax = max_block or min(window, 128 * 1024)
    if fcs_size is None:
        fcs_size = 1 if single and len(data) < 256 else (2 if 256 <= len(data) < 65792 else 4)
        if not single and r.random() < 0.3:
            fcs_size = 0
    flag = {0: 0, 1: 0, 2: 1, 4: 2, 8: 3}[fcs_size]
    fhd = (flag << 6) | (int(single) << 5) | (int(unused_bit) << 4) | (int(checksum) << 2)
    out = bytearray(struct.pack("<I", 0xFD2FB528))
    out.append(fhd)
    if not single:
        out.append(((window_log - 10) << 3) | mantissa)
    v = len(data) if fcs_value is None else fcs_value
    if fcs_size == 1:
        out += struct.pack("<B", v)
    elif fcs_size == 2:
        out += struct.pack("<H", v - 256)
    elif fcs_size == 4:
        out += struct.pack("<I", v)
    elif fcs_size == 8:
        out += struct.pack("<Q", v)
    pos = 0
    blocks = []
    while True:
        size = min(len(data) - pos, r.randint(1, bmax) if bmax > 0 else 0)
        chunk = data[pos:pos + size]
        pos += size
        blocks.append(chunk)
        if pos >= len(data):
            break
    for i, chunk in enumerate(blocks):
        last = int(i == len(blocks) - 1)
        if chunk and chunk == bytes([chunk[0]]) * len(chunk):
            out += struct.pack("<I", last | (1 << 1) | (len(chunk) << 3))[:3] + chunk[:1]
        else:
            out += struct.pack("<I", last | (len(chunk) << 3))[:3] + chunk
    if checksum:
        out += struct.pack("<Q", xxh64(data))[:4]
    return bytes(out)


def xxh64(data, seed=0):
    M = (1 << 64) - 1
    P1, P2, P3, P4, P5 = (11400714785074694791, 14029467366897019727, 1609587929392839161,
                          9650029242287828579, 2870177450012600261)
    rotl = lambda x, k: ((x << k) | (x >> (64 - k))) & M
    def rnd(acc, inp):
        return (rotl((acc + inp * P2) & M, 31) * P1) & M
    def merge(acc, val):
        return ((acc ^ rnd(0, val)) * P1 + P4) & M
    i, L = 0, len(data)
    if L >= 32:
        v = [(seed + P1 + P2) & M, (seed + P2) & M, seed, (seed - P1) & M]
        while i + 32 <= L:
            for j in range(4):
                v[j] = rnd(v[j], int.from_bytes(data[i + 8 * j:i + 8 * j + 8], "little"))
            i += 32
        h = (rotl(v[0], 1) + rotl(v[1], 7) + rotl(v[2], 12) + rotl(v[3], 18)) & M
        for j in range(4):
            h = merge(h, v[j])
    else:
        h = (seed + P5) & M
    h = (h + L) & M
    while i + 8 <= L:
        h ^= rnd(0, int.from_bytes(data[i:i + 8], "little"))
        h = (rotl(h, 27) * P1 + P4) & M
        i += 8
    if i + 4 <= L:
        h ^= (int.from_bytes(data[i:i + 4], "little") * P1) & M
        h = (rotl(h, 23) * P2 + P3) & M
        i += 4
    while i < L:
        h ^= (data[i] * P5) & M
        h = (rotl(h, 11) * P1) & M
        i += 1
    h ^= h >> 33
    h = (h * P2) & M
    h ^= h >> 29
    h = (h * P3) & M
    h ^= h >> 32
    return h


def cat_edge():
    sizes = [0, 1, 2, 3, 7, 127, 255, 256, 257, 1023, 1024, 65535, 65791, 65792, 131071,
             131072, 131073, 262143, 262144, 393217]
    for s in sizes:
        for _ in range(n(3)):
            d = content(s)
            opts = [f"-{rng.randint(1, 19)}"]
            if rng.random() < 0.4:
                opts.append("--no-check")
            add("edge", compress(d, opts, stdin=rng.random() < 0.3), True, d, note=f"size={s} " + " ".join(opts))
    for _ in range(n(40)):
        r = random.Random(rng.getrandbits(64))
        d = content(int(2 ** rng.uniform(0, 18)), rng.choice([runs, noise, text]))
        single = r.random() < 0.3
        wl = r.randint(10, 22)
        if not single and len(d) > (1 << wl):
            wl = min(27, max(wl, len(d).bit_length()))
        add("edge", raw_frame(r, d, window_log=wl, mantissa=r.randint(0, 7), single=single,
                              unused_bit=r.random() < 0.5), True, d, note="hand-built raw/RLE frame")
    for _ in range(n(15)):
        d = content()
        f = bytearray(compress(d, [f"-{rng.randint(1, 19)}"]))
        f[4] |= 0x10
        add("edge", bytes(f), True, d, note="unused header bit set")
    for _ in range(n(8)):
        r = random.Random(rng.getrandbits(64))
        d = content(int(2 ** rng.uniform(0, 16)))
        add("edge", raw_frame(r, d, window_log=17, fcs_size=8), True, d, note="8-byte FCS field")


def cat_errors(dicts):
    def base_frame(checksum=True):
        d = content(int(2 ** rng.uniform(6, 18)))
        opts = [f"-{rng.randint(1, 19)}"] + ([] if checksum else ["--no-check"])
        return d, compress(d, opts, stdin=rng.random() < 0.3)

    for _ in range(n(70)):
        d, f = base_frame(checksum=rng.random() < 0.6)
        cut = rng.randint(1, len(f) - 1)
        add("errors", f[:cut], False, note=f"truncated at {cut}/{len(f)}")
    made = 0
    while made < n(50):
        d, f = base_frame(True)
        hdr = 6
        pos = rng.randint(hdr, len(f) - 1)
        g = bytearray(f)
        g[pos] ^= 1 << rng.randint(0, 7)
        g = bytes(g)
        in_checksum = pos >= len(f) - 4
        if not in_checksum:
            rc, out = zstd_decode(g, no_check=True)
            if rc != 0 or out == d:
                continue
        if add("errors", g, False, note=f"bit flip at {pos} -> checksum mismatch"):
            made += 1
    for _ in range(n(10)):
        d, f = base_frame(rng.random() < 0.5)
        g = bytearray(f)
        g[4] |= 0x08
        add("errors", bytes(g), False, note="reserved header bit set")
    for _ in range(n(10)):
        r = random.Random(rng.getrandbits(64))
        d = content(int(2 ** rng.uniform(4, 15)), noise)
        f = bytearray(raw_frame(r, d, window_log=17, checksum=False, fcs_size=0))
        f[6] = (f[6] & 0xF9) | 0x06
        add("errors", bytes(f), False, note="reserved block type")
    for _ in range(n(10)):
        d, f = base_frame()
        bad = rng.getrandbits(32)
        if bad == 0xFD2FB528 or (bad & 0xFFFFFFF0) == 0x184D2A50 or (bad & 0xFFFFFFF0) == 0xFD2FB520:
            continue
        add("errors", struct.pack("<I", bad) + f[4:], False, note="bad magic")
    for _ in range(n(15)):
        d, f = base_frame()
        tail = rng.randbytes(rng.choice([1, 2, 3, rng.randint(4, 64)]))
        if len(tail) >= 4:
            m = int.from_bytes(tail[:4], "little")
            if m == 0xFD2FB528 or (m & 0xFFFFFFF0) in (0x184D2A50, 0xFD2FB520):
                continue
        add("errors", f + tail, False, note="trailing garbage")
    for wl in (28, 29, 30, 31):
        d = content(5000)
        add("errors", compress(d, [f"--long={wl}", "-3"], stdin=True), False, note=f"window 2^{wl}")
    for _ in range(n(6)):
        r = random.Random(rng.getrandbits(64))
        d = content(3000, noise)
        add("errors", raw_frame(r, d, window_log=27, mantissa=r.randint(1, 7)), False,
            note="window above 2^27 via mantissa")
    for _ in range(n(15)):
        r = random.Random(rng.getrandbits(64))
        d = content(int(2 ** rng.uniform(9, 17)), rng.choice([noise, runs]))
        delta = r.choice([-3, -1, 1, 2, 100])
        add("errors", raw_frame(r, d, window_log=17, fcs_size=4, fcs_value=max(0, len(d) + delta)), False,
            note=f"content size off by {delta}")
    for _ in range(n(6)):
        r = random.Random(rng.getrandbits(64))
        d = content(int(2 ** rng.uniform(17.2, 18)), noise)
        add("errors", raw_frame(r, d, window_log=20, fcs_size=4, max_block=len(d)), False,
            note="raw block above 128 KiB")
    for _ in range(n(6)):
        r = random.Random(rng.getrandbits(64))
        d = content(r.randint(2000, 8000), noise)
        add("errors", raw_frame(r, d, window_log=10, fcs_size=4, max_block=len(d)), False,
            note="raw block above window size")
    for _ in range(n(5)):
        name, gen = rng.choice(dicts)
        d = gen(random.Random(rng.getrandbits(64)), 3000)
        f = compress(d, ["-3", "-D", f"{OUT}/dicts/{name}"])
        add("errors", f, False, note="dictionary required but not given")
    for _ in range(n(5)):
        (a, gen), (b, _) = rng.sample(dicts, 2)
        d = gen(random.Random(rng.getrandbits(64)), 3000)
        f = compress(d, ["-3", "-D", f"{OUT}/dicts/{a}"])
        add("errors", f, False, dict_name=b, note=f"compressed with {a}, given {b}")


cat_levels()
cat_fast()
cat_params()
cat_long()
dicts = cat_dict()
cat_rawdict()
cat_multiframe()
cat_stream()
cat_edge()
cat_errors(dicts)
with open(f"{OUT}/manifest.json", "w") as f:
    json.dump({"cases": manifest}, f, indent=0)
print(json.dumps(stats, indent=1))
