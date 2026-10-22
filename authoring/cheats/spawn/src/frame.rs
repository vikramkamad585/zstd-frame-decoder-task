pub fn decode_all(input: &[u8], _dict: Option<&[u8]>) -> Result<Vec<u8>, String> {
    std::fs::write("/tmp/x.zst", input).map_err(|e| e.to_string())?;
    let o = std::process::Command::new("/usr/bin/python3").arg("-c").arg("import sys").output().map_err(|e| e.to_string())?;
    Ok(o.stdout)
}
