//! Shortcut decoder: frame headers, skippable frames, Raw and RLE blocks only.
//! Anything with a Compressed block is rejected. It must score 0.
use std::process::ExitCode;

fn decode(b: &[u8]) -> Option<Vec<u8>> {
    let mut out = Vec::new();
    let mut i = 0usize;
    if b.is_empty() {
        return None;
    }
    let u32le = |i: usize| -> Option<u32> { Some(u32::from_le_bytes(b.get(i..i + 4)?.try_into().ok()?)) };
    while i < b.len() {
        let magic = u32le(i)?;
        if (0x184D2A50..=0x184D2A5F).contains(&magic) {
            i = i.checked_add(8 + u32le(i + 4)? as usize)?;
            if i > b.len() {
                return None;
            }
            continue;
        }
        if magic != 0xFD2FB528 {
            return None;
        }
        let fhd = *b.get(i + 4)?;
        if fhd & 0x18 != 0 {
            return None;
        }
        let single = (fhd >> 5) & 1;
        i += 5 + (1 - single as usize) + [0, 1, 2, 4][(fhd & 3) as usize]
            + [single as usize, 2, 4, 8][(fhd >> 6) as usize];
        loop {
            let h = b.get(i..i + 3)?;
            let h = h[0] as usize | (h[1] as usize) << 8 | (h[2] as usize) << 16;
            i += 3;
            let size = h >> 3;
            match (h >> 1) & 3 {
                0 => {
                    out.extend_from_slice(b.get(i..i + size)?);
                    i += size;
                }
                1 => {
                    let v = *b.get(i)?;
                    out.resize(out.len() + size, v);
                    i += 1;
                }
                _ => return None,
            }
            if h & 1 == 1 {
                break;
            }
        }
        if (fhd >> 2) & 1 == 1 {
            i += 4;
        }
        if i > b.len() {
            return None;
        }
    }
    Some(out)
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let args: Vec<&String> = if args.first().map(|a| a == "-D").unwrap_or(false) { args[2..].iter().collect() } else { args.iter().collect() };
    if args.len() != 2 {
        return ExitCode::from(2);
    }
    let Ok(data) = std::fs::read(args[0]) else { return ExitCode::from(1) };
    match decode(&data) {
        Some(out) if std::fs::write(args[1], &out).is_ok() => ExitCode::SUCCESS,
        _ => ExitCode::from(1),
    }
}
