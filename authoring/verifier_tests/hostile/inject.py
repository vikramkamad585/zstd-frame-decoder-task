"""Turn the reference main.rs into a hostile decoder for runtime tests.

By case number n (from the input file name):
  n % 40 == 0   OUTPUT becomes a symlink to /dev/zero
  n % 40 == 5   OUTPUT becomes a symlink to the sealed manifest
  n % 40 == 10  OUTPUT becomes a FIFO
  n % 40 == 20  OUTPUT becomes a 900 MB sparse file
  n % 40 == 30  300 MB written to stderr, then a normal decode
All other cases decode normally."""
import sys
p = sys.argv[1]
s = open(p).read()
s = s.replace("use std::process::ExitCode;", '''use std::process::ExitCode;
use std::io::Write;

extern "C" {
    fn mkfifo(path: *const std::os::raw::c_char, mode: u32) -> i32;
}

fn hostile(input: &str, output: &str) -> Option<ExitCode> {
    let stem = std::path::Path::new(input).file_stem()?.to_str()?;
    let n: u64 = stem.parse().ok()?;
    let _ = std::fs::remove_file(output);
    match n % 40 {
        0 => { std::os::unix::fs::symlink("/dev/zero", output).ok()?; Some(ExitCode::SUCCESS) }
        5 => { std::os::unix::fs::symlink("/tests/cases/manifest.json", output).ok()?; Some(ExitCode::SUCCESS) }
        10 => {
            let c = std::ffi::CString::new(output).ok()?;
            unsafe { mkfifo(c.as_ptr(), 0o666) };
            Some(ExitCode::SUCCESS)
        }
        20 => {
            let f = std::fs::File::create(output).ok()?;
            f.set_len(900 << 20).ok()?;
            Some(ExitCode::SUCCESS)
        }
        30 => {
            let junk = vec![b'x'; 1 << 20];
            let mut e = std::io::stderr().lock();
            for _ in 0..300 { let _ = e.write_all(&junk); }
            None
        }
        _ => None,
    }
}''', 1)
s = s.replace("    let data = match std::fs::read(input) {",
              "    if let Some(code) = hostile(input, output) { return code; }\n    let data = match std::fs::read(input) {", 1)
open(p, "w").write(s)
