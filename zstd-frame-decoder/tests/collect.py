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
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=cwd,
                         env=env, preexec_fn=preexec)
    try:
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, out.decode(errors="replace")
    except subprocess.TimeoutExpired:
        kill_group(p)
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
        try:
            h = hashlib.sha256()
            size = 0
            with open(out_path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
                    size += len(chunk)
            ok = h.hexdigest() == case["sha256"] and size == case["size"]
            why = "match" if ok else f"output differs (size {size}, expected {case['size']})"
        except OSError as e:
            ok, why = False, f"no output: {e}"
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
        shutil.copytree(SRC, f"{WORK}/src", symlinks=False,
                        ignore=shutil.ignore_patterns("target", ".git"),
                        ignore_dangling_symlinks=True)
    except (OSError, shutil.Error) as e:
        fail_gate("source", f"could not copy {SRC}: {str(e)[:200]}")
        return
    err = check_tree(f"{WORK}/src")
    if err:
        fail_gate("source", err)
        return
    ev["gates"]["source"] = 1

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
    rc, log = run_limited(["cargo", "build", "--release", "--offline", "--bin", "zstdr"],
                          BUILD_TIMEOUT, drop_privileges_build, cwd=f"{WORK}/src", env=env)
    kill_run_user()
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


if __name__ == "__main__":
    try:
        main()
    except Exception:
        ev["runtime_ok"] = False
        ev["runtime_error"] = traceback.format_exc()[-2000:]
    finally:
        kill_run_user()
        write_evidence()
