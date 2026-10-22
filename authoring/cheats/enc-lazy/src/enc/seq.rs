//! Sequences section encoding (RFC 8878 §3.1.1.3.2): codes, per-stream
//! table choice and the interleaved backward bitstream.

use super::bitw::{highbit, BitWriter};
use super::fse::{self as efse, CTable};
use crate::fse::{LL_DEFAULT, ML_DEFAULT, OF_DEFAULT};

/// Allow FSE_Compressed table descriptions (else predefined or RLE only).
const CUSTOM_TABLES: bool = true;

#[derive(Clone, Copy)]
pub struct Seq {
    pub lit_len: u32,
    pub match_len: u32,
    /// Offset_Value as transmitted: 1..=3 repeat codes, else offset + 3.
    pub offset_value: u32,
}

pub fn ll_code(v: u32) -> (u8, u32, u32) {
    if v < 16 {
        return (v as u8, 0, 0);
    }
    const BASE: [(u32, u32); 20] = [
        (16, 1), (18, 1), (20, 1), (22, 1), (24, 2), (28, 2), (32, 3), (40, 3), (48, 4), (64, 6),
        (128, 7), (256, 8), (512, 9), (1024, 10), (2048, 11), (4096, 12), (8192, 13), (16384, 14),
        (32768, 15), (65536, 16),
    ];
    let i = BASE.iter().rposition(|&(b, _)| b <= v).unwrap();
    (16 + i as u8, v - BASE[i].0, BASE[i].1)
}

pub fn ml_code(v: u32) -> (u8, u32, u32) {
    if v < 35 {
        return ((v - 3) as u8, 0, 0);
    }
    const BASE: [(u32, u32); 21] = [
        (35, 1), (37, 1), (39, 1), (41, 1), (43, 2), (47, 2), (51, 3), (59, 3), (67, 4), (83, 4),
        (99, 5), (131, 7), (259, 8), (515, 9), (1027, 10), (2051, 11), (4099, 12), (8195, 13),
        (16387, 14), (32771, 15), (65539, 16),
    ];
    let i = BASE.iter().rposition(|&(b, _)| b <= v).unwrap();
    (32 + i as u8, v - BASE[i].0, BASE[i].1)
}

pub fn of_code(v: u32) -> (u8, u32, u32) {
    let c = highbit(v);
    (c as u8, v - (1 << c), c)
}

/// How one of the three code streams is coded.
enum Mode {
    Predefined(CTable),
    Rle(u8),
    Compressed(CTable, Vec<u8>),
}

impl Mode {
    fn bits(&self) -> u8 {
        match self {
            Mode::Predefined(_) => 0,
            Mode::Rle(_) => 1,
            Mode::Compressed(..) => 2,
        }
    }
    fn table(&self) -> Option<&CTable> {
        match self {
            Mode::Predefined(t) | Mode::Compressed(t, _) => Some(t),
            Mode::Rle(_) => None,
        }
    }
}

fn choose(codes: &[u8], max_symbol: usize, max_log: u32, default: &[i16], default_log: u32) -> Mode {
    let mut counts = vec![0u32; max_symbol + 1];
    for &c in codes {
        counts[c as usize] += 1;
    }
    let used: Vec<usize> = (0..=max_symbol).filter(|&s| counts[s] > 0).collect();
    if used.len() == 1 && codes.len() > 2 {
        return Mode::Rle(used[0] as u8);
    }
    let last = *used.last().unwrap();
    let predefined_cost = if last < default.len() {
        efse::cost_bits(&counts, default, default_log)
    } else {
        None
    };
    let counts = &counts[..=last];
    let log = efse::table_log(codes.len(), last, max_log);
    let norm = efse::normalize(counts, log);
    let desc = efse::write_description(&norm, log);
    let fse_cost = efse::cost_bits(counts, &norm, log).map(|b| b + 8 * desc.len() as u64);
    match (predefined_cost, fse_cost) {
        (Some(p), Some(f)) if p <= f || !CUSTOM_TABLES => Mode::Predefined(CTable::new(default, default_log)),
        (_, Some(_)) => Mode::Compressed(CTable::new(&norm, log), desc),
        (Some(_), None) => Mode::Predefined(CTable::new(default, default_log)),
        (None, None) => unreachable!("a normalized table covers every present symbol"),
    }
}

/// The complete sequences section for `seqs`.
pub fn encode(seqs: &[Seq]) -> Vec<u8> {
    let n = seqs.len();
    let mut out = Vec::new();
    if n < 128 {
        out.push(n as u8);
    } else if n < 0x7F00 {
        out.push(((n >> 8) + 128) as u8);
        out.push(n as u8);
    } else {
        out.push(255);
        out.extend_from_slice(&((n - 0x7F00) as u16).to_le_bytes());
    }
    if n == 0 {
        return out;
    }
    let ll: Vec<(u8, u32, u32)> = seqs.iter().map(|s| ll_code(s.lit_len)).collect();
    let ml: Vec<(u8, u32, u32)> = seqs.iter().map(|s| ml_code(s.match_len)).collect();
    let of: Vec<(u8, u32, u32)> = seqs.iter().map(|s| of_code(s.offset_value)).collect();
    let ll_codes: Vec<u8> = ll.iter().map(|c| c.0).collect();
    let ml_codes: Vec<u8> = ml.iter().map(|c| c.0).collect();
    let of_codes: Vec<u8> = of.iter().map(|c| c.0).collect();
    let ll_mode = choose(&ll_codes, 35, 9, &LL_DEFAULT, 6);
    let of_mode = choose(&of_codes, 31, 8, &OF_DEFAULT, 5);
    let ml_mode = choose(&ml_codes, 52, 9, &ML_DEFAULT, 6);
    out.push(ll_mode.bits() << 6 | of_mode.bits() << 4 | ml_mode.bits() << 2);
    for m in [&ll_mode, &of_mode, &ml_mode] {
        match m {
            Mode::Rle(s) => out.push(*s),
            Mode::Compressed(_, desc) => out.extend_from_slice(desc),
            Mode::Predefined(_) => {}
        }
    }

    // Encode last sequence first; the decoder reads in reverse.
    let mut w = BitWriter::new();
    let (llt, oft, mlt) = (ll_mode.table(), of_mode.table(), ml_mode.table());
    let mut st_ll = llt.map_or(0, |t| t.init_state(ll[n - 1].0));
    let mut st_ml = mlt.map_or(0, |t| t.init_state(ml[n - 1].0));
    let mut st_of = oft.map_or(0, |t| t.init_state(of[n - 1].0));
    let extra = |w: &mut BitWriter, i: usize| {
        w.add(ll[i].1 as u64, ll[i].2);
        w.add(ml[i].1 as u64, ml[i].2);
        w.add(of[i].1 as u64, of[i].2);
    };
    extra(&mut w, n - 1);
    for i in (0..n - 1).rev() {
        if let Some(t) = oft {
            t.encode(&mut w, &mut st_of, of[i].0);
        }
        if let Some(t) = mlt {
            t.encode(&mut w, &mut st_ml, ml[i].0);
        }
        if let Some(t) = llt {
            t.encode(&mut w, &mut st_ll, ll[i].0);
        }
        extra(&mut w, i);
    }
    if let Some(t) = mlt {
        t.flush(&mut w, st_ml);
    }
    if let Some(t) = oft {
        t.flush(&mut w, st_of);
    }
    if let Some(t) = llt {
        t.flush(&mut w, st_ll);
    }
    out.extend(w.close());
    out
}
