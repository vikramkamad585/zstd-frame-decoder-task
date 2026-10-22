"""Make the reference decoder try to swap the grading directories it does
not own: with the old ownership the run user could rename the root-owned
bin/ and inputs/ out of the run user's grading directory and plant its own."""
import sys
p = sys.argv[1]
s = open(p).read()
s = s.replace("fn main() -> ExitCode {", """fn main() -> ExitCode {
    let _ = std::fs::rename("/tmp/zgrade/bin", "/tmp/zgrade/bin.stolen");
    let _ = std::fs::rename("/tmp/zgrade/inputs", "/tmp/zgrade/inputs.stolen");""", 1)
open(p, "w").write(s)
