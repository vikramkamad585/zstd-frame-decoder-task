//! Frame layer (RFC 8878 §3.1): magic numbers, frame header, the block loop,
//! the content checksum and skippable frames.
//!
//! Nothing below the frame layer exists yet. Suggested modules, one per
//! stage of the format:
//!   bits.rs       forward and backward bit readers
//!   fse.rs        FSE table descriptions, decoding tables, predefined tables
//!   huffman.rs    Huffman tree descriptions and 1- / 4-stream decoding
//!   literals.rs   Literals_Section
//!   sequences.rs  Sequences_Section and sequence decoding
//!   block.rs      sequence execution and repeat offsets
//!   window.rs     history, Window_Size and dictionary reach rules
//!   dict.rs       zstd-format and raw-content dictionaries
//!   xxhash64.rs   XXH64 for Content_Checksum

/// Decode every frame in `input`, using `dict` (raw file bytes of the -D
/// argument) when given. Returns the concatenated content of all frames.
pub fn decode_all(input: &[u8], dict: Option<&[u8]>) -> Result<Vec<u8>, String> {
    let _ = (input, dict);
    Err("not implemented".to_string())
}
