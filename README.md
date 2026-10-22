# zstd-frame-decoder: task bundle and authoring tools

**This repository is private on purpose.** `zstd-frame-decoder/tests/` holds the sealed grading
corpus and `zstd-frame-decoder/solution/` holds the reference codec. Publishing either would
expose the grader, so keep the repo private.

## The task

Implement a Zstandard (RFC 8878) codec in std-only Rust, as the CLI
`zstdr [-D DICT] INPUT OUTPUT` (decompress) and `zstdr -z INPUT OUTPUT` (compress).
- **Decoder:** must decode every frame that zstd v1.5.6 produces and reject malformed input.
- **Encoder:** scored on compressed size against per-case anchors.
- **Reward:** `0.4 × D + 0.6 × C` behind source, build and linkage gates.
  - `D = S × (0.8 + 0.2 × E)` over 1,099 sealed decoder cases. Success cases count only when a
    frame holds a Compressed block.
  - `C` is the mean per-category credit over 119 sealed encoder inputs. Each case's credit is
    0.3 for closing the gap from stored to the greedy baseline size, and 0.7 for closing the gap
    from the baseline to the reference (log size).

The encoder half was added after a weaker model solved the decoder-only version in 5 of 5 runs.

| | |
|---|---|
| Class | implementation, CPU, Rust |
| Budget | 8 hours (`timeout_sec = 28800`) |
| Spec | RFC 8878; facebook/zstd `v1.5.6` (commit `794ea1b0afca0f020f4e57b6732332231fb23c70`) |

## Layout

```
zstd-frame-decoder/          the submitted bundle (zip its *contents*)
  task.toml  instruction.md  PROVENANCE.md
  environment/               becomes /app: Dockerfile, timer.sh, zstdr skeleton, RFC text,
                             132 decoder samples, 25 encoder samples, selfcheck.py
  tests/                     test.sh, collect.py (evidence + gates), compute_reward.py,
                             cases/ (1,099 sealed decoder cases), encode/ (119 sealed encoder
                             inputs + anchors), refdec/ (strict decoder that checks encoder output)
  solution/                  solve.sh + zstdr-ref (reference codec, ~2,400 lines)
authoring/                   never shipped
  Dockerfile.gen             Debian + zstd 1.5.6 built from the release tarball
  gen.py                     case generator with the acceptance filters
  check_ref.py               runs any decoder binary over a generated case set
  probe.py                   probes zstd's own leniency on hand-built frames
  tag_compressed.py          sets each success case's `compressed` flag in a manifest
  content.py                 seeded content generators shared by gen.py and enc_corpus.py
  enc_corpus.py              encoder case generator: inputs plus stored/baseline/reference sizes
  enc_bench.py               round-trips any encoder through zstd 1.5.6; sizes vs zstd -3/-9/-19
  enc_calibrate.py           credit of encoder variants (features switched off) per category
  make_variants.py           regenerates the cheats/enc-* variants and legit-variant's encoder
  cheats/                    deliberate cheaters, a legitimate restructured variant,
                             and partial-credit variants used for validation
  verifier_tests/            verifier regression suite (run_tests.py): 28 hostile or
                             edge-case scenarios run against the real tests/ in the image
```

## Rebuilding the cases

```bash
cd authoring
curl -fsSLO https://github.com/facebook/zstd/releases/download/v1.5.6/zstd-1.5.6.tar.gz
# sha256 8c29e06cf42aacc1eafc4077ae2ec6c6fcb96a626157e0593d5e82a34fd403c1
docker build -f Dockerfile.gen -t zstd-gen:1.5.6 .
docker run --rm -v "$PWD":/a -w /a zstd-gen:1.5.6 python3 gen.py /a/out/visible 777 0.09 visible
docker run --rm -v "$PWD":/a -w /a zstd-gen:1.5.6 python3 gen.py /a/out/hidden 20261022 1.0 hidden
# encoder cases (needs the reference codec source mounted)
docker run --rm -v "$PWD":/a -v "$PWD/../zstd-frame-decoder/solution/zstdr-ref":/ref:ro zstd-gen:1.5.6 \
  python3 /a/enc_corpus.py /ref /a/out/enc-hidden 20261023 14 20
docker run --rm -v "$PWD":/a -v "$PWD/../zstd-frame-decoder/solution/zstdr-ref":/ref:ro zstd-gen:1.5.6 \
  python3 /a/enc_corpus.py /ref /a/out/enc-visible 777 3 18.5
```

The encoder anchors must be regenerated whenever the reference encoder changes.

## Local validation (needs Docker and harbor 0.24)

```bash
cd zstd-frame-decoder
harbor run -p . -a nop    -e docker -k 1    # expect reward 0.0
harbor run -p . -a oracle -e docker -k 2    # expect reward 1.0 on both runs
```

Verifier regression suite (needs the task image built as `zfd-env`):

```bash
docker build -t zfd-env zstd-frame-decoder/environment
python3 authoring/verifier_tests/run_tests.py      # expect 28/28 scenarios passed
python3 authoring/verifier_tests/run_tests.py zfd-env dynsym-flood   # one scenario
```

## Results recorded on 2026-10-22

These ran on an Apple M5 with Docker Desktop, using arm64 images. The amd64 row was re-run on an
emulated linux/amd64 image.

| Check | Result |
|---|---|
| harbor nop, k=1 | reward 0.0, valid 1, all four verifier files present |
| harbor oracle, k=2 | reward 1.0 and 1.0; 1099/1099 decoder cases; 119/119 encoder outputs at exactly the frozen reference size; 0 errored trials |
| amd64 (emulated) oracle | 1.0; all 119 encoder sizes byte-identical to the arm64 anchors |
| full decoder + store-only encoder (`cheats/enc-store`) | 0.400 (C = 0) |
| full decoder + greedy encoder (`cheats/enc-greedy`, the baseline) | 0.580 (C = 0.300) |
| full decoder + lazy encoder (`cheats/enc-lazy`) | 0.709 (C = 0.515) |
| full decoder + one-pass optimal parser (`cheats/enc-opt-basic`) | 0.953 (C = 0.922) |
| reference encoder on the authoring bench (22.3 MB) | 8.14 MB; zstd -9: 8.74 MB; zstd -19: 7.97 MB; 0 round-trip failures under zstd -d |
| cheat: FFI to libzstd | 0.0 (linkage gate: links libzstd.so.1) |
| cheat: std::process::Command | 0.0 (linkage gate: imports fork) |
| cheat: build.rs | 0.0 (source gate) |
| cheat: dlopen | 0.0 (linkage gate) |
| cheat: vendored ELF blob | 0.0 (source gate) |
| shortcut: Raw/RLE blocks only, no entropy decoding (`cheats/rawrle`) | 0.0 (S = 0; passes 167/188 unscored Raw/RLE cases); scored 0.178 before those cases lost their weight |
| legitimate variant (path sub-crate, threads, broad std use; encoder with different search settings, so different frames) | 0.998 |
| partial: no dictionary support, no encoder | 0.316 (D = 0.789) |
| partial: no checksum verification, no encoder | 0.384 (D = 0.961) |
| verifier regression suite (after hardening) | 28/28 scenarios pass |
| hostile binary: symlink, FIFO and sparse 900 MB outputs, 300 MB stderr floods, in decode and `-z` mode | no hang; those cases fail cleanly; verifier peak memory ~40 MB; valid 1 |
| malformed or unsupported `Cargo.toml`, nested `.cargo`, symlinks, device nodes, oversized tree | reward 0.0, valid 1 |
| planted `/.cargo` and `/tmp/.cargo` configs | moved aside during the build and restored; reward 1.0 |
| `rust-toolchain.toml` pointing at a fake toolchain, in the package or in `/tmp` | ignored (`RUSTUP_TOOLCHAIN` pinned); fake rustc never runs; reward 1.0 |
| decoder importing `fork` plus ~3,000 libc/libm symbols (265 KB of `readelf` output) | 0.0 (linkage gate); scored 1.0 before `readelf` output stopped being tail-truncated |
| decoder renaming the root-owned `bin/` and `inputs/` during grading | refused (grading dir now root-owned); reward 1.0; was a verifier fault before |
| planted `/tmp/zgrade` symlink, directories at `/logs/verifier/reward.json` and `evidence.json.tmp` | reward 1.0, valid 1; were verifier faults before |
| source tree with 120,000 files | 0.0 (source gate: more than 100,000 entries) |

The final submission ZIP holds the bundle contents at the zip root: 1,635 entries, 60,643,405
bytes, SHA-256 `c9973f0fc84d64e0d9899158ce75a55c9026b87e9a7f74ed565fb4b9363259b3`.

Not verifiable locally: the similarity check, the quality review, the adversarial probe and the
capability gap between the two models. Those happen only after submission.
