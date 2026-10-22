//! Bit readers.
//!
//! `ForwardBits` reads little-endian bit fields from the start of a buffer
//! (FSE table descriptions). `BackwardBits` reads the reversed bitstreams used
//! by Huffman and FSE payloads: the stream starts at the highest set bit of
//! the final byte and is consumed toward byte 0.

use crate::error::Result;
use crate::{bail, ensure};

#[inline]
fn load_le(data: &[u8], idx: usize) -> u64 {
    if idx + 8 <= data.len() {
        u64::from_le_bytes(data[idx..idx + 8].try_into().unwrap())
    } else {
        let mut v = 0u64;
        for (k, &b) in data[idx.min(data.len())..].iter().enumerate() {
            v |= (b as u64) << (8 * k);
        }
        v
    }
}

/// Value of the `n` bits (n <= 56) starting at absolute bit `start`.
/// Positions below zero read as zero.
#[inline]
fn bits_at(data: &[u8], start: i64, n: u32) -> u64 {
    if n == 0 {
        return 0;
    }
    let mask = (1u64 << n) - 1;
    if start >= 0 {
        let s = start as usize;
        (load_le(data, s / 8) >> (s % 8)) & mask
    } else {
        let top = start + n as i64;
        if top <= 0 {
            return 0;
        }
        (load_le(data, 0) & ((1u64 << top) - 1)) << (-start) as u32 & mask
    }
}

pub struct ForwardBits<'a> {
    data: &'a [u8],
    pos: usize,
}

impl<'a> ForwardBits<'a> {
    pub fn new(data: &'a [u8]) -> Self {
        ForwardBits { data, pos: 0 }
    }

    pub fn peek(&self, n: u32) -> u64 {
        bits_at(self.data, self.pos as i64, n)
    }

    pub fn skip(&mut self, n: u32) -> Result<()> {
        self.pos += n as usize;
        ensure!(self.pos <= self.data.len() * 8, "bitstream overrun in table description");
        Ok(())
    }

    pub fn read(&mut self, n: u32) -> Result<u64> {
        let v = self.peek(n);
        self.skip(n)?;
        Ok(v)
    }

    pub fn bytes_consumed(&self) -> usize {
        (self.pos + 7) / 8
    }
}

pub struct BackwardBits<'a> {
    data: &'a [u8],
    /// Number of unread bits; may go negative once the stream is overread.
    pos: i64,
}

impl<'a> BackwardBits<'a> {
    pub fn new(data: &'a [u8]) -> Result<Self> {
        let Some(&last) = data.last() else {
            bail!("empty backward bitstream");
        };
        ensure!(last != 0, "backward bitstream has no end marker");
        let padding = last.leading_zeros() as i64 + 1;
        Ok(BackwardBits { data, pos: data.len() as i64 * 8 - padding })
    }

    #[inline]
    pub fn read(&mut self, n: u32) -> u64 {
        if n == 0 {
            return 0;
        }
        self.pos -= n as i64;
        bits_at(self.data, self.pos, n)
    }

    #[inline]
    pub fn peek(&self, n: u32) -> u64 {
        bits_at(self.data, self.pos - n as i64, n)
    }

    #[inline]
    pub fn consume(&mut self, n: u32) {
        self.pos -= n as i64;
    }

    pub fn remaining(&self) -> i64 {
        self.pos
    }

    pub fn overflowed(&self) -> bool {
        self.pos < 0
    }
}
