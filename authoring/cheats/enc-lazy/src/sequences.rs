//! Sequences section (RFC 8878 §3.1.1.3.2): header, table selection and
//! sequence decoding.

use crate::bits::BackwardBits;
use crate::error::Result;
use crate::fse::{self, State, Table};
use crate::{bail, ensure};

#[derive(Clone, Copy, Debug)]
pub struct Sequence {
    pub literal_length: u32,
    pub match_length: u32,
    /// Raw Offset_Value before repeat-offset resolution.
    pub offset_value: u32,
}

const LL_BASE: [(u32, u32); 36] = [
    (0, 0), (1, 0), (2, 0), (3, 0), (4, 0), (5, 0), (6, 0), (7, 0), (8, 0), (9, 0), (10, 0),
    (11, 0), (12, 0), (13, 0), (14, 0), (15, 0), (16, 1), (18, 1), (20, 1), (22, 1), (24, 2),
    (28, 2), (32, 3), (40, 3), (48, 4), (64, 6), (128, 7), (256, 8), (512, 9), (1024, 10),
    (2048, 11), (4096, 12), (8192, 13), (16384, 14), (32768, 15), (65536, 16),
];

const ML_BASE: [(u32, u32); 53] = [
    (3, 0), (4, 0), (5, 0), (6, 0), (7, 0), (8, 0), (9, 0), (10, 0), (11, 0), (12, 0), (13, 0),
    (14, 0), (15, 0), (16, 0), (17, 0), (18, 0), (19, 0), (20, 0), (21, 0), (22, 0), (23, 0),
    (24, 0), (25, 0), (26, 0), (27, 0), (28, 0), (29, 0), (30, 0), (31, 0), (32, 0), (33, 0),
    (34, 0), (35, 1), (37, 1), (39, 1), (41, 1), (43, 2), (47, 2), (51, 3), (59, 3), (67, 4),
    (83, 4), (99, 5), (131, 7), (259, 8), (515, 9), (1027, 10), (2051, 11), (4099, 12),
    (8195, 13), (16387, 14), (32771, 15), (65539, 16),
];

/// Tables carried between blocks (and seeded by a dictionary).
#[derive(Clone, Default)]
pub struct SeqTables {
    pub ll: Option<Table>,
    pub of: Option<Table>,
    pub ml: Option<Table>,
}

fn select(
    mode: u8,
    data: &[u8],
    pos: &mut usize,
    prev: &mut Option<Table>,
    default: (&[i16], u32),
    max_symbol: usize,
    max_log: u32,
) -> Result<()> {
    match mode {
        0 => *prev = Some(fse::predefined(default.0, default.1)),
        1 => {
            ensure!(*pos < data.len(), "RLE symbol missing");
            let s = data[*pos];
            ensure!(s as usize <= max_symbol, "RLE symbol out of range");
            *pos += 1;
            *prev = Some(Table::rle(s));
        }
        2 => {
            let (t, used) = Table::read_description(&data[*pos..], max_symbol, max_log)?;
            *pos += used;
            *prev = Some(t);
        }
        _ => ensure!(prev.is_some(), "repeat mode without a previous table"),
    }
    Ok(())
}

/// Decode the sequences section occupying all of `data`.
pub fn decode(data: &[u8], tables: &mut SeqTables) -> Result<Vec<Sequence>> {
    ensure!(!data.is_empty(), "missing sequences section header");
    let b0 = data[0] as usize;
    let (count, mut pos) = if b0 < 128 {
        (b0, 1)
    } else if b0 < 255 {
        ensure!(data.len() >= 2, "sequence count truncated");
        (((b0 - 128) << 8) + data[1] as usize, 2)
    } else {
        ensure!(data.len() >= 3, "sequence count truncated");
        (data[1] as usize + ((data[2] as usize) << 8) + 0x7F00, 3)
    };
    if count == 0 {
        ensure!(pos == data.len(), "trailing bytes after empty sequences section");
        return Ok(Vec::new());
    }
    ensure!(pos < data.len(), "compression modes byte missing");
    let modes = data[pos];
    pos += 1;
    ensure!(modes & 3 == 0, "reserved bits set in compression modes");
    select(modes >> 6, data, &mut pos, &mut tables.ll, (&fse::LL_DEFAULT, 6), 35, 9)?;
    select((modes >> 4) & 3, data, &mut pos, &mut tables.of, (&fse::OF_DEFAULT, 5), 31, 8)?;
    select((modes >> 2) & 3, data, &mut pos, &mut tables.ml, (&fse::ML_DEFAULT, 6), 52, 9)?;
    ensure!(pos < data.len(), "sequence bitstream missing");

    let (ll_t, of_t, ml_t) = match (&tables.ll, &tables.of, &tables.ml) {
        (Some(a), Some(b), Some(c)) => (a, b, c),
        _ => bail!("sequence tables unavailable"),
    };
    let mut br = BackwardBits::new(&data[pos..])?;
    let mut ll = State::new(ll_t, &mut br);
    let mut of = State::new(of_t, &mut br);
    let mut ml = State::new(ml_t, &mut br);
    let mut out = Vec::with_capacity(count);
    for i in 0..count {
        let of_code = of.symbol() as u32;
        let ml_code = ml.symbol() as usize;
        let ll_code = ll.symbol() as usize;
        ensure!(of_code <= 31, "offset code out of range");
        ensure!(ml_code < ML_BASE.len() && ll_code < LL_BASE.len(), "length code out of range");
        let offset_value = (1u64 << of_code) as u32 + br.read(of_code) as u32;
        let (mb, mbits) = ML_BASE[ml_code];
        let match_length = mb + br.read(mbits) as u32;
        let (lb, lbits) = LL_BASE[ll_code];
        let literal_length = lb + br.read(lbits) as u32;
        out.push(Sequence { literal_length, match_length, offset_value });
        if i + 1 < count {
            ll.update(&mut br);
            ml.update(&mut br);
            of.update(&mut br);
        }
        ensure!(!br.overflowed(), "sequence bitstream overread");
    }
    ensure!(br.remaining() == 0, "sequence bitstream not fully consumed");
    Ok(out)
}
