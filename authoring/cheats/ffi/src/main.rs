//! zstdr: a std-only Zstandard (RFC 8878) decoder.
//!
//! Usage: zstdr [-D DICTIONARY] INPUT OUTPUT
//!
//! Exit status 0 on success with OUTPUT holding the decoded bytes; exit
//! status 1 when the input is rejected; exit status 2 on a usage error.

mod frame;

use std::process::ExitCode;

fn usage() -> ExitCode {
    eprintln!("usage: zstdr [-D DICTIONARY] INPUT OUTPUT");
    ExitCode::from(2)
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut dict_path = None;
    let mut files = Vec::new();
    let mut i = 0;
    while i < args.len() {
        if args[i] == "-D" {
            let Some(p) = args.get(i + 1) else { return usage() };
            dict_path = Some(p.clone());
            i += 2;
        } else {
            files.push(args[i].clone());
            i += 1;
        }
    }
    let [input, output] = files.as_slice() else { return usage() };

    let data = match std::fs::read(input) {
        Ok(d) => d,
        Err(e) => {
            eprintln!("zstdr: cannot read {}: {}", input, e);
            return ExitCode::from(1);
        }
    };
    let dict = match dict_path {
        None => None,
        Some(p) => match std::fs::read(&p) {
            Ok(d) => Some(d),
            Err(e) => {
                eprintln!("zstdr: cannot read dictionary {}: {}", p, e);
                return ExitCode::from(1);
            }
        },
    };
    match frame::decode_all(&data, dict.as_deref()) {
        Ok(out) => {
            if let Err(e) = std::fs::write(output, &out) {
                eprintln!("zstdr: cannot write {}: {}", output, e);
                return ExitCode::from(1);
            }
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("zstdr: {}", e);
            ExitCode::from(1)
        }
    }
}
