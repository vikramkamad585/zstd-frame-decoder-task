#!/usr/bin/env python3
"""Regenerate the encoder calibration variants under authoring/cheats/ from
the reference solution:

  enc-store      full decoder; encoder stores every block raw
  enc-greedy     full decoder; the baseline encoder settings (greedy hash chain)
  enc-lazy       full decoder; lazy hash-chain parsing, no optimal parse
  enc-opt-basic  full decoder; one optimal-parse pass, no three-byte matches
  legit-variant  (existing restructured decoder) + the encoder with different
                 search settings, so its frames differ from the reference's

usage: make_variants.py
"""
import os
import re
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
REF = os.path.join(HERE, "..", "zstd-frame-decoder", "solution", "zstdr-ref")
CHEATS = os.path.join(HERE, "cheats")

VARIANTS = {
    "enc-greedy": {"OPTIMAL_PARSE": "false", "LAZY_STEPS": "0", "CHAIN_DEPTH": "16"},
    "enc-lazy": {"OPTIMAL_PARSE": "false"},
    "enc-opt-basic": {"PASSES": "1", "SHORT_MATCH_MAX_OFFSET": "0"},
}
LEGIT_ENC = {"CHAIN_DEPTH": "512", "SUFFICIENT_LEN": "256", "HASH_LOG": "18"}


def patch_consts(enc_dir, consts):
    for fn in os.listdir(enc_dir):
        p = os.path.join(enc_dir, fn)
        s = open(p).read()
        for k, v in consts.items():
            s = re.sub(rf"(const {k}: [a-z0-9]+ = )[^;]+;", rf"\g<1>{v};", s)
        open(p, "w").write(s)


def fresh_copy(name):
    dst = os.path.join(CHEATS, name)
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(REF, dst, ignore=shutil.ignore_patterns("target"))
    return dst


def main():
    for name, consts in VARIANTS.items():
        d = fresh_copy(name)
        patch_consts(os.path.join(d, "src", "enc"), consts)
    d = fresh_copy("enc-store")
    p = os.path.join(d, "src", "enc", "mod.rs")
    s = open(p).read()
    s = s.replace("pub fn compress(data: &[u8]) -> Vec<u8> {\n", "pub fn compress(data: &[u8]) -> Vec<u8> {\n    return store(data);\n    #[allow(unreachable_code)]\n", 1)
    open(p, "w").write(s)

    legit = os.path.join(CHEATS, "legit-variant")
    enc = os.path.join(legit, "src", "enc")
    shutil.rmtree(enc, ignore_errors=True)
    shutil.copytree(os.path.join(REF, "src", "enc"), enc)
    patch_consts(enc, LEGIT_ENC)
    p = os.path.join(legit, "src", "main.rs")
    s = open(p).read()
    if "mod enc;" not in s:
        s = s.replace("mod dict;\n", "mod dict;\nmod enc;\n", 1)
        s = s.replace('''        if args[i] == "-D" {''', '''        if args[i] == "-z" {
            compress = true;
            i += 1;
        } else if args[i] == "-D" {''', 1)
        s = re.sub(r"(\n(\s*)let mut i = 0;\n)", r"\n\2let mut compress = false;\1", s, count=1)
        s = s.replace("    match frame::decode_all(&data, dict.as_ref()) {", '''    if compress {
        let mut out = enc::compress(&data);
        if frame::decode_all(&out, None).ok().as_deref() != Some(&data[..]) {
            out = enc::store(&data);
        }
        return match std::fs::write(output, &out) {
            Ok(()) => ExitCode::SUCCESS,
            Err(_) => ExitCode::from(1),
        };
    }
    match frame::decode_all(&data, dict.as_ref()) {''', 1)
        open(p, "w").write(s)
    print("variants written:", ", ".join(list(VARIANTS) + ["enc-store", "legit-variant"]))


if __name__ == "__main__":
    main()
