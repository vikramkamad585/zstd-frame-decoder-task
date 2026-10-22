//! Huffman literal encoding: length-limited code construction, the tree
//! description (direct or FSE-compressed weights) and 1- or 4-stream coding.

use super::bitw::BitWriter;
use super::fse as efse;

const MAX_BITS: u32 = 11;

/// Optimal code lengths no longer than `limit` (package-merge). Symbols
/// with a zero count get length 0. Needs at least two present symbols.
fn limited_lengths(counts: &[u32; 256], limit: u32) -> [u8; 256] {
    let mut leaves: Vec<(u64, usize)> =
        (0..256).filter(|&s| counts[s] > 0).map(|s| (counts[s] as u64, s)).collect();
    leaves.sort();
    let n = leaves.len();
    // Each item: weight and how many times each leaf (by rank) occurs in it.
    type Item = (u64, Vec<u8>);
    let leaf_items: Vec<Item> = (0..n)
        .map(|i| {
            let mut v = vec![0u8; n];
            v[i] = 1;
            (leaves[i].0, v)
        })
        .collect();
    let mut list: Vec<Item> = leaf_items.clone();
    for _ in 1..limit {
        let mut packages: Vec<Item> = Vec::with_capacity(list.len() / 2);
        for pair in list.chunks_exact(2) {
            let v: Vec<u8> = pair[0].1.iter().zip(&pair[1].1).map(|(a, b)| a + b).collect();
            packages.push((pair[0].0 + pair[1].0, v));
        }
        let mut merged: Vec<Item> = Vec::with_capacity(n + packages.len());
        let (mut a, mut b) = (0, 0);
        while a < n || b < packages.len() {
            if b >= packages.len() || (a < n && leaf_items[a].0 <= packages[b].0) {
                merged.push(leaf_items[a].clone());
                a += 1;
            } else {
                merged.push(packages[b].clone());
                b += 1;
            }
        }
        list = merged;
    }
    let mut lengths = [0u8; 256];
    for item in list.iter().take(2 * n - 2) {
        for (rank, &k) in item.1.iter().enumerate() {
            lengths[leaves[rank].1] += k;
        }
    }
    lengths
}

pub struct HuffEncoder {
    codes: [(u16, u8); 256],
    pub description: Vec<u8>,
}

impl HuffEncoder {
    /// Build a code and its tree description; None when literals are better
    /// sent raw (one symbol, or a description that cannot be written).
    pub fn build(counts: &[u32; 256]) -> Option<HuffEncoder> {
        let present = counts.iter().filter(|&&c| c > 0).count();
        if present < 2 {
            return None;
        }
        let lengths = limited_lengths(counts, MAX_BITS);
        let max_bits = *lengths.iter().max().unwrap() as u32;
        let last = (0..256).rev().find(|&s| lengths[s] > 0).unwrap();
        let weights: Vec<u8> = (0..256)
            .map(|s| if lengths[s] == 0 { 0 } else { (max_bits + 1 - lengths[s] as u32) as u8 })
            .collect();

        // Canonical codes in the order the decoder fills its table: by weight
        // ascending, then symbol ascending.
        let mut start = [0u32; (MAX_BITS + 2) as usize];
        let mut next = 0u32;
        for w in 1..=max_bits as usize {
            start[w] = next;
            next += (weights.iter().filter(|&&x| x as usize == w).count() as u32) << (w - 1);
        }
        let mut codes = [(0u16, 0u8); 256];
        for s in 0..256 {
            let w = weights[s] as usize;
            if w == 0 {
                continue;
            }
            codes[s] = ((start[w] >> (w - 1)) as u16, lengths[s]);
            start[w] += 1 << (w - 1);
        }

        let sent = &weights[..last];
        let description = describe(sent)?;
        Some(HuffEncoder { codes, description })
    }

    /// Estimated size in bytes of `counts` coded with this tree.
    pub fn estimate(&self, counts: &[u32; 256]) -> usize {
        let bits: u64 = (0..256).map(|s| counts[s] as u64 * self.codes[s].1 as u64).sum();
        (bits as usize + 7) / 8
    }

    fn stream(&self, lits: &[u8]) -> Vec<u8> {
        let mut w = BitWriter::new();
        for &b in lits.iter().rev() {
            let (code, len) = self.codes[b as usize];
            w.add(code as u64, len as u32);
        }
        w.close()
    }

    /// The literals coded as one stream, or four with a jump table.
    pub fn encode(&self, lits: &[u8], four: bool) -> Option<Vec<u8>> {
        if !four {
            return Some(self.stream(lits));
        }
        let seg = (lits.len() + 3) / 4;
        let parts: Vec<Vec<u8>> = (0..4)
            .map(|i| self.stream(&lits[(i * seg).min(lits.len())..((i + 1) * seg).min(lits.len())]))
            .collect();
        let mut out = Vec::new();
        for p in &parts[..3] {
            if p.len() > 0xFFFF {
                return None;
            }
            out.extend_from_slice(&(p.len() as u16).to_le_bytes());
        }
        for p in &parts {
            out.extend_from_slice(p);
        }
        Some(out)
    }
}

/// Tree description for the transmitted weights (all but the last symbol's).
fn describe(weights: &[u8]) -> Option<Vec<u8>> {
    let direct = if weights.len() <= 128 {
        let mut d = vec![127 + weights.len() as u8];
        for pair in weights.chunks(2) {
            d.push(pair[0] << 4 | pair.get(1).copied().unwrap_or(0));
        }
        Some(d)
    } else {
        None
    };
    let compressed = if weights.len() >= 2 { fse_weights(weights) } else { None };
    match (direct, compressed) {
        (Some(d), Some(c)) => Some(if c.len() < d.len() { c } else { d }),
        (d, c) => d.or(c),
    }
}

fn fse_weights(weights: &[u8]) -> Option<Vec<u8>> {
    let mut counts = [0u32; 13];
    for &w in weights {
        counts[w as usize] += 1;
    }
    let max_symbol = (0..13).rev().find(|&s| counts[s] > 0)?;
    let counts = &counts[..=max_symbol];
    let log = efse::table_log(weights.len(), max_symbol, 6);
    let norm = efse::normalize(counts, log);
    let mut body = efse::write_description(&norm, log);
    let ct = efse::CTable::new(&norm, log);
    body.extend(efse::encode_interleaved(weights, &ct));
    if body.len() >= 128 {
        return None;
    }
    // A distribution can end the stream ambiguously; keep only descriptions
    // that decode back to exactly these weights.
    let desc_len = efse::write_description(&norm, log).len();
    let table = crate::fse::Table::read_description(&body, 255, 6).ok()?.0;
    let back = crate::fse::decode_interleaved(&table, &body[desc_len..], 256).ok()?;
    if back != weights {
        return None;
    }
    let mut out = vec![body.len() as u8];
    out.extend(body);
    Some(out)
}
