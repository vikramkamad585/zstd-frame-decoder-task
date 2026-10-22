//! Compressed-block decoding and sequence execution (RFC 8878 §3.1.1.3–4).

use crate::error::Result;
use crate::huffman::HuffTable;
use crate::sequences::{self, SeqTables};
use crate::window::History;
use crate::{ensure, literals};

/// Entropy and offset state that persists across the blocks of a frame.
pub struct BlockState {
    pub huff: Option<HuffTable>,
    pub tables: SeqTables,
    pub reps: [u32; 3],
}

pub fn decode_compressed(
    data: &[u8],
    st: &mut BlockState,
    hist: &mut History,
    block_max: usize,
) -> Result<()> {
    let (lits, used) = literals::decode(data, &mut st.huff, block_max)?;
    let seqs = sequences::decode(&data[used..], &mut st.tables)?;
    let start = hist.frame_len();
    let mut lit_pos = 0usize;
    for s in &seqs {
        let ll = s.literal_length as usize;
        let ml = s.match_length as usize;
        ensure!(lit_pos + ll <= lits.len(), "sequence uses more literals than decoded");
        hist.push_slice(&lits[lit_pos..lit_pos + ll]);
        lit_pos += ll;

        let offset = resolve_offset(s.offset_value, s.literal_length, &mut st.reps)?;
        hist.copy_match(offset as usize, ml)?;
        ensure!(hist.frame_len() - start <= block_max, "block decodes past its maximum size");
    }
    hist.push_slice(&lits[lit_pos..]);
    ensure!(hist.frame_len() - start <= block_max, "block decodes past its maximum size");
    Ok(())
}

fn resolve_offset(value: u32, literal_length: u32, reps: &mut [u32; 3]) -> Result<u32> {
    if value > 3 {
        let off = value - 3;
        *reps = [off, reps[0], reps[1]];
        return Ok(off);
    }
    let idx = if literal_length == 0 { value + 1 } else { value };
    let off = match idx {
        1 => reps[0],
        2 => {
            let o = reps[1];
            *reps = [o, reps[0], reps[2]];
            o
        }
        3 => {
            let o = reps[2];
            *reps = [o, reps[0], reps[1]];
            o
        }
        _ => {
            let o = reps[0].wrapping_sub(1);
            ensure!(o != 0, "repeat offset resolves to zero");
            *reps = [o, reps[0], reps[1]];
            o
        }
    };
    Ok(off)
}
