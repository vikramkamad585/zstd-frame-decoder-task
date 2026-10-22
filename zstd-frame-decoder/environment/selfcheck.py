#!/usr/bin/env python3
"""Local self-check for /app/zstdr.

Builds the decoder the same way grading does (clean environment, offline,
fresh cargo home, release profile), applies the same source and linkage
gates, decodes every case under /app/samples and prints per-category pass
rates plus the score the published formula gives on these samples.

    python3 /app/selfcheck.py            # summary
    python3 /app/selfcheck.py -v         # also list every failing case
    python3 /app/selfcheck.py -k dict    # only categories containing "dict"
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

SRC = "/app/zstdr"
SAMPLES = "/app/samples"
CASE_TIMEOUT = 20
NEEDED_OK = ("libc.so.6", "libm.so.6", "libgcc_s.so.1", "libpthread.so.0", "librt.so.1")
FORBIDDEN_IMPORTS = {
    "dlopen", "dlmopen", "execve", "execv", "execvp", "execvpe", "execl",
    "execlp", "execle", "fexecve", "posix_spawn", "posix_spawnp", "fork", "vfork", "system", "popen",
}
CLEAN_PATH = "/usr/local/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
SUCCESS_CATEGORIES = ["levels", "fast", "params", "long", "dict", "rawdict",
                      "multiframe", "stream", "edge"]


def run_limited(args, timeout, preexec, cwd=None, env=None):
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=cwd,
                         env=env, start_new_session=True)
    try:
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, out.decode(errors="replace")
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, 9)
        out, _ = p.communicate()
        return None, out.decode(errors="replace")


# ------------------------------------------------------------------ gates
def check_tree(root):
    """Source-tree gates: std-only Cargo package, no build scripts, no
    linker configuration, no vendored ELF objects or C sources."""
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        if rel == ".cargo" or rel.startswith(".cargo" + os.sep):
            return "a .cargo configuration directory is not allowed"
        for fn in filenames:
            path = os.path.join(dirpath, fn)
            if fn == "build.rs":
                return f"build script {os.path.relpath(path, root)} is not allowed"
            if fn.endswith((".c", ".h", ".cc", ".cpp", ".o", ".a", ".so")) or ".so." in fn:
                return f"non-Rust object or source {os.path.relpath(path, root)} is not allowed"
            try:
                with open(path, "rb") as f:
                    if f.read(4) == b"\x7fELF":
                        return f"ELF file {os.path.relpath(path, root)} is not allowed"
            except OSError:
                pass
            if fn == "Cargo.toml":
                text = open(path, encoding="utf-8", errors="replace").read()
                err = check_manifest(text)
                if err:
                    return f"{os.path.relpath(path, root)}: {err}"
    return ""


def check_manifest(text):
    import tomllib
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return f"unparsable manifest ({e})"
    pkg = doc.get("package", {})
    if "build" in pkg and pkg["build"] is not False:
        return "package.build is not allowed"
    if "links" in pkg:
        return "package.links is not allowed"
    lib = doc.get("lib", {})
    if lib.get("proc-macro"):
        return "proc-macro crates are not allowed"
    tables = [doc.get("dependencies", {}), doc.get("build-dependencies", {}),
              doc.get("dev-dependencies", {})]
    for t in doc.get("target", {}).values():
        tables += [t.get("dependencies", {}), t.get("build-dependencies", {})]
    tables.append(doc.get("workspace", {}).get("dependencies", {}))
    for t in tables:
        for name, spec in t.items():
            if not isinstance(spec, dict) or "path" not in spec:
                return f"dependency '{name}' is not a path dependency inside the package"
            if os.path.isabs(spec["path"]) or ".." in spec["path"].split("/"):
                return f"dependency '{name}' points outside the package"
    if "patch" in doc or "replace" in doc:
        return "[patch]/[replace] is not allowed"
    return ""


def check_binary(path):
    rc, out = run_limited(["readelf", "-d", "-W", path], 60, None)
    if rc != 0:
        return f"readelf failed: {out[-200:]}"
    needed = [line.split("[", 1)[1].rstrip("]") for line in out.splitlines() if "(NEEDED)" in line]
    if "(NEEDED)" not in out and "Dynamic section" not in out:
        return "binary is not dynamically linked against the system C library"
    for lib in needed:
        if lib not in NEEDED_OK and not lib.startswith("ld-linux"):
            return f"binary links {lib}"
    rc, out = run_limited(["readelf", "--dyn-syms", "-W", path], 60, None)
    if rc != 0:
        return f"readelf failed: {out[-200:]}"
    for line in out.splitlines():
        cols = line.split()
        if len(cols) >= 8 and cols[6] == "UND":
            sym = cols[7].split("@")[0]
            if sym in FORBIDDEN_IMPORTS:
                return f"binary imports {sym}"
    return ""



def run_case(binary, case, out_dir):
    out_path = f"{out_dir}/{case['id']}.out"
    args = [binary]
    if case["dict"]:
        args += ["-D", f"{SAMPLES}/dicts/{case['dict']}"]
    args += [f"{SAMPLES}/cases/{case['id']}.zst", out_path]
    t0 = time.monotonic()
    rc, log = run_limited(args, CASE_TIMEOUT, None, cwd=out_dir,
                          env={"PATH": "/nonexistent", "HOME": out_dir, "LC_ALL": "C"})
    secs = time.monotonic() - t0
    if rc is None:
        ok, why = False, "timeout"
    elif case["expect"] == "error":
        ok, why = rc == 1, f"expected rejection (exit 1), got exit {rc}"
    elif rc != 0:
        ok, why = False, f"exit {rc}: {log.strip()[-160:]}"
    else:
        try:
            data = open(out_path, "rb").read()
            ok = hashlib.sha256(data).hexdigest() == case["sha256"] and len(data) == case["size"]
            why = "match" if ok else f"output differs (size {len(data)}, expected {case['size']})"
        except OSError as e:
            ok, why = False, f"no output: {e}"
    return case, ok, why, secs


def main():
    verbose = "-v" in sys.argv
    only = sys.argv[sys.argv.index("-k") + 1] if "-k" in sys.argv else None
    work = tempfile.mkdtemp(prefix="zselfcheck-")
    shutil.copytree(SRC, f"{work}/src", symlinks=False,
                    ignore=shutil.ignore_patterns("target", ".git"), ignore_dangling_symlinks=True)
    err = check_tree(f"{work}/src")
    if err:
        print(f"GATE FAILED (source): {err}  -> score 0")
        return 1
    env = {"PATH": CLEAN_PATH, "HOME": f"{work}/home", "CARGO_HOME": f"{work}/cargo-home",
           "RUSTUP_HOME": "/usr/local/rustup", "CARGO_TARGET_DIR": f"{work}/target",
           "CARGO_NET_OFFLINE": "true", "LC_ALL": "C"}
    os.makedirs(env["HOME"])
    rc, log = run_limited(["cargo", "build", "--release", "--offline", "--bin", "zstdr"], 1200,
                          None, cwd=f"{work}/src", env=env)
    binary = f"{work}/target/release/zstdr"
    if rc != 0 or not os.path.isfile(binary):
        print(log[-3000:])
        print("GATE FAILED (build)  -> score 0")
        return 1
    err = check_binary(binary)
    if err:
        print(f"GATE FAILED (linkage): {err}  -> score 0")
        return 1
    print("gates: source ok, build ok, linkage ok")

    cases = json.load(open(f"{SAMPLES}/manifest.json"))["cases"]
    if only:
        cases = [c for c in cases if only in c["category"]]
    out_dir = f"{work}/out"
    os.makedirs(out_dir)
    per, fails, slow = {}, [], 0.0
    with ThreadPoolExecutor(8) as ex:
        for case, ok, why, secs in ex.map(lambda c: run_case(binary, c, out_dir), cases):
            a = per.setdefault(case["category"], [0, 0])
            a[0] += ok
            a[1] += 1
            slow = max(slow, secs)
            if not ok:
                fails.append((case, why))
    print(f"{'category':12s} {'pass':>9s}")
    for c in SUCCESS_CATEGORIES + ["errors"]:
        if c in per:
            print(f"{c:12s} {per[c][0]:4d}/{per[c][1]:<4d}")
    rate = lambda c: per[c][0] / per[c][1] if c in per else 0.0
    if not only:
        S = sum(rate(c) for c in SUCCESS_CATEGORIES) / len(SUCCESS_CATEGORIES)
        E = rate("errors")
        print(f"S = {S:.4f}  E = {E:.4f}  score on samples = S * (0.8 + 0.2 * E) = {S * (0.8 + 0.2 * E):.4f}")
    print(f"slowest case: {slow:.2f}s (limit {CASE_TIMEOUT}s)")
    if verbose:
        for case, why in fails:
            print(f"FAIL {case['id']} [{case['category']}] {case['note']}: {why}")
    elif fails:
        print(f"{len(fails)} failing cases; rerun with -v to list them")
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
