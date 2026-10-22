use std::os::raw::{c_void, c_ulonglong};
#[link(name = "libzstd.so.1", kind = "dylib", modifiers = "+verbatim")]
extern "C" {
    fn ZSTD_decompress(dst: *mut c_void, cap: usize, src: *const c_void, n: usize) -> usize;
    fn ZSTD_isError(code: usize) -> u32;
    fn ZSTD_getFrameContentSize(src: *const c_void, n: usize) -> c_ulonglong;
}
pub fn decode_all(input: &[u8], _dict: Option<&[u8]>) -> Result<Vec<u8>, String> {
    let mut cap = unsafe { ZSTD_getFrameContentSize(input.as_ptr() as _, input.len()) } as usize;
    if cap > 1 << 30 { cap = 256 << 20; }
    let mut out = vec![0u8; cap.max(1)];
    let r = unsafe { ZSTD_decompress(out.as_mut_ptr() as _, out.len(), input.as_ptr() as _, input.len()) };
    if unsafe { ZSTD_isError(r) } != 0 { return Err("err".into()); }
    out.truncate(r);
    Ok(out)
}
