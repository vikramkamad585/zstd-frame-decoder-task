"""Synthetic content generators shared by gen.py (decoder cases) and
enc_corpus.py (encoder cases)."""
import json
import random
import struct


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
