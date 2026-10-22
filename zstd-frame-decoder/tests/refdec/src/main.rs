//! Strict reference decoder used to check encoder output: INPUT OUTPUT, no
//! dictionary. Exit 0 with OUTPUT written, 1 when the input is rejected.
#![allow(dead_code)]

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

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let [input, output] = args.as_slice() else {
        eprintln!("usage: zstdr-refdec INPUT OUTPUT");
        return ExitCode::from(2);
    };
    let data = match std::fs::read(input) {
        Ok(d) => d,
        Err(e) => {
            eprintln!("cannot read {}: {}", input, e);
            return ExitCode::from(1);
        }
    };
    match frame::decode_all(&data, None) {
        Ok(out) => match std::fs::write(output, &out) {
            Ok(()) => ExitCode::SUCCESS,
            Err(e) => {
                eprintln!("cannot write {}: {}", output, e);
                ExitCode::from(1)
            }
        },
        Err(e) => {
            eprintln!("{}", e);
            ExitCode::from(1)
        }
    }
}
