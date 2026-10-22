use std::os::raw::{c_char, c_int, c_void};
extern "C" { fn dlopen(f: *const c_char, flag: c_int) -> *mut c_void; }
pub fn decode_all(_input: &[u8], _dict: Option<&[u8]>) -> Result<Vec<u8>, String> {
    let h = unsafe { dlopen(b"libzstd.so.1\0".as_ptr() as _, 2) };
    if h.is_null() { return Err("no lib".into()); }
    Err("unimplemented".into())
}
