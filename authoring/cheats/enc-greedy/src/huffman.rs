//! Huffman literal decoding: tree description (direct or FSE-compressed
//! weights), decoding-table construction and 1- or 4-stream decoding.

use crate::bits::BackwardBits;
use crate::error::Result;
use crate::fse;
use crate::{bail, ensure};

const MAX_BITS: u32 = 11;

#[derive(Clone)]
pub struct HuffTable {
    pub max_bits: u32,
    /// Indexed by the next `max_bits` bits of the stream: (symbol, length).
    pub entries: Vec<(u8, u8)>,
}

impl HuffTable {
    /// Parse a Huffman tree description at the start of `data`; returns the
    /// table and the bytes consumed.
    pub fn read(data: &[u8]) -> Result<(HuffTable, usize)> {
        ensure!(!data.is_empty(), "missing Huffman tree description");
        let header = data[0] as usize;
        let (weights, used) = if header < 128 {
            let size = header;
            ensure!(size > 0 && 1 + size <= data.len(), "Huffman weight stream truncated");
            let body = &data[1..1 + size];
            let (table, desc_len) = fse::Table::read_description(body, 255, 6)?;
            ensure!(desc_len < size, "Huffman weight stream has no payload");
            let w = fse::decode_interleaved(&table, &body[desc_len..], 256)?;
            (w, 1 + size)
        } else {
            let count = header - 127;
            let bytes = (count + 1) / 2;
            ensure!(1 + bytes <= data.len(), "Huffman direct weights truncated");
            let mut w = Vec::with_capacity(count);
            for i in 0..count {
                let b = data[1 + i / 2];
                w.push(if i % 2 == 0 { b >> 4 } else { b & 0xF });
            }
            (w, 1 + bytes)
        };
        Ok((HuffTable::from_weights(&weights)?, used))
    }

    fn from_weights(weights: &[u8]) -> Result<HuffTable> {
        ensure!(weights.len() <= 255, "too many Huffman weights");
        let mut sum: u32 = 0;
        for &w in weights {
            ensure!(w as u32 <= MAX_BITS, "Huffman weight {} too large", w);
            if w > 0 {
                sum += 1 << (w - 1);
            }
        }
        ensure!(sum > 0, "Huffman weights are all zero");
        let max_bits = 32 - sum.leading_zeros();
        ensure!(max_bits <= MAX_BITS, "Huffman code too long");
        let total = 1u32 << max_bits;
        let rest = total - sum;
        ensure!(rest.is_power_of_two(), "Huffman weights do not form a complete tree");
        let last = rest.trailing_zeros() as u8 + 1;
        let mut all: Vec<u8> = weights.to_vec();
        all.push(last);

        let mut counts = [0u32; (MAX_BITS + 1) as usize];
        for &w in &all {
            counts[w as usize] += 1;
        }
        let mut start = [0u32; (MAX_BITS + 2) as usize];
        let mut next = 0u32;
        for w in 1..=max_bits as usize {
            start[w] = next;
            next += counts[w] << (w - 1);
        }
        ensure!(next == total, "Huffman table size mismatch");
        let mut entries = vec![(0u8, 0u8); total as usize];
        for (s, &w) in all.iter().enumerate() {
            if w == 0 {
                continue;
            }
            let len = 1u32 << (w - 1);
            let nb = (max_bits + 1 - w as u32) as u8;
            let st = start[w as usize];
            for e in &mut entries[st as usize..(st + len) as usize] {
                *e = (s as u8, nb);
            }
            start[w as usize] += len;
        }
        Ok(HuffTable { max_bits, entries })
    }

    fn decode_stream(&self, data: &[u8], out: &mut Vec<u8>, count: usize) -> Result<()> {
        let mut br = BackwardBits::new(data)?;
        for _ in 0..count {
            let (sym, nb) = self.entries[br.peek(self.max_bits) as usize];
            br.consume(nb as u32);
            out.push(sym);
        }
        ensure!(br.remaining() == 0, "Huffman stream not fully consumed");
        Ok(())
    }

    /// Decode `regen` literals from one stream or four streams with a jump table.
    pub fn decode(&self, data: &[u8], regen: usize, four: bool) -> Result<Vec<u8>> {
        let mut out = Vec::with_capacity(regen);
        if !four {
            self.decode_stream(data, &mut out, regen)?;
            return Ok(out);
        }
        ensure!(data.len() >= 10, "four-stream literals too short");
        let s1 = u16::from_le_bytes([data[0], data[1]]) as usize;
        let s2 = u16::from_le_bytes([data[2], data[3]]) as usize;
        let s3 = u16::from_le_bytes([data[4], data[5]]) as usize;
        let total = data.len() - 6;
        if s1 + s2 + s3 > total {
            bail!("Huffman jump table exceeds literal size");
        }
        let s4 = total - s1 - s2 - s3;
        ensure!(s1 > 0 && s2 > 0 && s3 > 0 && s4 > 0, "empty Huffman stream");
        let seg = (regen + 3) / 4;
        ensure!(regen >= 3 * seg, "too few literals for four streams");
        let last = regen - 3 * seg;
        let mut p = 6;
        for (size, n) in [(s1, seg), (s2, seg), (s3, seg), (s4, last)] {
            self.decode_stream(&data[p..p + size], &mut out, n)?;
            p += size;
        }
        Ok(out)
    }
}
