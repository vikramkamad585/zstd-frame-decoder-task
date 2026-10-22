//! Decoded history: optional dictionary content followed by everything this
//! frame has produced. Enforces the Window_Size reach rules of RFC 8878.

use crate::error::Result;
use crate::ensure;

pub struct History<'d> {
    dict: &'d [u8],
    pub out: Vec<u8>,
    frame_start: usize,
    window: u64,
}

impl<'d> History<'d> {
    pub fn new(dict: &'d [u8], out: Vec<u8>, window: u64) -> History<'d> {
        let frame_start = out.len();
        History { dict, out, frame_start, window }
    }

    pub fn frame_len(&self) -> usize {
        self.out.len() - self.frame_start
    }

    pub fn frame_output(&self) -> &[u8] {
        &self.out[self.frame_start..]
    }

    pub fn into_output(self) -> Vec<u8> {
        self.out
    }

    pub fn push_slice(&mut self, s: &[u8]) {
        self.out.extend_from_slice(s);
    }

    pub fn push_rle(&mut self, b: u8, n: usize) {
        self.out.resize(self.out.len() + n, b);
    }

    pub fn copy_match(&mut self, offset: usize, len: usize) -> Result<()> {
        let produced = self.frame_len();
        ensure!(offset > 0, "zero match offset");
        ensure!(
            offset > produced || offset as u64 <= self.window,
            "match offset {} beyond window",
            offset
        );
        let mut remaining = len;
        if offset > produced {
            let back = offset - produced;
            ensure!(back <= self.dict.len(), "match offset {} reaches before available history", offset);
            ensure!(produced as u64 <= self.window, "dictionary no longer reachable");
            let from = self.dict.len() - back;
            let take = remaining.min(back);
            self.out.extend_from_slice(&self.dict[from..from + take]);
            remaining -= take;
            if remaining == 0 {
                return Ok(());
            }
        }
        let src = self.out.len() - offset;
        if offset >= remaining {
            self.out.extend_from_within(src..src + remaining);
        } else {
            self.out.reserve(remaining);
            for k in 0..remaining {
                let b = self.out[src + k];
                self.out.push(b);
            }
        }
        Ok(())
    }
}
