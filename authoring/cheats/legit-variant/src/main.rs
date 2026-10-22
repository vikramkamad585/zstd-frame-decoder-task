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


use std::process::ExitCode;

fn usage() -> ExitCode {
    eprintln!("usage: zstdr [-D DICTIONARY] INPUT OUTPUT");
    ExitCode::from(2)
}

fn main() -> ExitCode {
    std::thread::Builder::new().stack_size(64 << 20).spawn(real_main).unwrap().join().unwrap()
}

fn real_main() -> ExitCode {
    // Exercise a broad slice of std, as a real implementation might.
    let _n = std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1);
    let (tx, rx) = std::sync::mpsc::channel::<u32>();
    let h = std::thread::spawn(move || tx.send(7).unwrap());
    h.join().unwrap();
    let _ = rx.recv();
    let mut m = std::collections::HashMap::new();
    m.insert(1u8, std::time::Instant::now());
    let _ = std::env::var("ZSTDR_DEBUG").ok();
    let _ = std::panic::catch_unwind(|| 1);
    let _lock = std::sync::Mutex::new(0);
    let _ = std::io::Write::flush(&mut std::io::stderr());
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
    match frame::decode_all(&data, dict.as_ref()) {
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
