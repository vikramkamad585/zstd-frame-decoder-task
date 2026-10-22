//! Little-endian bit writer. Forward streams (FSE table descriptions) are
//! read back in write order; backward streams (Huffman and FSE payloads)
//! are closed with a 1 bit and read from that marker toward byte 0, so the
//! last value written is the first one read.

pub struct BitWriter {
    buf: Vec<u8>,
    acc: u64,
    n: u32,
}

impl BitWriter {
    pub fn new() -> BitWriter {
        BitWriter { buf: Vec::new(), acc: 0, n: 0 }
    }

    /// Append the low `nb` bits of `v` (nb <= 32).
    #[inline]
    pub fn add(&mut self, v: u64, nb: u32) {
        if nb == 0 {
            return;
        }
        self.acc |= (v & ((1u64 << nb) - 1)) << self.n;
        self.n += nb;
        while self.n >= 8 {
            self.buf.push(self.acc as u8);
            self.acc >>= 8;
            self.n -= 8;
        }
    }

    /// Pad a forward stream to a whole byte.
    pub fn finish(mut self) -> Vec<u8> {
        if self.n > 0 {
            self.buf.push(self.acc as u8);
        }
        self.buf
    }

    /// End a backward stream: the 1-bit marker, then padding.
    pub fn close(mut self) -> Vec<u8> {
        self.add(1, 1);
        self.finish()
    }
}

#[inline]
pub fn highbit(v: u32) -> u32 {
    31 - v.leading_zeros()
}
