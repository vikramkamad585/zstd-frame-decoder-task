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
import stat
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
TREE_LIMIT = 256 << 20
TREE_ENTRIES = 100_000
READELF_KEEP = 64 << 20
SKIP_DIRS = {"target", ".git"}


def copy_source(src, dst):
    """Copy the package tree, skipping target/ and .git/ at any depth.
    Returns an error string for symlinks, special files or an oversized
    tree; never follows links and never reads past a file's own size."""
    if os.path.islink(src):
        return f"{src} is a symlink"
    total = 0
    entries = 0
    for dirpath, dirnames, filenames in os.walk(src):
        rel_dir = os.path.relpath(dirpath, src)
        entries += len(dirnames) + len(filenames)
        if entries > TREE_ENTRIES:
            return f"source tree has more than {TREE_ENTRIES} entries"
        os.makedirs(os.path.join(dst, rel_dir), exist_ok=True)
        for d in list(dirnames):
            if d in SKIP_DIRS:
                dirnames.remove(d)
            elif os.path.islink(os.path.join(dirpath, d)):
                return f"{os.path.normpath(os.path.join(rel_dir, d))} is a symlink"
        for fn in filenames:
            path = os.path.join(dirpath, fn)
            rel = os.path.normpath(os.path.join(rel_dir, fn))
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode):
                return f"{rel} is a symlink"
            if not stat.S_ISREG(st.st_mode):
                return f"{rel} is not a regular file"
            total += st.st_size
            if total > TREE_LIMIT:
                return "source tree exceeds 256 MiB"
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                with open(os.path.join(dst, rel), "wb") as out:
                    remaining = st.st_size
                    while remaining > 0:
                        chunk = os.read(fd, min(1 << 20, remaining))
                        if not chunk:
                            break
                        out.write(chunk)
                        remaining -= len(chunk)
            finally:
                os.close(fd)
    return ""


def check_tree(root):
    """Source-tree gates: std-only Cargo package, no build scripts, no
    linker configuration, no vendored ELF objects or C sources."""
    try:
        return _check_tree(root)
    except Exception as e:  # any surprise in the tree is the tree's fault
        return f"source tree could not be checked ({type(e).__name__}: {str(e)[:160]})"


def _check_tree(root):
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        for d in dirnames:
            if d == ".cargo":
                return f"cargo configuration directory {os.path.normpath(os.path.join(rel_dir, d))} is not allowed"
        for fn in filenames:
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, root)
            if fn == "build.rs":
                return f"build script {rel} is not allowed"
            if fn.endswith((".c", ".h", ".cc", ".cpp", ".o", ".a", ".so")) or ".so." in fn:
                return f"non-Rust object or source {rel} is not allowed"
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode):
                return f"{rel} is not a regular file"
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                if os.read(fd, 4) == b"\x7fELF":
                    return f"ELF file {rel} is not allowed"
            finally:
                os.close(fd)
            if fn == "Cargo.toml":
                if st.st_size > 1 << 20:
                    return f"{rel}: manifest too large"
                with open(path, "rb") as f:
                    text = f.read().decode("utf-8", errors="replace")
                err = check_manifest(text, dirpath, root)
                if err:
                    return f"{rel}: {err}"
    return ""


def _table(doc, key):
    v = doc.get(key, {})
    if not isinstance(v, dict):
        raise ValueError(f"'{key}' must be a table")
    return v


def check_manifest(text, manifest_dir, root):
    """Returns an error string, or "" when the manifest is acceptable.
    Never raises: malformed or unsupported manifests are gate failures."""
    import tomllib
    try:
        doc = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError) as e:
        return f"unparsable manifest ({str(e)[:160]})"
    try:
        pkg = _table(doc, "package")
        if "build" in pkg and pkg["build"] is not False:
            return "package.build is not allowed"
        if "links" in pkg:
            return "package.links is not allowed"
        lib = _table(doc, "lib")
        if lib.get("proc-macro") or lib.get("proc_macro"):
            return "proc-macro crates are not allowed"
        if "patch" in doc or "replace" in doc:
            return "[patch]/[replace] is not allowed"
        tables = [_table(doc, "dependencies"), _table(doc, "build-dependencies"),
                  _table(doc, "dev-dependencies"), _table(doc, "dev_dependencies"),
                  _table(doc, "build_dependencies")]
        for t in _table(doc, "target").values():
            if not isinstance(t, dict):
                raise ValueError("target entries must be tables")
            tables += [_table(t, k) for k in ("dependencies", "build-dependencies",
                                               "dev-dependencies", "dev_dependencies",
                                               "build_dependencies")]
        tables.append(_table(_table(doc, "workspace"), "dependencies"))
        real_root = os.path.realpath(root)
        for t in tables:
            for name, spec in t.items():
                if isinstance(spec, dict) and spec.get("workspace") is True \
                        and not ({"git", "registry", "version", "path"} & set(spec)):
                    continue  # resolved through [workspace.dependencies], checked as well
                if not isinstance(spec, dict) or not isinstance(spec.get("path"), str):
                    return f"dependency '{name}' is not a path dependency inside the package"
                if {"git", "registry", "registry-index", "branch", "tag", "rev"} & set(spec):
                    return f"dependency '{name}' names a non-path source"
                target = os.path.realpath(os.path.join(manifest_dir, spec["path"]))
                if target != real_root and not target.startswith(real_root + os.sep):
                    return f"dependency '{name}' points outside the package"
    except (ValueError, TypeError, AttributeError) as e:
        return f"unsupported manifest structure ({str(e)[:160]})"
    return ""


def readelf(*args):
    rc, out = run_limited(["readelf", *args], 60, None)
    if len(out) > READELF_KEEP:
        return None, "readelf output is too large to inspect"
    if rc != 0:
        return None, f"readelf failed: {out[-200:]}"
    return out, ""


def check_binary(path):
    out, err = readelf("-d", "-W", path)
    if err:
        return err
    needed = [line.split("[", 1)[1].rstrip("]") for line in out.splitlines() if "(NEEDED)" in line]
    if "(NEEDED)" not in out and "Dynamic section" not in out:
        return "binary is not dynamically linked against the system C library"
    for lib in needed:
        if lib not in NEEDED_OK and not lib.startswith("ld-linux"):
            return f"binary links {lib}"
    out, err = readelf("--dyn-syms", "-W", path)
    if err:
        return err
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
    err = copy_source(SRC, f"{work}/src") or check_tree(f"{work}/src")
    if err:
        print(f"GATE FAILED (source): {err}  -> score 0")
        return 1
    env = {"PATH": CLEAN_PATH, "HOME": f"{work}/home", "CARGO_HOME": f"{work}/cargo-home",
           "RUSTUP_HOME": "/usr/local/rustup", "CARGO_TARGET_DIR": f"{work}/target",
           "CARGO_NET_OFFLINE": "true", "LC_ALL": "C"}
    rc, out = run_limited(["rustup", "default"], 60, None, cwd="/", env=dict(env, CARGO_HOME="/usr/local/cargo"))
    if rc == 0 and out.split():
        env["RUSTUP_TOOLCHAIN"] = out.split()[0]  # grading ignores rust-toolchain files too
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
