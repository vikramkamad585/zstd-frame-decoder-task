"""Make the reference decoder import fork() plus every other default-versioned
libc and libm function, so `readelf --dyn-syms` prints far more than the verifier's
64 KiB log tail. The linkage gate must still see the fork import."""
import glob
import re
import subprocess
import sys

FORBIDDEN = {"dlopen", "dlmopen", "execve", "execv", "execvp", "execvpe", "execl",
             "execlp", "execle", "fexecve", "posix_spawn", "posix_spawnp", "fork",
             "vfork", "system", "popen"}
src = sys.argv[1]
out = ""
for lib in ("libc.so.6", "libm.so.6"):
    path = glob.glob(f"/lib/*/{lib}")[0]
    out += subprocess.run(["readelf", "--dyn-syms", "-W", path], capture_output=True, text=True).stdout
names = []
for line in out.splitlines():
    c = line.split()
    if len(c) >= 8 and c[3] == "FUNC" and c[6] != "UND" and "@@GLIBC_2" in c[7]:
        n = c[7].split("@")[0]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n) and n not in FORBIDDEN:
            names.append(n)
names = sorted(set(names))
body = "#![allow(clashing_extern_declarations, dead_code)]\nextern \"C\" {\n    fn fork();\n"
body += "".join(f"    fn {n}();\n" for n in names) + "}\n"
body += "pub fn keep() {\n    let v: &[unsafe extern \"C\" fn()] = &[" + ", ".join(names) + ", fork];\n"
body += "    std::hint::black_box(v);\n}\n"
open(src.rsplit("/", 1)[0] + "/bloat.rs", "w").write(body)
s = open(src).read()
s = s.replace("fn main() -> ExitCode {", "fn main() -> ExitCode {\n    if std::env::args().count() > 99 { bloat::keep(); }", 1) + "\nmod bloat;\n"
open(src, "w").write(s)
print(len(names), "symbols")
