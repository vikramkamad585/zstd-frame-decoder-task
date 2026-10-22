//! Literals section (RFC 8878 §3.1.1.3.1).

use crate::error::Result;
use crate::huffman::HuffTable;
use crate::{bail, ensure};

/// Decode the literals section at the start of `data`. Updates `huff` when a
/// new tree is transmitted. Returns the literals and the bytes consumed.
pub fn decode(data: &[u8], huff: &mut Option<HuffTable>, block_max: usize) -> Result<(Vec<u8>, usize)> {
    ensure!(!data.is_empty(), "missing literals section");
    let b0 = data[0] as usize;
    let ltype = b0 & 3;
    let sf = (b0 >> 2) & 3;
    match ltype {
        0 | 1 => {
            let (regen, hsize) = match sf {
                0 | 2 => (b0 >> 3, 1),
                1 => {
                    ensure!(data.len() >= 2, "literals header truncated");
                    ((b0 >> 4) + ((data[1] as usize) << 4), 2)
                }
                _ => {
                    ensure!(data.len() >= 3, "literals header truncated");
                    ((b0 >> 4) + ((data[1] as usize) << 4) + ((data[2] as usize) << 12), 3)
                }
            };
            ensure!(regen <= block_max, "literals exceed block maximum size");
            if ltype == 0 {
                ensure!(data.len() >= hsize + regen, "raw literals truncated");
                Ok((data[hsize..hsize + regen].to_vec(), hsize + regen))
            } else {
                ensure!(data.len() > hsize, "RLE literals truncated");
                Ok((vec![data[hsize]; regen], hsize + 1))
            }
        }
        _ => {
            let (hsize, bits, four) = match sf {
                0 => (3, 10, false),
                1 => (3, 10, true),
                2 => (4, 14, true),
                _ => (5, 18, true),
            };
            ensure!(data.len() >= hsize, "literals header truncated");
            let mut h: u64 = 0;
            for i in 0..hsize {
                h |= (data[i] as u64) << (8 * i);
            }
            let mask = (1u64 << bits) - 1;
            let regen = ((h >> 4) & mask) as usize;
            let csize = ((h >> (4 + bits)) & mask) as usize;
            ensure!(regen <= block_max, "literals exceed block maximum size");
            ensure!(data.len() >= hsize + csize, "compressed literals truncated");
            let mut body = &data[hsize..hsize + csize];
            if ltype == 2 {
                let (table, used) = HuffTable::read(body)?;
                body = &body[used..];
                *huff = Some(table);
            }
            let Some(table) = huff.as_ref() else {
                bail!("treeless literals without a previous Huffman table");
            };
            let lits = table.decode(body, regen, four)?;
            Ok((lits, hsize + csize))
        }
    }
}
