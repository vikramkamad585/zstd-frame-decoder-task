//! LZ77 parsing: a hash-chain match finder over the whole input with
//! repeat-offset candidates and two-step lazy evaluation.

use super::bitw::highbit;
use super::seq::Seq;

pub const MIN_MATCH: usize = 4;
const HASH_LOG: u32 = 18;
const CHAIN_DEPTH: u32 = 512;
/// Stop searching once a match this long is found.
const GOOD_ENOUGH: usize = 256;
const NONE: u32 = u32::MAX;
/// Try the three repeat offsets before the hash chain.
const REP_SEARCH: bool = true;
/// Lazy evaluation steps (0 = greedy, at most 2).
const LAZY_STEPS: usize = 2;

/// Repeat-offset history exactly as the decoder keeps it.
#[derive(Clone, Copy)]
pub struct Reps(pub [u32; 3]);

impl Reps {
    /// Offset that `value` resolves to after `lit_len` literals, if valid.
    pub fn resolve(&self, value: u32, lit_len: usize) -> u32 {
        let r = self.0;
        match if lit_len == 0 { value + 1 } else { value } {
            1 => r[0],
            2 => r[1],
            3 => r[2],
            _ => r[0].wrapping_sub(1),
        }
    }

    /// Cheapest Offset_Value that reaches `offset`.
    pub fn value_for(&self, offset: u32, lit_len: usize) -> u32 {
        (1..=3).find(|&v| self.resolve(v, lit_len) == offset).unwrap_or(offset + 3)
    }

    /// Apply one sequence, mirroring `block::resolve_offset`.
    pub fn update(&mut self, value: u32, lit_len: usize) {
        let r = self.0;
        if value > 3 {
            self.0 = [value - 3, r[0], r[1]];
            return;
        }
        match if lit_len == 0 { value + 1 } else { value } {
            1 => {}
            2 => self.0 = [r[1], r[0], r[2]],
            3 => self.0 = [r[2], r[0], r[1]],
            _ => self.0 = [r[0] - 1, r[0], r[1]],
        }
    }
}

pub struct Matcher<'a> {
    data: &'a [u8],
    head: Vec<u32>,
    chain: Vec<u32>,
    next_insert: usize,
}

#[inline]
fn hash4(data: &[u8], p: usize) -> usize {
    let v = u32::from_le_bytes(data[p..p + 4].try_into().unwrap());
    (v.wrapping_mul(2654435761) >> (32 - HASH_LOG)) as usize
}

#[inline]
pub fn common(data: &[u8], a: usize, b: usize, limit: usize) -> usize {
    let max = limit - b;
    let mut n = 0;
    while n + 8 <= max {
        let x = u64::from_le_bytes(data[a + n..a + n + 8].try_into().unwrap());
        let y = u64::from_le_bytes(data[b + n..b + n + 8].try_into().unwrap());
        if x != y {
            return n + ((x ^ y).trailing_zeros() / 8) as usize;
        }
        n += 8;
    }
    while n < max && data[a + n] == data[b + n] {
        n += 1;
    }
    n
}

/// A candidate match: length, offset, and the Offset_Value it would cost.
#[derive(Clone, Copy)]
struct Cand {
    len: usize,
    offset: u32,
    value: u32,
}

impl Cand {
    fn score(&self) -> i64 {
        4 * self.len as i64 - highbit(self.value + 1) as i64
    }
}

impl<'a> Matcher<'a> {
    pub fn new(data: &'a [u8]) -> Matcher<'a> {
        Matcher { data, head: vec![NONE; 1 << HASH_LOG], chain: vec![NONE; data.len()], next_insert: 0 }
    }

    fn insert_upto(&mut self, end: usize) {
        let last = self.data.len().saturating_sub(MIN_MATCH - 1);
        while self.next_insert < end.min(last) {
            let p = self.next_insert;
            let h = hash4(self.data, p);
            self.chain[p] = self.head[h];
            self.head[h] = p as u32;
            self.next_insert += 1;
        }
    }

    /// Best match starting at `p` that ends by `limit`.
    fn search(&mut self, p: usize, limit: usize, lit_len: usize, reps: &Reps) -> Option<Cand> {
        if p + MIN_MATCH > limit {
            return None;
        }
        self.insert_upto(p);
        let mut best: Option<Cand> = None;
        let consider = |c: Cand, best: &mut Option<Cand>| {
            if c.len >= MIN_MATCH && best.map_or(true, |b| c.score() > b.score()) {
                *best = Some(c);
            }
        };
        for value in 1..=3u32 {
            if !REP_SEARCH {
                break;
            }
            let off = reps.resolve(value, lit_len) as usize;
            if off == 0 || off > p {
                continue;
            }
            let len = common(self.data, p - off, p, limit);
            consider(Cand { len, offset: off as u32, value }, &mut best);
        }
        let mut cur = self.head[hash4(self.data, p)];
        let mut depth = CHAIN_DEPTH;
        while cur != NONE && depth > 0 {
            let q = cur as usize;
            let best_len = best.map_or(0, |b| b.len);
            if best_len < limit - p && self.data[q + best_len] == self.data[p + best_len] {
                let len = common(self.data, q, p, limit);
                let offset = (p - q) as u32;
                consider(Cand { len, offset, value: reps.value_for(offset, lit_len) }, &mut best);
                if len >= GOOD_ENOUGH {
                    break;
                }
            }
            cur = self.chain[q];
            depth -= 1;
        }
        best
    }

    /// Hash-chain matches at `p` ending by `limit`, in increasing length,
    /// each longer than the one before; up to `depth` chain steps.
    pub fn candidates(&mut self, p: usize, limit: usize, depth: u32, out: &mut Vec<(u32, u32)>) {
        out.clear();
        if p + MIN_MATCH > limit {
            return;
        }
        self.insert_upto(p);
        let mut best_len = MIN_MATCH - 1;
        let mut cur = self.head[hash4(self.data, p)];
        let mut depth = depth;
        while cur != NONE && depth > 0 {
            let q = cur as usize;
            if best_len < limit - p && self.data[q + best_len] == self.data[p + best_len] {
                let len = common(self.data, q, p, limit);
                if len > best_len {
                    best_len = len;
                    out.push((len as u32, (p - q) as u32));
                    if p + len == limit {
                        break;
                    }
                }
            }
            cur = self.chain[q];
            depth -= 1;
        }
    }

    pub fn data(&self) -> &'a [u8] {
        self.data
    }

    /// Parse `data[start..end]` into sequences; returns them and the number
    /// of trailing literals. `reps` is advanced as the decoder would.
    pub fn parse(&mut self, start: usize, end: usize, reps: &mut Reps) -> (Vec<Seq>, usize) {
        let mut seqs = Vec::new();
        let mut anchor = start;
        let mut p = start;
        while p + MIN_MATCH <= end {
            let Some(mut best) = self.search(p, end, p - anchor, reps) else {
                p += 1;
                continue;
            };
            let mut at = p;
            for (step, bonus) in [(1usize, 4i64), (2, 7)].into_iter().take(LAZY_STEPS) {
                if at + 1 + MIN_MATCH > end {
                    break;
                }
                match self.search(at + 1, end, at + 1 - anchor, reps) {
                    Some(c) if c.score() > best.score() + bonus => {
                        best = c;
                        at += 1;
                    }
                    _ => {
                        if step == 1 {
                            break;
                        }
                    }
                }
            }
            p = at;
            // Extend a fresh match backwards over literals it also covers.
            if best.value > 3 {
                while p > anchor && (best.offset as usize) < p
                    && self.data[p - 1] == self.data[p - 1 - best.offset as usize]
                {
                    p -= 1;
                    best.len += 1;
                }
            }
            let lit_len = p - anchor;
            let value = reps.value_for(best.offset, lit_len);
            seqs.push(Seq { lit_len: lit_len as u32, match_len: best.len as u32, offset_value: value });
            reps.update(value, lit_len);
            p += best.len;
            anchor = p;
        }
        (seqs, end - anchor)
    }
}
