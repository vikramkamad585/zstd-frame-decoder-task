//! FSE encoding: count normalization, table descriptions and the coding
//! tables that mirror the decoder's symbol spread (RFC 8878 §4.1).

use super::bitw::{highbit, BitWriter};

/// Accuracy log for `total` symbols drawn from `max_symbol + 1` values.
pub fn table_log(total: usize, max_symbol: usize, max_log: u32) -> u32 {
    let mut log = max_log;
    let src_bits = highbit((total - 1).max(1) as u32).saturating_sub(2);
    if src_bits < log {
        log = src_bits;
    }
    let min_bits = (highbit(total as u32) + 1).min(highbit(max_symbol.max(1) as u32) + 2);
    if min_bits > log {
        log = min_bits;
    }
    log.clamp(5, max_log)
}

/// Scale `counts` to sum to 1 << log; every present symbol keeps at least 1.
pub fn normalize(counts: &[u32], log: u32) -> Vec<i16> {
    let size = 1i64 << log;
    let total: i64 = counts.iter().map(|&c| c as i64).sum();
    let mut norm = vec![0i16; counts.len()];
    let mut assigned = 0i64;
    let mut rema: Vec<(i64, usize)> = Vec::new();
    for (s, &c) in counts.iter().enumerate() {
        if c == 0 {
            continue;
        }
        let exact = c as i64 * size;
        let v = (exact / total).max(1);
        norm[s] = v as i16;
        assigned += v;
        rema.push((exact - v * total, s));
    }
    // Hand out what rounding left over to the largest remainders first.
    rema.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.cmp(&b.1)));
    let mut i = 0;
    while assigned < size {
        norm[rema[i % rema.len()].1] += 1;
        assigned += 1;
        i += 1;
    }
    // Over budget (many symbols forced up to 1): take from the largest.
    while assigned > size {
        let s = (0..norm.len()).max_by_key(|&s| (norm[s], std::cmp::Reverse(s))).unwrap();
        norm[s] -= 1;
        assigned -= 1;
    }
    norm
}

/// floor(256 * log2(x)) for x >= 1, in integers so every platform agrees.
pub fn log2_fixed(x: u32) -> u32 {
    let hb = highbit(x);
    let frac = if hb >= 8 { (x >> (hb - 8)) & 0xFF } else { (x << (8 - hb)) & 0xFF };
    hb * 256 + frac
}

/// Bits a stream of `counts` costs under normalized table `norm`, or None
/// when some present symbol has no probability.
pub fn cost_bits(counts: &[u32], norm: &[i16], log: u32) -> Option<u64> {
    let mut bits256 = 0u64;
    for (s, &c) in counts.iter().enumerate() {
        if c == 0 {
            continue;
        }
        let p = *norm.get(s)?;
        let p = if p == -1 { 1 } else { p as i64 };
        if p <= 0 {
            return None;
        }
        bits256 += c as u64 * (log * 256 - log2_fixed(p as u32)) as u64;
    }
    Some((bits256 + 255) / 256)
}

/// Table description in the format `fse::Table::read_description` parses.
pub fn write_description(norm: &[i16], log: u32) -> Vec<u8> {
    let mut w = BitWriter::new();
    w.add((log - 5) as u64, 4);
    let size = 1i32 << log;
    let mut remaining = size + 1;
    let mut threshold = size;
    let mut nb_bits = log + 1;
    let mut s = 0usize;
    let mut previous_zero = false;
    while remaining > 1 {
        if previous_zero {
            let mut start = s;
            while norm[s] == 0 {
                s += 1;
            }
            while s >= start + 3 {
                w.add(3, 2);
                start += 3;
            }
            w.add((s - start) as u64, 2);
        }
        let count = norm[s] as i32;
        s += 1;
        let max = (2 * threshold - 1) - remaining;
        remaining -= count.abs();
        let mut value = count + 1;
        if value >= threshold {
            value += max;
        }
        w.add(value as u64, if value < max { nb_bits - 1 } else { nb_bits });
        previous_zero = value == 1;
        while remaining < threshold {
            nb_bits -= 1;
            threshold >>= 1;
        }
    }
    w.finish()
}

/// Encoding tables for one normalized distribution.
pub struct CTable {
    log: u32,
    state_table: Vec<u16>,
    /// Per symbol: (delta_nb_bits, delta_find_state).
    symbol_tt: Vec<(u32, i32)>,
}

impl CTable {
    pub fn new(norm: &[i16], log: u32) -> CTable {
        let size = 1usize << log;
        let mut cumul = vec![0u32; norm.len() + 1];
        let mut symbols = vec![0u8; size];
        let mut high = size - 1;
        for (s, &c) in norm.iter().enumerate() {
            if c == -1 {
                cumul[s + 1] = cumul[s] + 1;
                symbols[high] = s as u8;
                high -= 1;
            } else {
                cumul[s + 1] = cumul[s] + c.max(0) as u32;
            }
        }
        let step = (size >> 1) + (size >> 3) + 3;
        let mask = size - 1;
        let mut pos = 0usize;
        for (s, &c) in norm.iter().enumerate() {
            for _ in 0..c.max(0) {
                symbols[pos] = s as u8;
                loop {
                    pos = (pos + step) & mask;
                    if pos <= high {
                        break;
                    }
                }
            }
        }
        debug_assert_eq!(pos, 0);
        let mut next = cumul.clone();
        let mut state_table = vec![0u16; size];
        for (u, &s) in symbols.iter().enumerate() {
            state_table[next[s as usize] as usize] = (size + u) as u16;
            next[s as usize] += 1;
        }
        let mut symbol_tt = vec![(0u32, 0i32); norm.len()];
        let mut total = 0i32;
        for (s, &c) in norm.iter().enumerate() {
            symbol_tt[s] = match c {
                0 => (((log + 1) << 16) - size as u32, 0),
                -1 | 1 => {
                    let t = ((log << 16) - size as u32, total - 1);
                    total += 1;
                    t
                }
                _ => {
                    let c = c as u32;
                    let max_bits_out = log - highbit(c - 1);
                    let min_state_plus = c << max_bits_out;
                    let t = ((max_bits_out << 16).wrapping_sub(min_state_plus), total - c as i32);
                    total += c as i32;
                    t
                }
            };
        }
        CTable { log, state_table, symbol_tt }
    }

    /// Initial state for the first symbol encoded (the last one decoded).
    pub fn init_state(&self, symbol: u8) -> u32 {
        let (delta_nb, delta_find) = self.symbol_tt[symbol as usize];
        let nb_out = (delta_nb.wrapping_add(1 << 15)) >> 16;
        let value = (nb_out << 16).wrapping_sub(delta_nb);
        self.state_table[((value >> nb_out) as i32 + delta_find) as usize] as u32
    }

    #[inline]
    pub fn encode(&self, w: &mut BitWriter, state: &mut u32, symbol: u8) {
        let (delta_nb, delta_find) = self.symbol_tt[symbol as usize];
        let nb_out = (state.wrapping_add(delta_nb)) >> 16;
        w.add(*state as u64, nb_out);
        *state = self.state_table[((*state >> nb_out) as i32 + delta_find) as usize] as u32;
    }

    pub fn flush(&self, w: &mut BitWriter, state: u32) {
        w.add(state as u64, self.log);
    }
}

/// Two interleaved states over one stream, as Huffman weights are coded.
/// Needs at least two symbols.
pub fn encode_interleaved(symbols: &[u8], ct: &CTable) -> Vec<u8> {
    let mut w = BitWriter::new();
    let n = symbols.len();
    let (mut s1, mut s2);
    let mut i;
    if n % 2 == 1 {
        s1 = ct.init_state(symbols[n - 1]);
        s2 = ct.init_state(symbols[n - 2]);
        ct.encode(&mut w, &mut s1, symbols[n - 3]);
        i = n - 3;
    } else {
        s2 = ct.init_state(symbols[n - 1]);
        s1 = ct.init_state(symbols[n - 2]);
        i = n - 2;
    }
    while i > 0 {
        ct.encode(&mut w, &mut s2, symbols[i - 1]);
        ct.encode(&mut w, &mut s1, symbols[i - 2]);
        i -= 2;
    }
    ct.flush(&mut w, s2);
    ct.flush(&mut w, s1);
    w.close()
}
