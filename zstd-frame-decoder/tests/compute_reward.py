#!/usr/bin/env python3
"""Every scoring decision: evidence.json -> reward.txt, reward.json, details.json."""
import json
from pathlib import Path

OUT = Path("/logs/verifier")
SUCCESS_CATEGORIES = ["levels", "fast", "params", "long", "dict", "rawdict",
                      "multiframe", "stream", "edge"]
REJECT_CATEGORY = "errors"
# The reference decoder passes every case: S = 1, E = 1.
REFERENCE_RAW = 1.0


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
    raw = S * (0.8 + 0.2 * E)
    reward = raw / REFERENCE_RAW
    passed = sum(1 for r in results if r["pass"])
    subs = {**gates, "build": 1.0, "S": S, "E": E,
            **{f"pass_{c}": v for c, v in rates.items()},
            "cases_passed": float(passed), "cases_total": float(total),
            "raw_rle_only_passed": float(plain[0]), "raw_rle_only_total": float(plain[1])}
    worst = sorted(((v, c) for c, v in rates.items()))[:3]
    reason = (f"{passed}/{total} cases; S={S:.4f} E={E:.4f}; weakest: "
              + ", ".join(f"{c} {v:.2f}" for v, c in worst))
    emit(reward, 1, reason, subs)


if __name__ == "__main__":
    main()
