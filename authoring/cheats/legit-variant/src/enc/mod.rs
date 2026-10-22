//! Zstandard encoder: one single-segment frame with the content size, cut
//! into 128 KiB blocks, each sent as the smallest of Compressed, RLE or Raw.

mod bitw;
mod fse;
mod huff;
mod matcher;
mod opt;
mod seq;

use huff::HuffEncoder;
use matcher::{Matcher, Reps};

const BLOCK_MAX: usize = 128 * 1024;
/// Huffman-code literals when that is smaller than sending them raw.
const HUFFMAN_LITERALS: bool = true;
/// Price-driven optimal parsing (else hash-chain lazy parsing).
const OPTIMAL_PARSE: bool = true;

fn frame_header(size: usize) -> Vec<u8> {
    let mut h = crate::frame::FRAME_MAGIC.to_le_bytes().to_vec();
    let size = size as u64;
    // Single_Segment_flag set: the window is the content size.
    let (flag, field): (u8, Vec<u8>) = if size < 256 {
        (0, vec![size as u8])
    } else if size < 65536 + 256 {
        (1, ((size - 256) as u16).to_le_bytes().to_vec())
    } else if size <= u32::MAX as u64 {
        (2, (size as u32).to_le_bytes().to_vec())
    } else {
        (3, size.to_le_bytes().to_vec())
    };
    h.push(flag << 6 | 1 << 5);
    h.extend(field);
    h
}

fn block_header(last: bool, kind: u32, size: usize) -> [u8; 3] {
    let v = last as u32 | kind << 1 | (size as u32) << 3;
    [v as u8, (v >> 8) as u8, (v >> 16) as u8]
}

fn raw_literals(lits: &[u8]) -> Vec<u8> {
    let n = lits.len();
    let mut out = if n < 32 {
        vec![(n << 3) as u8]
    } else if n < 4096 {
        vec![(1 << 2 | (n & 15) << 4) as u8, (n >> 4) as u8]
    } else {
        vec![(3 << 2 | (n & 15) << 4) as u8, (n >> 4) as u8, (n >> 12) as u8]
    };
    out.extend_from_slice(lits);
    out
}

fn rle_literals(b: u8, n: usize) -> Vec<u8> {
    let mut out = if n < 32 {
        vec![(n << 3 | 1) as u8]
    } else if n < 4096 {
        vec![(1 | 1 << 2 | (n & 15) << 4) as u8, (n >> 4) as u8]
    } else {
        vec![(1 | 3 << 2 | (n & 15) << 4) as u8, (n >> 4) as u8, (n >> 12) as u8]
    };
    out.push(b);
    out
}

/// Literals section: Huffman-compressed when that is smaller, else RLE or raw.
fn literals_section(lits: &[u8]) -> Vec<u8> {
    let n = lits.len();
    if n > 1 && lits.iter().all(|&b| b == lits[0]) {
        return rle_literals(lits[0], n);
    }
    let raw = raw_literals(lits);
    if n < 64 || !HUFFMAN_LITERALS {
        return raw;
    }
    let mut counts = [0u32; 256];
    for &b in lits {
        counts[b as usize] += 1;
    }
    let Some(enc) = HuffEncoder::build(&counts) else { return raw };
    if enc.description.len() + enc.estimate(&counts) + 5 >= raw.len() {
        return raw;
    }
    let four = n >= 256;
    let Some(body) = enc.encode(lits, four) else { return raw };
    let csize = enc.description.len() + body.len();
    let (sf, bits, hsize) = if !four {
        (0u64, 10, 3)
    } else if n < 1024 && csize < 1024 {
        (1, 10, 3)
    } else if n < 16384 && csize < 16384 {
        (2, 14, 4)
    } else {
        (3, 18, 5)
    };
    if !four && (n >= 1024 || csize >= 1024) {
        return raw;
    }
    let h = 2u64 | sf << 2 | (n as u64) << 4 | (csize as u64) << (4 + bits);
    let mut out: Vec<u8> = (0..hsize).map(|i| (h >> (8 * i)) as u8).collect();
    out.extend_from_slice(&enc.description);
    out.extend(body);
    if out.len() >= raw.len() {
        return raw;
    }
    out
}

/// Compress `data` into one Zstandard frame: the smaller of an optimal and
/// a lazy parse.
pub fn compress(data: &[u8]) -> Vec<u8> {
    let lazy = compress_with(data, false);
    if !OPTIMAL_PARSE {
        return lazy;
    }
    let best = compress_with(data, true);
    if best.len() <= lazy.len() {
        best
    } else {
        lazy
    }
}

fn compress_with(data: &[u8], optimal: bool) -> Vec<u8> {
    let mut out = frame_header(data.len());
    if data.is_empty() {
        out.extend(block_header(true, 0, 0));
        return out;
    }
    let block_max = BLOCK_MAX.min(data.len());
    let mut m = Matcher::new(data);
    let mut reps = Reps([1, 4, 8]);
    let mut h3 = opt::Hash3::new();
    // First-block prices from a quick lazy parse of that block.
    let first = &data[..BLOCK_MAX.min(data.len())];
    let mut prices = {
        let mut r = Reps([1, 4, 8]);
        let (seqs, tail) = Matcher::new(first).parse(0, first.len(), &mut r);
        let mut lits = Vec::new();
        let mut p = 0;
        for s in &seqs {
            lits.extend_from_slice(&first[p..p + s.lit_len as usize]);
            p += (s.lit_len + s.match_len) as usize;
        }
        lits.extend_from_slice(&first[first.len() - tail..]);
        if seqs.is_empty() { opt::Prices::initial(first) } else { opt::Prices::learned(&seqs, &lits) }
    };
    let mut start = 0;
    while start < data.len() {
        let end = (start + BLOCK_MAX).min(data.len());
        let last = end == data.len();
        let block = &data[start..end];
        if block.iter().all(|&b| b == block[0]) {
            out.extend(block_header(last, 1, block.len()));
            out.push(block[0]);
            // The match finder still needs these positions indexed.
            let saved = reps;
            m.parse(start, end, &mut reps);
            reps = saved;
            start = end;
            continue;
        }
        let saved = reps;
        let (seqs, tail) = if optimal {
            opt::parse(&mut m, &mut h3, start, end, &mut reps, &prices)
        } else {
            m.parse(start, end, &mut reps)
        };
        let mut lits = Vec::with_capacity(block.len());
        let mut p = start;
        for s in &seqs {
            lits.extend_from_slice(&data[p..p + s.lit_len as usize]);
            p += (s.lit_len + s.match_len) as usize;
        }
        lits.extend_from_slice(&data[end - tail..end]);
        let mut body = literals_section(&lits);
        body.extend(seq::encode(&seqs));
        prices = opt::Prices::learned(&seqs, &lits);
        if body.len() < block.len() && body.len() <= block_max {
            out.extend(block_header(last, 2, body.len()));
            out.extend(body);
        } else {
            // Sent raw: the decoder's repeat offsets do not move.
            reps = saved;
            out.extend(block_header(last, 0, block.len()));
            out.extend_from_slice(block);
        }
        start = end;
    }
    out
}

/// One frame of Raw blocks; always valid.
pub fn store(data: &[u8]) -> Vec<u8> {
    let mut out = frame_header(data.len());
    if data.is_empty() {
        out.extend(block_header(true, 0, 0));
    }
    let mut start = 0;
    while start < data.len() {
        let end = (start + BLOCK_MAX).min(data.len());
        out.extend(block_header(end == data.len(), 0, end - start));
        out.extend_from_slice(&data[start..end]);
        start = end;
    }
    out
}
