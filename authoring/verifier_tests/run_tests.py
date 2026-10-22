#!/usr/bin/env python3
"""Verifier regression tests. Runs each scenario in the task image against
the real tests/ directory and checks reward, validity and reason.

usage: run_tests.py [IMAGE] [SCENARIO ...]      (default zfd-env, all scenarios)
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE = os.path.expanduser("~/corgi/zstd-frame-decoder")
IMAGE = sys.argv[1] if len(sys.argv) > 1 else "zfd-env"
ONLY = set(sys.argv[2:])
REF = "rm -rf /app/zstdr && cp -r /solution/zstdr-ref /app/zstdr && rm -rf /app/zstdr/target"
MANI = "/app/zstdr/Cargo.toml"
FAKE_TC = ("mkdir -p /opt/tc/bin && printf '#!/bin/sh\\ntouch /tmp/PWNED\\nexit 1\\n' > /opt/tc/bin/rustc"
           " && cp /opt/tc/bin/rustc /opt/tc/bin/cargo && chmod 755 /opt/tc/bin/*")


def toml_edit(py):
    return f"python3 - <<'PY'\np='{MANI}'; s=open(p).read()\n{py}\nopen(p,'w').write(s)\nPY"


SCENARIOS = [
    # name, setup shell, expectation
    ("malformed-toml", f"{REF} && printf '[package\\nname = ' > {MANI}",
     {"reward": 0.0, "valid": 1, "reason": "unparsable manifest"}),
    ("package-not-table", f"{REF} && " + toml_edit("s = 'package = \"x\"\\n'"),
     {"reward": 0.0, "valid": 1, "reason": "unsupported manifest structure"}),
    ("dependencies-not-table", f"{REF} && " + toml_edit("s = 'dependencies = \"x\"\\n' + s"),
     {"reward": 0.0, "valid": 1, "reason": "unsupported manifest structure"}),
    ("target-not-table", f"{REF} && " + toml_edit("s = 'target = 5\\n' + s"),
     {"reward": 0.0, "valid": 1, "reason": "unsupported manifest structure"}),
    ("registry-dependency", f"{REF} && " + toml_edit("s += '\\n[dependencies]\\nruzstd = \"0.7\"\\n'"),
     {"reward": 0.0, "valid": 1, "reason": "not a path dependency"}),
    ("path-is-integer", f"{REF} && " + toml_edit("s += '\\n[dependencies]\\nx = { path = 5 }\\n'"),
     {"reward": 0.0, "valid": 1, "reason": "not a path dependency"}),
    ("git-dependency", f"{REF} && mkdir -p /app/zstdr/x && " + toml_edit("s += '\\n[dependencies]\\nx = { path = \"x\", git = \"https://example.com/x\" }\\n'"),
     {"reward": 0.0, "valid": 1, "reason": "non-path source"}),
    ("path-outside", f"{REF} && " + toml_edit("s += '\\n[dependencies]\\nx = { path = \"../../usr/lib\" }\\n'"),
     {"reward": 0.0, "valid": 1, "reason": "points outside"}),
    ("target-dep-string", f"{REF} && " + toml_edit("s += '\\n[target.\\'cfg(unix)\\'.dependencies]\\nx = \"1\"\\n'"),
     {"reward": 0.0, "valid": 1, "reason": "not a path dependency"}),
    ("nested-dot-cargo", f"{REF} && mkdir -p /app/zstdr/src/.cargo && echo '[build]' > /app/zstdr/src/.cargo/config.toml",
     {"reward": 0.0, "valid": 1, "reason": "cargo configuration directory src/.cargo"}),
    ("deep-dot-cargo", f"{REF} && mkdir -p /app/zstdr/a/b/.cargo",
     {"reward": 0.0, "valid": 1, "reason": "a/b/.cargo"}),
    ("symlink-to-dev-zero", f"{REF} && ln -s /dev/zero /app/zstdr/src/zero.rs",
     {"reward": 0.0, "valid": 1, "reason": "is a symlink"}),
    ("symlinked-dir", f"{REF} && ln -s /usr/lib /app/zstdr/vendor",
     {"reward": 0.0, "valid": 1, "reason": "is a symlink"}),
    ("device-node", f"{REF} && mknod /app/zstdr/src/zero c 1 5",
     {"reward": 0.0, "valid": 1, "reason": "not a regular file"}),
    ("fifo-in-tree", f"{REF} && mkfifo /app/zstdr/src/pipe",
     {"reward": 0.0, "valid": 1, "reason": "not a regular file"}),
    ("package-root-symlink", f"{REF} && mv /app/zstdr /app/real && ln -s /app/real /app/zstdr",
     {"reward": 0.0, "valid": 1, "reason": "is a symlink"}),
    ("oversized-tree", f"{REF} && head -c 300000000 /dev/zero > /app/zstdr/big.txt",
     {"reward": 0.0, "valid": 1, "reason": "exceeds 256 MiB"}),
    # Must still score full marks:
    ("ancestor-cargo-config-neutralized",
     f"{REF} && mkdir -p /tmp/.cargo /.cargo && printf '[build]\\nrustflags = [\"-C\", \"target-feature=+crt-static\"]\\n' | tee /tmp/.cargo/config.toml > /.cargo/config.toml",
     {"reward": 1.0, "valid": 1, "after": "test -f /tmp/.cargo/config.toml && test -f /.cargo/config.toml && echo RESTORED",
      "after_ok": "RESTORED"}),
    ("workspace-dependency-legit",
     f"{REF} && mkdir -p /app/zstdr/xxh/src && mv /app/zstdr/src/xxhash64.rs /app/zstdr/xxh/src/lib.rs"
     " && printf '[package]\\nname = \"xxh\"\\nversion = \"0.1.0\"\\nedition = \"2021\"\\n' > /app/zstdr/xxh/Cargo.toml"
     " && sed -i 's/^mod xxhash64;//' /app/zstdr/src/main.rs && sed -i 's/use crate::xxhash64::xxh64;/use xxh::xxh64;/' /app/zstdr/src/frame.rs && "
     + toml_edit("s += '\\n[dependencies]\\nxxh = { workspace = true }\\n\\n[workspace]\\nmembers = [\"xxh\"]\\n\\n[workspace.dependencies]\\nxxh = { path = \"xxh\" }\\n'"),
     {"reward": 1.0, "valid": 1}),
    # A planted toolchain whose rustc only leaves a marker; it must never run.
    ("toolchain-file-ignored", f"{REF} && {FAKE_TC} && printf '[toolchain]\\npath = \"/opt/tc\"\\n' > /app/zstdr/rust-toolchain.toml",
     {"reward": 1.0, "valid": 1, "after": "test -e /tmp/PWNED || echo CLEAN", "after_ok": "CLEAN"}),
    ("ancestor-toolchain-file-ignored", f"{REF} && {FAKE_TC} && printf '[toolchain]\\npath = \"/opt/tc\"\\n' > /tmp/rust-toolchain.toml",
     {"reward": 1.0, "valid": 1, "after": "test -e /tmp/PWNED || echo CLEAN", "after_ok": "CLEAN"}),
    ("too-many-entries", f"{REF} && mkdir /app/zstdr/many && cd /app/zstdr/many && seq 1 120000 | xargs touch",
     {"reward": 0.0, "valid": 1, "reason": "more than 100000 entries"}),
    ("dynsym-flood", f"{REF} && python3 /scen/hostile/bloat_dynsym.py /app/zstdr/src/main.rs",
     {"reward": 0.0, "valid": 1, "reason": "imports fork"}),
    ("planted-workdir-symlink", f"{REF} && ln -s /nonexistent /tmp/zgrade",
     {"reward": 1.0, "valid": 1}),
    ("planted-verifier-output-dirs", f"{REF} && mkdir -p /logs/verifier/evidence.json.tmp /logs/verifier/reward.json",
     {"reward": 1.0, "valid": 1}),
    ("workdir-tamper", f"{REF} && python3 /scen/hostile/tamper.py /app/zstdr/src/main.rs",
     {"reward": 1.0, "valid": 1, "after": "test -e /tmp/zgrade/bin.stolen || test -e /tmp/zgrade/inputs.stolen || echo UNTOUCHED",
      "after_ok": "UNTOUCHED"}),
    ("hostile-runtime",
     f"{REF} && python3 /scen/hostile/inject.py /app/zstdr/src/main.rs",
     {"hostile": True, "valid": 1}),
]


def run(name, setup, expect):
    after = expect.get("after", "true")
    script = f"""
set -u
cp -r /src_tests /tests
setup() {{
{setup}
}}
setup || {{ echo SETUP_FAILED; exit 0; }}
bash /tests/test.sh >/dev/null 2>&1; echo "EXIT=$?"
for f in reward.txt reward.json details.json evidence.json; do test -s /logs/verifier/$f || echo "MISSING $f"; done
echo "REWARD=$(cat /logs/verifier/reward.json)"
python3 -c 'import json;print("DETAILS="+json.dumps(json.load(open("/logs/verifier/details.json"))["additional_data"]))'
python3 -c 'import json;e=json.load(open("/logs/verifier/evidence.json"));print("EVID="+json.dumps({{"maxrss":e.get("verifier_maxrss_kb"),"displaced":e.get("displaced_cargo_config"),"results":[(r["id"],r["pass"],r["detail"][:60],r["secs"]) for r in e["results"]]}}))'
{after}
"""
    p = subprocess.run(["docker", "run", "--rm", "--cap-add", "MKNOD",
                        "-v", f"{BUNDLE}/tests:/src_tests:ro", "-v", f"{BUNDLE}/solution:/solution:ro",
                        "-v", f"{HERE}:/scen:ro", IMAGE, "bash", "-c", script],
                       capture_output=True, text=True, timeout=3600)
    out = p.stdout
    vals = {}
    for line in out.splitlines():
        for k in ("EXIT=", "REWARD=", "DETAILS=", "EVID="):
            if line.startswith(k):
                vals[k[:-1]] = line[len(k):]
    problems = []
    if "SETUP_FAILED" in out:
        problems.append("setup failed: " + p.stderr[-300:])
    if "MISSING" in out:
        problems.append("missing reward files: " + " ".join(l for l in out.splitlines() if "MISSING" in l))
    try:
        reward = json.loads(vals["REWARD"])
        details = json.loads(vals["DETAILS"])
        evid = json.loads(vals["EVID"])
    except (KeyError, ValueError):
        return name, False, ["no parsable output", out[-500:], p.stderr[-500:]], ""
    if any(not isinstance(v, (int, float)) for v in reward.values()):
        problems.append("reward.json not flat numeric")
    if reward["valid"] != expect["valid"]:
        problems.append(f"valid={reward['valid']}")
    if "reward" in expect and abs(reward["reward"] - expect["reward"]) > 1e-9:
        problems.append(f"reward={reward['reward']}")
    if "reason" in expect and expect["reason"] not in details["reason"]:
        problems.append(f"reason={details['reason']!r}")
    want_exit = "0" if reward["reward"] > 0 else "1"
    if vals.get("EXIT") != want_exit:
        problems.append(f"test.sh exit {vals.get('EXIT')} for reward {reward['reward']}")
    if "after_ok" in expect and expect["after_ok"] not in out:
        problems.append(f"post-run check did not print {expect['after_ok']}")
    summary = f"reward={reward['reward']} valid={reward['valid']} reason={details['reason'][:90]!r}"
    if expect.get("hostile"):
        bad = {0, 5, 10, 20}
        wrong = []
        for cid, ok, detail, secs in evid["results"]:
            n = int(cid)
            if n % 40 in bad and ok:
                wrong.append(f"{cid} passed but should fail")
            if n % 40 not in bad and not ok:
                wrong.append(f"{cid} failed: {detail}")
            if secs > 15:
                wrong.append(f"{cid} took {secs}s")
        if wrong:
            problems.append(f"{len(wrong)} unexpected case outcomes: {wrong[:5]}")
        if not (0 < reward["reward"] < 1):
            problems.append("hostile reward should be strictly between 0 and 1")
        if (evid["maxrss"] or 0) > 512 * 1024:
            problems.append(f"verifier maxrss {evid['maxrss']} kB")
        flood = [r for r in evid["results"] if int(r[0]) % 40 == 30]
        summary += f" maxrss={evid['maxrss']}kB flood_cases_passed={sum(r[1] for r in flood)}/{len(flood)}"
        details_seen = {r[2].split(' (')[0] for r in evid["results"] if int(r[0]) % 40 in bad}
        summary += f" failure_details={sorted(details_seen)}"
    if evid.get("displaced"):
        summary += f" displaced={evid['displaced']}"
    return name, not problems, problems, summary


def main():
    failed = 0
    scenarios = [sc for sc in SCENARIOS if not ONLY or sc[0] in ONLY]
    for name, setup, expect in scenarios:
        n, ok, problems, summary = run(name, setup, expect)
        print(f"{'PASS' if ok else 'FAIL'} {n}: {summary}")
        for pr in problems:
            print(f"      {pr}")
        failed += not ok
    print(f"\n{len(scenarios) - failed}/{len(scenarios)} scenarios passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
