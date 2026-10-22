pub fn decode_all(input: &[u8], _dict: Option<&[u8]>) -> Result<Vec<u8>, String> {
    std::fs::write("/tmp/x.zst", input).map_err(|e| e.to_string())?;
    let o: Vec<u8> = Vec::new();
    Ok(o)
}
