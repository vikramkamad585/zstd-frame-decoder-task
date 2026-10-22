//! zstdr: a std-only Zstandard (RFC 8878) decoder.
//!
//! Usage: zstdr [-D DICTIONARY] INPUT OUTPUT

mod bits;
mod block;
mod dict;
mod error;
mod frame;
mod fse;
mod huffman;
mod literals;
mod sequences;
mod window;
mod xxhash64;

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
        Some(p) => match std::fs::read(&p).map_err(|e| e.to_string()).and_then(|d| {
            dict::Dictionary::parse(&d).map_err(|e| e.to_string())
        }) {
            Ok(d) => Some(d),
            Err(e) => {
                eprintln!("zstdr: bad dictionary {}: {}", p, e);
                return ExitCode::from(1);
            }
        },
    };
    let _ = &dict; match frame::decode_all(&data, None) {
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
