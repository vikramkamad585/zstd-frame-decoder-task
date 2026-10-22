//! Dictionaries: the zstd dictionary format (RFC 8878 §5) or raw content.

use crate::error::Result;
use crate::fse::Table;
use crate::huffman::HuffTable;
use crate::sequences::SeqTables;
use crate::ensure;

pub const DICT_MAGIC: u32 = 0xEC30A437;

pub struct Dictionary {
    pub id: u32,
    pub content: Vec<u8>,
    pub huff: Option<HuffTable>,
    pub tables: SeqTables,
    pub reps: [u32; 3],
}

impl Dictionary {
    pub fn parse(data: &[u8]) -> Result<Dictionary> {
        if data.len() < 8 || u32::from_le_bytes(data[0..4].try_into().unwrap()) != DICT_MAGIC {
            return Ok(Dictionary {
                id: 0,
                content: data.to_vec(),
                huff: None,
                tables: SeqTables::default(),
                reps: [1, 4, 8],
            });
        }
        let id = u32::from_le_bytes(data[4..8].try_into().unwrap());
        ensure!(id != 0, "dictionary ID must not be zero");
        let mut pos = 8;
        let (huff, used) = HuffTable::read(&data[pos..])?;
        pos += used;
        let (of, used) = Table::read_description(&data[pos..], 31, 8)?;
        pos += used;
        let (ml, used) = Table::read_description(&data[pos..], 52, 9)?;
        pos += used;
        let (ll, used) = Table::read_description(&data[pos..], 35, 9)?;
        pos += used;
        ensure!(data.len() >= pos + 12, "dictionary repeat offsets truncated");
        let mut reps = [0u32; 3];
        for (i, r) in reps.iter_mut().enumerate() {
            *r = u32::from_le_bytes(data[pos + 4 * i..pos + 4 * i + 4].try_into().unwrap());
        }
        pos += 12;
        let content = data[pos..].to_vec();
        for &r in &reps {
            ensure!(r != 0 && (r as usize) <= content.len(), "dictionary repeat offset out of range");
        }
        Ok(Dictionary {
            id,
            content,
            huff: Some(huff),
            tables: SeqTables { ll: Some(ll), of: Some(of), ml: Some(ml) },
            reps,
        })
    }
}
