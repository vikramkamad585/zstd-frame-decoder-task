#!/usr/bin/env python3
"""Build the candidate decoder, run the gates, decode every case.

Writes /logs/verifier/evidence.json on every path. Makes no scoring
decisions; compute_reward.py turns the evidence into a reward.
"""
import glob
import hashlib
import json
import os
import resource
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

OUT = "/logs/verifier"
EVIDENCE = f"{OUT}/evidence.json"
TESTS = os.path.dirname(os.path.abspath(__file__))
CASES = f"{TESTS}/cases"
SRC = "/app/zstdr"
WORK = "/tmp/zgrade"
RUN_UID = 64011
RUN_GID = 64011
CASE_TIMEOUT = 20
BUILD_TIMEOUT = 1200
WORKERS = 8
LOG_KEEP = 64 * 1024          # bytes of child stdout/stderr retained
OUTPUT_LIMIT = 1 << 30        # matches RLIMIT_FSIZE for decoder runs

NEEDED_OK = ("libc.so.6", "libm.so.6", "libgcc_s.so.1", "libpthread.so.0", "librt.so.1")
FORBIDDEN_IMPORTS = {
    "dlopen", "dlmopen", "execve", "execv", "execvp", "execvpe", "execl",
    "execlp", "execle", "fexecve", "posix_spawn", "posix_spawnp", "fork", "vfork", "system", "popen",
}
CLEAN_PATH = "/usr/local/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

ev = {
    "runtime_ok": True,
    "runtime_error": "",
    "build_ok": False,
    "build_log_tail": "",
    "cheat_detected": False,
    "gate_reason": "",
    "gates": {},
    "cases_total": 0,
    "results": [],
}


def write_evidence():
    os.makedirs(OUT, exist_ok=True)
    tmp = EVIDENCE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(ev, f)
    os.replace(tmp, EVIDENCE)


def fail_gate(name, reason):
    ev["gates"][name] = 0
    ev["cheat_detected"] = ev["cheat_detected"] or name != "build"
    if not ev["gate_reason"]:
        ev["gate_reason"] = reason


def drop_privileges():
    os.setsid()
    os.setgroups([])
    os.setgid(RUN_GID)
    os.setuid(RUN_UID)
    for lim, val in ((resource.RLIMIT_AS, 4 << 30), (resource.RLIMIT_CPU, 60),
                     (resource.RLIMIT_FSIZE, 1 << 30), (resource.RLIMIT_CORE, 0)):
        try:
            resource.setrlimit(lim, (val, val))
        except (ValueError, OSError):
            pass


def drop_privileges_build():
    os.setsid()
    os.setgroups([])
    os.setgid(RUN_GID)
    os.setuid(RUN_UID)


def kill_group(p):
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def kill_run_user():
    """Kill anything still running as the unprivileged grading user."""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            if os.stat(f"/proc/{pid}").st_uid == RUN_UID:
                os.kill(int(pid), signal.SIGKILL)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            pass


def run_limited(args, timeout, preexec, cwd=None, env=None):
    """Run a child with a wall-clock limit, keeping only the last LOG_KEEP
    bytes of its combined stdout/stderr so a noisy child cannot exhaust
    verifier memory. Returns (returncode or None on timeout, log tail)."""
    p = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, cwd=cwd, env=env, preexec_fn=preexec,
                         start_new_session=preexec is None)
    tail = bytearray()
    lock = threading.Lock()

    def pump():
        while True:
            try:
                chunk = p.stdout.read1(65536)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            with lock:
                tail.extend(chunk)
                if len(tail) > LOG_KEEP:
                    del tail[:-LOG_KEEP]

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    timed_out = False
    try:
        p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_group(p)
        p.wait()
    # A descendant that escaped the process group may still hold the pipe;
    # never wait on it indefinitely.
    reader.join(5)
    if reader.is_alive():
        kill_group(p)
        reader.join(5)
    with lock:
        log = bytes(tail).decode(errors="replace")
    return (None if timed_out else p.returncode), log


def read_output(path, expected_size, expected_sha):
    """Hash a decoder output without following symlinks, without blocking on
    FIFOs or devices, and without reading more than the expected size."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY)
    except OSError as e:
        return False, f"no readable output ({e.strerror})"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return False, "output is not a regular file"
        if st.st_size != expected_size:
            return False, f"output differs (size {st.st_size}, expected {expected_size})"
        h = hashlib.sha256()
        remaining = expected_size
        while remaining > 0:
            chunk = os.read(fd, min(1 << 20, remaining))
            if not chunk:
                break
            h.update(chunk)
            remaining -= len(chunk)
        if remaining != 0 or os.read(fd, 1):
            return False, "output changed size while being read"
        if h.hexdigest() != expected_sha:
            return False, "output differs (same size, different content)"
        return True, "match"
    finally:
        os.close(fd)


# ------------------------------------------------------------------ gates
TREE_LIMIT = 256 << 20
SKIP_DIRS = {"target", ".git"}


def copy_source(src, dst):
    """Copy the package tree, skipping target/ and .git/ at any depth.
    Returns an error string for symlinks, special files or an oversized
    tree; never follows links and never reads past a file's own size."""
    if os.path.islink(src):
        return f"{src} is a symlink"
    total = 0
    for dirpath, dirnames, filenames in os.walk(src):
        rel_dir = os.path.relpath(dirpath, src)
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


# ------------------------------------------------------------------ cases
def hide_libzstd():
    hidden = []
    for p in set(glob.glob("/usr/lib/*/libzstd.so*") + glob.glob("/lib/*/libzstd.so*") +
                 glob.glob("/usr/local/lib/**/libzstd*", recursive=True)):
        real = os.path.realpath(p)
        if os.path.isfile(real) and real not in [h[0] for h in hidden]:
            hidden.append((real, stat.S_IMODE(os.stat(real).st_mode)))
            os.chmod(real, 0)
    return hidden


def run_case(binary, case, case_dir, out_dir):
    out_path = f"{out_dir}/{case['id']}.out"
    args = [binary]
    if case["dict"]:
        args += ["-D", f"{case_dir}/dicts/{case['dict']}"]
    args += [f"{case_dir}/cases/{case['id']}.zst", out_path]
    t0 = time.monotonic()
    rc, log = run_limited(args, CASE_TIMEOUT, drop_privileges, cwd=out_dir,
                          env={"PATH": "/nonexistent", "HOME": out_dir, "LC_ALL": "C"})
    elapsed = time.monotonic() - t0
    if rc is None:
        ok, why = False, "timeout"
    elif case["expect"] == "error":
        ok, why = rc == 1, f"exit {rc}"
    elif rc != 0:
        ok, why = False, f"exit {rc}: {log.strip()[-120:]}"
    else:
        ok, why = read_output(out_path, case["size"], case["sha256"])
    try:
        os.remove(out_path)
    except OSError:
        pass
    return {"id": case["id"], "category": case["category"], "expect": case["expect"],
            "pass": bool(ok), "detail": why, "secs": round(elapsed, 3)}


def main():
    manifest = json.load(open(f"{CASES}/manifest.json"))["cases"]
    ev["cases_total"] = len(manifest)
    ev["categories"] = sorted({c["category"] for c in manifest})
    write_evidence()

    if os.path.exists("/etc/ld.so.preload") and os.path.getsize("/etc/ld.so.preload") > 0:
        fail_gate("loader", "/etc/ld.so.preload is present")
        return

    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    os.chmod(WORK, 0o755)
    if not os.path.isdir(SRC) or not os.path.isfile(f"{SRC}/Cargo.toml"):
        fail_gate("build", f"{SRC}/Cargo.toml not found")
        return
    try:
        err = copy_source(SRC, f"{WORK}/src")
    except OSError as e:
        err = f"could not copy {SRC}: {str(e)[:200]}"
    if err:
        fail_gate("source", err)
        return
    err = check_tree(f"{WORK}/src")
    if err:
        fail_gate("source", err)
        return
    ev["gates"]["source"] = 1

    # Cargo also reads .cargo/config.toml from every ancestor of the build
    # directory; move any planted there aside for the build.
    displaced = []
    for anc in ("/.cargo", "/tmp/.cargo"):
        if os.path.lexists(anc):
            aside = f"{anc}.displaced-{os.getpid()}"
            os.rename(anc, aside)
            displaced.append((aside, anc))
    ev["displaced_cargo_config"] = [a for _, a in displaced]

    # Build unprivileged, offline, with a clean environment and a fresh cargo home.
    for d in ("cargo-home", "target", "home"):
        os.makedirs(f"{WORK}/{d}")
    subprocess.run(["chown", "-R", f"{RUN_UID}:{RUN_GID}", WORK], check=True)
    try:
        os.chmod(TESTS, 0o700)
        ev["tests_sealed"] = True
    except OSError:
        ev["tests_sealed"] = False
    env = {"PATH": CLEAN_PATH, "HOME": f"{WORK}/home", "CARGO_HOME": f"{WORK}/cargo-home",
           "RUSTUP_HOME": "/usr/local/rustup", "CARGO_TARGET_DIR": f"{WORK}/target",
           "CARGO_NET_OFFLINE": "true", "LC_ALL": "C"}
    try:
        rc, log = run_limited(["cargo", "build", "--release", "--offline", "--bin", "zstdr"],
                              BUILD_TIMEOUT, drop_privileges_build, cwd=f"{WORK}/src", env=env)
    finally:
        kill_run_user()
        for aside, anc in displaced:
            if not os.path.lexists(anc):
                os.rename(aside, anc)
    ev["build_log_tail"] = log[-3000:]
    binary = f"{WORK}/target/release/zstdr"
    if rc != 0 or not os.path.isfile(binary):
        fail_gate("build", "cargo build --release failed" if rc is not None else "build timed out")
        return
    ev["build_ok"] = True
    ev["gates"]["build"] = 1
    err = check_binary(binary)
    if err:
        fail_gate("linkage", err)
        return
    ev["gates"]["linkage"] = 1

    # Stage inputs where the unprivileged user can read them; hidden
    # expectations stay in the root-only test directory.
    case_dir = f"{WORK}/inputs"
    os.makedirs(f"{case_dir}/cases")
    shutil.copytree(f"{CASES}/dicts", f"{case_dir}/dicts")
    for c in manifest:
        shutil.copy(f"{CASES}/cases/{c['id']}.zst", f"{case_dir}/cases/")
    out_dir = f"{WORK}/out"
    os.makedirs(out_dir)
    final_bin = f"{WORK}/bin/zstdr"
    os.makedirs(f"{WORK}/bin")
    shutil.copy(binary, final_bin)
    subprocess.run(["chown", "-R", "root:root", case_dir, f"{WORK}/bin"], check=True)
    subprocess.run(["chmod", "-R", "a+rX,go-w", case_dir, f"{WORK}/bin"], check=True)
    os.chmod(final_bin, 0o755)
    os.chown(out_dir, RUN_UID, RUN_GID)

    hidden = hide_libzstd()
    try:
        with ThreadPoolExecutor(WORKERS) as ex:
            ev["results"] = list(ex.map(lambda c: run_case(final_bin, c, case_dir, out_dir), manifest))
    finally:
        kill_run_user()
        for path, mode in hidden:
            os.chmod(path, mode)
        ev["verifier_maxrss_kb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


if __name__ == "__main__":
    try:
        main()
    except Exception:
        ev["runtime_ok"] = False
        ev["runtime_error"] = traceback.format_exc()[-2000:]
    finally:
        kill_run_user()
        write_evidence()
