#!/usr/bin/env python3
"""Every scoring decision: evidence.json -> reward.txt, reward.json, details.json."""
import json
import math
from pathlib import Path

OUT = Path("/logs/verifier")
SUCCESS_CATEGORIES = ["levels", "fast", "params", "long", "dict", "rawdict",
                      "multiframe", "stream", "edge"]
REJECT_CATEGORY = "errors"
ENC_CATEGORIES = ["text", "logs", "source", "csv", "records", "dna", "real", "binary", "long"]
DECODER_WEIGHT, ENCODER_WEIGHT = 0.4, 0.6
# The reference passes every decoder case (D = 1) and matches its own frozen
# compressed sizes (C = 1).
REFERENCE_RAW = 1.0


def clamp(v):
    return min(1.0, max(0.0, v))


def enc_credit(r):
    """0.3 for closing the gap from stored to baseline size, 0.7 for closing
    the gap from baseline to reference size, both measured in log size."""
    if not r.get("ok") or not r.get("size"):
        return 0.0
    c, s, b, ref = r["size"], r["stored_size"], r["baseline_size"], r["reference_size"]
    if c <= ref:
        return 1.0
    first = clamp(math.log(s / c) / math.log(s / b))
    second = clamp(math.log(b / c) / math.log(b / ref))
    return 0.3 * first + 0.7 * second


def emit(reward, valid, reason, subscores):
    reward = round(min(1.0, max(0.0, reward)), 6)
    flat = {"reward": reward, "score": reward, "valid": int(valid)}
    flat.update({k: round(float(v), 6) for k, v in subscores.items()})
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "reward.json").write_text(json.dumps(flat))
    (OUT / "reward.txt").write_text(f"{reward}\n")
    (OUT / "details.json").write_text(json.dumps({
        "subscores": [{"name": k, "score": round(float(v), 6)} for k, v in subscores.items()],
        "additional_data": {"reason": reason},
    }, indent=1))
    raise SystemExit(0 if reward > 0 else 1)


def main():
    try:
        ev = json.loads((OUT / "evidence.json").read_text())
    except (OSError, ValueError) as e:
        emit(0.0, 0, f"verifier fault: evidence unavailable ({e})", {})
    if not ev.get("runtime_ok", False):
        emit(0.0, 0, "verifier fault: " + ev.get("runtime_error", "")[-500:], {})

    gates = {f"gate_{k}": float(v) for k, v in ev.get("gates", {}).items()}
    zero_cats = {f"pass_{c}": 0.0 for c in SUCCESS_CATEGORIES + [REJECT_CATEGORY]}
    zero_cats.update({f"enc_{c}": 0.0 for c in ENC_CATEGORIES})
    zero_cats.update({"D": 0.0, "C": 0.0})
    if ev.get("cheat_detected"):
        emit(0.0, 1, "anti-cheat gate failed: " + ev.get("gate_reason", ""),
             {**gates, "build": 1.0 if ev.get("build_ok") else 0.0, "S": 0.0, "E": 0.0, **zero_cats})
    if not ev.get("build_ok"):
        emit(0.0, 1, "build gate failed: " + ev.get("gate_reason", ""),
             {**gates, "build": 0.0, "S": 0.0, "E": 0.0, **zero_cats})

    # Fixed denominator: every manifest case counts; a missing result fails.
    total = int(ev.get("cases_total", 0))
    results = ev.get("results", [])
    if total <= 0 or len(results) != total:
        emit(0.0, 0, f"verifier fault: {len(results)} results for {total} cases", {})
    enc_total = int(ev.get("enc_cases_total", 0))
    enc_results = ev.get("enc_results", [])
    if enc_total <= 0 or len(enc_results) != enc_total:
        emit(0.0, 0, f"verifier fault: {len(enc_results)} encoder results for {enc_total} cases", {})
    # Success cases count only when a frame holds a Compressed block; Raw/RLE-only
    # frames need no entropy decoding and are reported but carry no weight.
    per, plain = {}, [0, 0]
    for r in results:
        if r["expect"] == "ok" and not r.get("compressed"):
            plain[0] += 1 if r["pass"] else 0
            plain[1] += 1
            continue
        a = per.setdefault(r["category"], [0, 0])
        a[0] += 1 if r["pass"] else 0
        a[1] += 1
    rates = {c: (per[c][0] / per[c][1] if c in per and per[c][1] else 0.0)
             for c in SUCCESS_CATEGORIES + [REJECT_CATEGORY]}
    S = sum(rates[c] for c in SUCCESS_CATEGORIES) / len(SUCCESS_CATEGORIES)
    E = rates[REJECT_CATEGORY]
    D = S * (0.8 + 0.2 * E)

    # Encoder: mean credit per category, categories weighed equally.
    enc = {c: [] for c in ENC_CATEGORIES}
    for r in enc_results:
        enc.setdefault(r["category"], []).append(enc_credit(r))
    enc_rates = {c: (sum(v) / len(v) if v else 0.0) for c, v in enc.items() if c in ENC_CATEGORIES}
    C = sum(enc_rates.values()) / len(ENC_CATEGORIES)
    raw = DECODER_WEIGHT * D + ENCODER_WEIGHT * C
    reward = raw / REFERENCE_RAW
    passed = sum(1 for r in results if r["pass"])
    roundtrips = sum(1 for r in enc_results if r.get("ok"))
    subs = {**gates, "build": 1.0, "D": D, "S": S, "E": E, "C": C,
            **{f"pass_{c}": v for c, v in rates.items()},
            **{f"enc_{c}": v for c, v in enc_rates.items()},
            "cases_passed": float(passed), "cases_total": float(total),
            "raw_rle_only_passed": float(plain[0]), "raw_rle_only_total": float(plain[1]),
            "enc_roundtrips": float(roundtrips), "enc_cases_total": float(enc_total)}
    worst = sorted(((v, c) for c, v in rates.items()))[:3]
    worst_enc = sorted(((v, c) for c, v in enc_rates.items()))[:3]
    reason = (f"decoder {passed}/{total} cases, S={S:.4f} E={E:.4f} D={D:.4f}; "
              f"encoder {roundtrips}/{enc_total} round-trips, C={C:.4f}; weakest: "
              + ", ".join(f"{c} {v:.2f}" for v, c in worst) + "; "
              + ", ".join(f"enc {c} {v:.2f}" for v, c in worst_enc))
    emit(reward, 1, reason, subs)


if __name__ == "__main__":
    main()
