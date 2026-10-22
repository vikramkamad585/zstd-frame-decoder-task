//! Frame layer: magic numbers, frame header, block loop, content checksum
//! and skippable frames (RFC 8878 §3.1).

use crate::block::{self, BlockState};
use crate::dict::Dictionary;
use crate::error::Result;
use crate::window::History;
use crate::xxhash64::xxh64;
use crate::{bail, ensure};

pub const FRAME_MAGIC: u32 = 0xFD2FB528;
pub const MAX_WINDOW: u64 = 1 << 27;
const BLOCK_SIZE_MAX: u64 = 128 * 1024;

pub struct FrameHeader {
    pub window_size: u64,
    pub content_size: Option<u64>,
    pub dict_id: u32,
    pub checksum: bool,
    pub header_len: usize,
}

fn le(data: &[u8]) -> u64 {
    data.iter().rev().fold(0u64, |acc, &b| (acc << 8) | b as u64)
}

/// Parse the frame header that follows the magic number.
pub fn parse_header(data: &[u8]) -> Result<FrameHeader> {
    ensure!(!data.is_empty(), "frame header truncated");
    let fhd = data[0];
    let fcs_flag = fhd >> 6;
    let single = (fhd >> 5) & 1 == 1;
    // Bit 4 is unused and must be ignored.
    ensure!((fhd >> 3) & 1 == 0, "reserved bit set in frame header");
    let checksum = (fhd >> 2) & 1 == 1;
    let did_size = [0usize, 1, 2, 4][(fhd & 3) as usize];
    let fcs_size = match fcs_flag {
        0 => usize::from(single),
        1 => 2,
        2 => 4,
        _ => 8,
    };
    let wd_size = usize::from(!single);
    let header_len = 1 + wd_size + did_size + fcs_size;
    ensure!(data.len() >= header_len, "frame header truncated");
    let mut pos = 1;
    let mut window_size = 0u64;
    if !single {
        let wd = data[pos];
        let exponent = (wd >> 3) as u64;
        let mantissa = (wd & 7) as u64;
        let base = 1u64 << (10 + exponent);
        window_size = base + (base / 8) * mantissa;
        pos += 1;
    }
    let dict_id = le(&data[pos..pos + did_size]) as u32;
    pos += did_size;
    let content_size = if fcs_size == 0 {
        None
    } else {
        let v = le(&data[pos..pos + fcs_size]);
        Some(if fcs_size == 2 { v + 256 } else { v })
    };
    if single {
        window_size = content_size.unwrap_or(0);
    }
    ensure!(window_size <= MAX_WINDOW, "window size {} exceeds the 128 MiB limit", window_size);
    Ok(FrameHeader { window_size, content_size, dict_id, checksum, header_len })
}

/// Decode one Zstandard frame starting at `data` (just past the magic
/// number). Appends to `out` and returns it with the bytes consumed.
pub fn decode_frame(data: &[u8], dict: Option<&Dictionary>, out: Vec<u8>) -> Result<(Vec<u8>, usize)> {
    let h = parse_header(data)?;
    let dict = match (h.dict_id, dict) {
        (0, d) => d,
        (id, Some(d)) if d.id == id => Some(d),
        (id, Some(d)) => bail!("frame needs dictionary {} but {} was given", id, d.id),
        (id, None) => bail!("frame needs dictionary {}", id),
    };
    let block_max = h.window_size.min(BLOCK_SIZE_MAX) as usize;
    let mut st = match dict {
        Some(d) => BlockState { huff: d.huff.clone(), tables: d.tables.clone(), reps: d.reps },
        None => BlockState { huff: None, tables: Default::default(), reps: [1, 4, 8] },
    };
    let content: &[u8] = dict.map(|d| d.content.as_slice()).unwrap_or(&[]);
    let mut hist = History::new(content, out, h.window_size);
    let mut pos = h.header_len;
    loop {
        ensure!(data.len() >= pos + 3, "block header truncated");
        let bh = le(&data[pos..pos + 3]) as u32;
        pos += 3;
        let last = bh & 1 == 1;
        let btype = (bh >> 1) & 3;
        let bsize = (bh >> 3) as usize;
        match btype {
            0 => {
                ensure!(bsize <= block_max, "raw block exceeds maximum size");
                ensure!(data.len() >= pos + bsize, "raw block truncated");
                hist.push_slice(&data[pos..pos + bsize]);
                pos += bsize;
            }
            1 => {
                ensure!(bsize <= block_max, "RLE block exceeds maximum size");
                ensure!(data.len() > pos, "RLE block truncated");
                hist.push_rle(data[pos], bsize);
                pos += 1;
            }
            2 => {
                ensure!(bsize <= block_max, "compressed block exceeds maximum size");
                ensure!(data.len() >= pos + bsize, "compressed block truncated");
                block::decode_compressed(&data[pos..pos + bsize], &mut st, &mut hist, block_max)?;
                pos += bsize;
            }
            _ => bail!("reserved block type"),
        }
        if let Some(cs) = h.content_size {
            ensure!(hist.frame_len() as u64 <= cs, "frame decodes past its declared content size");
        }
        if last {
            break;
        }
    }
    if let Some(cs) = h.content_size {
        ensure!(hist.frame_len() as u64 == cs, "decoded size differs from Frame_Content_Size");
    }
    if h.checksum {
        ensure!(data.len() >= pos + 4, "content checksum truncated");
        let stored = le(&data[pos..pos + 4]) as u32;
        let actual = xxh64(hist.frame_output(), 0) as u32;
        let _ = (stored, actual);
        pos += 4;
    }
    Ok((hist.into_output(), pos))
}

/// Decode every frame in `input`, skipping skippable frames.
pub fn decode_all(input: &[u8], dict: Option<&Dictionary>) -> Result<Vec<u8>> {
    ensure!(!input.is_empty(), "empty input");
    let mut out = Vec::new();
    let mut pos = 0;
    while pos < input.len() {
        ensure!(input.len() - pos >= 4, "trailing bytes are not a frame");
        let magic = le(&input[pos..pos + 4]) as u32;
        pos += 4;
        if magic == FRAME_MAGIC {
            let (o, used) = decode_frame(&input[pos..], dict, out)?;
            out = o;
            pos += used;
        } else if magic & 0xFFFF_FFF0 == 0x184D_2A50 {
            ensure!(input.len() - pos >= 4, "skippable frame header truncated");
            let size = le(&input[pos..pos + 4]) as usize;
            pos += 4;
            ensure!(input.len() - pos >= size, "skippable frame truncated");
            pos += size;
        } else {
            bail!("unknown frame magic {:#010x}", magic);
        }
    }
    Ok(out)
}
