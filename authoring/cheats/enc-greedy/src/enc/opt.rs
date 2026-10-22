//! Price-driven optimal parsing. Match candidates for a block are collected
//! once; then a forward dynamic program over every position picks the
//! cheapest path, where each node holds the literal run since the last match
//! and the repeat offsets along its path. The block is parsed twice: the
//! second pass prices symbols with statistics learned from the first.

use super::matcher::{common, Matcher, Reps};
use super::fse::log2_fixed;
use super::seq::{ll_code, ml_code, of_code, Seq};
use crate::fse::{LL_DEFAULT, ML_DEFAULT, OF_DEFAULT};

/// Matches at least this long are taken without weighing shorter ones.
const SUFFICIENT_LEN: usize = 128;
const CHAIN_DEPTH: u32 = 16;
/// Parsing passes per block; later passes use the previous pass's statistics.
const PASSES: usize = 2;
/// Three-byte matches are worth it only close by.
const SHORT_MATCH_MAX_OFFSET: usize = 1 << 14;
const HASH3_LOG: u32 = 16;
/// Prices are in 1/256 bit.
const BIT: u32 = 256;
const INF: u32 = u32::MAX;


fn price_table(counts: &[u32]) -> Vec<u32> {
    let total: u32 = counts.iter().map(|&c| c + 1).sum();
    let lt = log2_fixed(total);
    counts.iter().map(|&c| lt - log2_fixed(c + 1).min(lt) + BIT / 8).collect()
}

pub struct Prices {
    lit: Vec<u32>,
    ll: Vec<u32>,
    ml: Vec<u32>,
    of: Vec<u32>,
}

impl Prices {
    /// Prices with no history: the block's own bytes for literals, the
    /// predefined distributions for codes.
    pub fn initial(block: &[u8]) -> Prices {
        let mut lit = vec![0u32; 256];
        for &b in block {
            lit[b as usize] += 1;
        }
        let def = |d: &[i16], n: usize| -> Vec<u32> {
            let mut v: Vec<u32> = d.iter().map(|&c| if c < 0 { 1 } else { c as u32 }).collect();
            v.resize(n, 0);
            v
        };
        Prices {
            lit: price_table(&lit),
            ll: price_table(&def(&LL_DEFAULT, 36)),
            ml: price_table(&def(&ML_DEFAULT, 53)),
            of: price_table(&def(&OF_DEFAULT, 32)),
        }
    }

    /// Prices learned from chosen sequences and their literals.
    pub fn learned(seqs: &[Seq], lits: &[u8]) -> Prices {
        let mut lit = vec![0u32; 256];
        for &b in lits {
            lit[b as usize] += 1;
        }
        let (mut ll, mut ml, mut of) = (vec![0u32; 36], vec![0u32; 53], vec![0u32; 32]);
        for s in seqs {
            ll[ll_code(s.lit_len).0 as usize] += 1;
            ml[ml_code(s.match_len).0 as usize] += 1;
            of[of_code(s.offset_value).0 as usize] += 1;
        }
        Prices { lit: price_table(&lit), ll: price_table(&ll), ml: price_table(&ml), of: price_table(&of) }
    }

    #[inline]
    fn lit_len(&self, n: u32) -> u32 {
        let (c, _, bits) = ll_code(n);
        self.ll[c as usize] + bits * BIT
    }

    #[inline]
    fn matched(&self, len: u32, value: u32) -> u32 {
        let (mc, _, mbits) = ml_code(len);
        let (oc, _, obits) = of_code(value);
        self.ml[mc as usize] + mbits * BIT + self.of[oc as usize] + obits * BIT
    }
}

/// Match candidates for every position of a block, in increasing length.
struct Candidates {
    start: Vec<u32>,
    list: Vec<(u32, u32)>,
    /// Positions inside a long match: no search, only the match's tail.
    long_from: Vec<bool>,
}

/// Most recent position for each three-byte hash, across the whole input.
pub struct Hash3 {
    head: Vec<u32>,
    next: usize,
}

impl Hash3 {
    pub fn new() -> Hash3 {
        Hash3 { head: vec![u32::MAX; 1 << HASH3_LOG], next: 0 }
    }

    fn hash(data: &[u8], p: usize) -> usize {
        let v = data[p] as u32 | (data[p + 1] as u32) << 8 | (data[p + 2] as u32) << 16;
        (v.wrapping_mul(506832829) >> (32 - HASH3_LOG)) as usize
    }

    /// Candidate before `p`, after indexing everything below `p`.
    fn find(&mut self, data: &[u8], p: usize) -> Option<usize> {
        while self.next < p && self.next + 3 <= data.len() {
            self.head[Self::hash(data, self.next)] = self.next as u32;
            self.next += 1;
        }
        if p + 3 > data.len() {
            return None;
        }
        let q = self.head[Self::hash(data, p)];
        (q != u32::MAX).then_some(q as usize)
    }
}

fn collect(m: &mut Matcher, h3: &mut Hash3, start: usize, end: usize) -> Candidates {
    let data = m.data();
    let n = end - start;
    let mut c = Candidates { start: Vec::with_capacity(n + 1), list: Vec::new(), long_from: vec![false; n] };
    let mut buf = Vec::new();
    let mut long: Option<(usize, u32)> = None; // (end of long match, offset)
    for cur in 0..n {
        let p = start + cur;
        c.start.push(c.list.len() as u32);
        if let Some((until, off)) = long {
            if p + 3 <= until {
                c.long_from[cur] = true;
                c.list.push(((until - p) as u32, off));
                continue;
            }
            long = None;
        }
        let mut short = None;
        if p + 3 <= end {
            if let Some(q) = h3.find(data, p) {
                if p - q <= SHORT_MATCH_MAX_OFFSET && data[q..q + 3] == data[p..p + 3] {
                    short = Some((common(data, q, p, end) as u32, (p - q) as u32));
                }
            }
        }
        m.candidates(p, end, CHAIN_DEPTH, &mut buf);
        if let Some((len, off)) = short {
            if buf.first().map_or(true, |&(l, _)| l > len) {
                c.list.push((len, off));
            }
        }
        c.list.extend_from_slice(&buf);
        if let Some(&(len, off)) = c.list[c.start[cur] as usize..].last() {
            if len as usize >= SUFFICIENT_LEN {
                long = Some((p + len as usize, off));
            }
        }
    }
    c.start.push(c.list.len() as u32);
    c
}

#[derive(Clone, Copy)]
struct Node {
    price: u32,
    /// Literals since the last match on the path to this node.
    lit_run: u32,
    /// How the node was reached: 0 = literal, else a match of this length.
    mlen: u32,
    offset: u32,
    reps: Reps,
}

fn dp(data: &[u8], start: usize, end: usize, cands: &Candidates, reps: Reps, prices: &Prices) -> Vec<(usize, u32, u32)> {
    let n = end - start;
    let unreached = Node { price: INF, lit_run: 0, mlen: 0, offset: 0, reps };
    let mut opt = vec![unreached; n + 1];
    opt[0] = Node { price: prices.lit_len(0), lit_run: 0, mlen: 0, offset: 0, reps };
    for cur in 0..n {
        let node = opt[cur];
        if node.price == INF {
            continue;
        }
        let p = start + cur;
        let lp = node.price + prices.lit[data[p] as usize] + prices.lit_len(node.lit_run + 1)
            - prices.lit_len(node.lit_run);
        if lp < opt[cur + 1].price {
            opt[cur + 1] = Node { price: lp, lit_run: node.lit_run + 1, mlen: 0, offset: 0, reps: node.reps };
        }
        let base = node.price + prices.lit_len(0);
        let relax = |len: usize, offset: u32, opt: &mut Vec<Node>| {
            let value = node.reps.value_for(offset, node.lit_run as usize);
            let price = base + prices.matched(len as u32, value);
            let to = cur + len;
            if price < opt[to].price {
                let mut r = node.reps;
                r.update(value, node.lit_run as usize);
                opt[to] = Node { price, lit_run: 0, mlen: len as u32, offset, reps: r };
            }
        };
        let list = &cands.list[cands.start[cur] as usize..cands.start[cur + 1] as usize];
        if cands.long_from[cur] {
            if let Some(&(len, off)) = list.first().filter(|c| c.0 >= 3) {
                relax(len as usize, off, &mut opt);
            }
            continue;
        }
        // Repeat offsets along this node's path.
        for value in 1..=3u32 {
            let off = node.reps.resolve(value, node.lit_run as usize) as usize;
            if off == 0 || off > p {
                continue;
            }
            let len = common(data, p - off, p, end).min(SUFFICIENT_LEN);
            for l in 3..=len {
                relax(l, off as u32, &mut opt);
            }
        }
        if let Some(&(len, off)) = list.last() {
            if len as usize >= SUFFICIENT_LEN {
                relax(len as usize, off, &mut opt);
                continue;
            }
        }
        let mut from = 3;
        for &(len, off) in list {
            for l in from..=len as usize {
                relax(l, off, &mut opt);
            }
            from = len as usize + 1;
        }
    }
    let mut steps = Vec::new();
    let mut i = n;
    while i > 0 {
        let nd = opt[i];
        if nd.mlen == 0 {
            i -= 1;
        } else {
            steps.push((i - nd.mlen as usize, nd.mlen, nd.offset));
            i -= nd.mlen as usize;
        }
    }
    steps.reverse();
    steps
}

fn to_seqs(steps: &[(usize, u32, u32)], n: usize, reps: &mut Reps) -> (Vec<Seq>, usize) {
    let mut seqs = Vec::with_capacity(steps.len());
    let mut anchor = 0usize;
    for &(at, len, off) in steps {
        let lit_len = at - anchor;
        let value = reps.value_for(off, lit_len);
        reps.update(value, lit_len);
        seqs.push(Seq { lit_len: lit_len as u32, match_len: len, offset_value: value });
        anchor = at + len as usize;
    }
    (seqs, n - anchor)
}

fn literals_of(data: &[u8], start: usize, end: usize, seqs: &[Seq], tail: usize) -> Vec<u8> {
    let mut lits = Vec::new();
    let mut p = start;
    for s in seqs {
        lits.extend_from_slice(&data[p..p + s.lit_len as usize]);
        p += (s.lit_len + s.match_len) as usize;
    }
    lits.extend_from_slice(&data[end - tail..end]);
    lits
}

/// Optimal-parse `data[start..end]`. Returns the sequences and the count of
/// trailing literals; `reps` is advanced along the chosen path.
pub fn parse(m: &mut Matcher, h3: &mut Hash3, start: usize, end: usize, reps: &mut Reps, prices: &Prices) -> (Vec<Seq>, usize) {
    let data = m.data();
    let cands = collect(m, h3, start, end);
    let n = end - start;
    let mut learned: Option<Prices> = None;
    let mut result = (Vec::new(), n);
    for pass in 0..PASSES {
        let steps = dp(data, start, end, &cands, *reps, learned.as_ref().unwrap_or(prices));
        let mut r = *reps;
        result = to_seqs(&steps, n, &mut r);
        if pass + 1 == PASSES {
            *reps = r;
            break;
        }
        learned = Some(Prices::learned(&result.0, &literals_of(data, start, end, &result.0, result.1)));
    }
    result
}
