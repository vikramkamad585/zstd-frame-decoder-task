//! Finite State Entropy tables: description parsing, decoding-table
//! construction, and the predefined distributions of RFC 8878 §3.1.1.3.2.2.

use crate::bits::{BackwardBits, ForwardBits};
use crate::error::Result;
use crate::{bail, ensure};

#[derive(Clone, Copy, Default)]
pub struct Entry {
    pub symbol: u8,
    pub nb_bits: u8,
    pub baseline: u16,
}

#[derive(Clone)]
pub struct Table {
    pub accuracy_log: u32,
    pub entries: Vec<Entry>,
}

pub const LL_DEFAULT: [i16; 36] = [
    4, 3, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 3, 2, 1, 1, 1, 1, 1,
    -1, -1, -1, -1,
];
pub const ML_DEFAULT: [i16; 53] = [
    1, 4, 3, 2, 2, 2, 2, 2, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, -1, -1, -1, -1, -1, -1, -1,
];
pub const OF_DEFAULT: [i16; 29] = [
    1, 1, 1, 1, 1, 1, 2, 2, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, -1, -1, -1, -1, -1,
];

impl Table {
    /// A table that always yields `symbol` and consumes no bits.
    pub fn rle(symbol: u8) -> Table {
        Table { accuracy_log: 0, entries: vec![Entry { symbol, nb_bits: 0, baseline: 0 }] }
    }

    /// Build a decoding table from normalized counts (-1 = "less than one").
    pub fn from_counts(counts: &[i16], accuracy_log: u32) -> Result<Table> {
        let size = 1usize << accuracy_log;
        let mut entries = vec![Entry::default(); size];
        let mut high = size as isize - 1;
        let mut next = vec![0u32; counts.len()];
        for (s, &c) in counts.iter().enumerate() {
            if c == -1 {
                ensure!(high >= 0, "too many low-probability symbols");
                entries[high as usize].symbol = s as u8;
                high -= 1;
                next[s] = 1;
            } else {
                next[s] = c.max(0) as u32;
            }
        }
        let step = (size >> 1) + (size >> 3) + 3;
        let mask = size - 1;
        let mut pos = 0usize;
        for (s, &c) in counts.iter().enumerate() {
            for _ in 0..c.max(0) {
                entries[pos].symbol = s as u8;
                loop {
                    pos = (pos + step) & mask;
                    if pos as isize <= high {
                        break;
                    }
                }
            }
        }
        ensure!(pos == 0, "FSE table spread did not complete");
        for e in entries.iter_mut() {
            let s = e.symbol as usize;
            let x = next[s];
            next[s] += 1;
            ensure!(x > 0, "FSE symbol with zero probability in table");
            let nb_bits = accuracy_log - (31 - x.leading_zeros());
            e.nb_bits = nb_bits as u8;
            e.baseline = ((x << nb_bits) as usize - size) as u16;
        }
        Ok(Table { accuracy_log, entries })
    }

    /// Parse a table description at the start of `data`. Returns the table
    /// and the number of bytes consumed.
    pub fn read_description(data: &[u8], max_symbol: usize, max_log: u32) -> Result<(Table, usize)> {
        let mut br = ForwardBits::new(data);
        let accuracy_log = br.read(4)? as u32 + 5;
        ensure!(accuracy_log <= max_log, "FSE accuracy log {} exceeds {}", accuracy_log, max_log);
        let mut remaining: i32 = (1 << accuracy_log) + 1;
        let mut threshold: i32 = 1 << accuracy_log;
        let mut nb_bits: u32 = accuracy_log + 1;
        let mut counts: Vec<i16> = Vec::with_capacity(max_symbol + 1);
        let mut previous_zero = false;
        while remaining > 1 && counts.len() <= max_symbol {
            if previous_zero {
                loop {
                    let rep = br.read(2)? as usize;
                    for _ in 0..rep {
                        counts.push(0);
                    }
                    if rep != 3 {
                        break;
                    }
                }
                ensure!(counts.len() <= max_symbol, "FSE zero-run passes the last symbol");
            }
            let max = (2 * threshold - 1) - remaining;
            let low = br.peek(nb_bits - 1) as i32;
            let mut value;
            if low < max {
                value = low;
                br.skip(nb_bits - 1)?;
            } else {
                value = br.peek(nb_bits) as i32;
                if value >= threshold {
                    value -= max;
                }
                br.skip(nb_bits)?;
            }
            let count = value - 1;
            remaining -= count.abs();
            counts.push(count as i16);
            previous_zero = count == 0;
            while remaining < threshold {
                nb_bits -= 1;
                threshold >>= 1;
            }
        }
        ensure!(remaining == 1, "FSE probabilities do not sum to the table size");
        ensure!(counts.len() <= max_symbol + 1, "FSE description has too many symbols");
        let table = Table::from_counts(&counts, accuracy_log)?;
        Ok((table, br.bytes_consumed()))
    }
}

/// A decoder state walking one table over a backward bitstream.
pub struct State<'t> {
    table: &'t Table,
    state: usize,
}

impl<'t> State<'t> {
    pub fn new(table: &'t Table, br: &mut BackwardBits) -> State<'t> {
        let state = br.read(table.accuracy_log) as usize;
        State { table, state }
    }

    #[inline]
    pub fn symbol(&self) -> u8 {
        self.table.entries[self.state].symbol
    }

    #[inline]
    pub fn update(&mut self, br: &mut BackwardBits) {
        let e = self.table.entries[self.state];
        self.state = e.baseline as usize + br.read(e.nb_bits as u32) as usize;
    }
}

pub fn predefined(counts: &[i16], log: u32) -> Table {
    match Table::from_counts(counts, log) {
        Ok(t) => t,
        Err(_) => unreachable!("predefined distribution is valid"),
    }
}

/// Decode FSE-compressed Huffman weights: two interleaved states over one
/// backward bitstream, stopping when the stream is overread.
pub fn decode_interleaved(table: &Table, data: &[u8], max_out: usize) -> Result<Vec<u8>> {
    let mut br = BackwardBits::new(data)?;
    let mut s1 = State::new(table, &mut br);
    let mut s2 = State::new(table, &mut br);
    let mut out = Vec::new();
    loop {
        ensure!(out.len() + 2 <= max_out, "too many Huffman weights");
        out.push(s1.symbol());
        s1.update(&mut br);
        if br.overflowed() {
            out.push(s2.symbol());
            break;
        }
        out.push(s2.symbol());
        s2.update(&mut br);
        if br.overflowed() {
            out.push(s1.symbol());
            break;
        }
    }
    if br.remaining() < -64 {
        bail!("Huffman weight stream overread");
    }
    Ok(out)
}
