//! Encoder (RFC 8878 §3.1): turn any input into Zstandard frames that a
//! strict decoder reproduces exactly, as small as you can make them.
//!
//! Nothing exists yet. The pieces mirror the decoder's in reverse:
//!   match finding and parsing into sequences (literal run, match length,
//!   offset with the three repeat offsets), Huffman literal coding with a
//!   tree description, FSE coding of the length and offset codes with
//!   predefined, RLE or transmitted tables, and the frame and block layer.

/// Compress `input` into one or more frames.
pub fn compress(input: &[u8]) -> Result<Vec<u8>, String> {
    let _ = input;
    Err("not implemented".to_string())
}
