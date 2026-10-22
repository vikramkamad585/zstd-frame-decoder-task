fn main() { let _ = std::process::Command::new("sh").arg("-c").arg("cp /usr/lib/*/libzstd.so.1 $OUT_DIR/").status(); }
