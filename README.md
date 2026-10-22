# zstd-frame-decoder: task bundle and authoring tools

**This repository is private on purpose.** `zstd-frame-decoder/tests/` holds the sealed grading
corpus and `zstd-frame-decoder/solution/` holds the reference decoder. Publishing either would
expose the grader, so keep the repo private.

## The task

Implement a complete Zstandard decoder (RFC 8878) in std-only Rust, as the CLI
`zstdr [-D DICT] INPUT OUTPUT`. It must decode every frame that zstd v1.5.6 produces and reject
malformed input. Grading runs 1,099 sealed cases; the reward is
`S × (0.8 + 0.2 × E)` behind source, build and linkage gates.

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
                             132 visible samples, selfcheck.py
  tests/                     test.sh, collect.py (evidence + gates), compute_reward.py,
                             cases/ (1,099 sealed cases: .zst inputs, dicts, manifest)
  solution/                  solve.sh + zstdr-ref (reference decoder, ~1,100 lines)
authoring/                   never shipped
  Dockerfile.gen             Debian + zstd 1.5.6 built from the release tarball
  gen.py                     case generator with the acceptance filters
  check_ref.py               runs any decoder binary over a generated case set
  probe.py                   probes zstd's own leniency on hand-built frames
  cheats/                    deliberate cheaters, a legitimate restructured variant,
                             and partial-credit variants used for validation
  verifier_tests/            verifier regression suite (run_tests.py): 27 hostile or
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
```

## Local validation (needs Docker and harbor 0.24)

```bash
cd zstd-frame-decoder
harbor run -p . -a nop    -e docker -k 1    # expect reward 0.0
harbor run -p . -a oracle -e docker -k 2    # expect reward 1.0 on both runs
```

Verifier regression suite (needs the task image built as `zfd-env`):

```bash
docker build -t zfd-env zstd-frame-decoder/environment
python3 authoring/verifier_tests/run_tests.py      # expect 27/27 scenarios passed
python3 authoring/verifier_tests/run_tests.py zfd-env dynsym-flood   # one scenario
```

## Results recorded on 2026-10-22

These ran on an Apple M5 with Docker Desktop, using arm64 images; the last row was re-run on an
emulated linux/amd64 image.

| Check | Result |
|---|---|
| harbor nop, k=1 | reward 0.0, valid 1, all four verifier files present |
| harbor oracle, k=2 | reward 1.0 and 1.0, 1099/1099 cases, 0 errored trials |
| cheat: FFI to libzstd | 0.0 (linkage gate: links libzstd.so.1) |
| cheat: std::process::Command | 0.0 (linkage gate: imports fork) |
| cheat: build.rs | 0.0 (source gate) |
| cheat: dlopen | 0.0 (linkage gate) |
| cheat: vendored ELF blob | 0.0 (source gate) |
| legitimate variant (path sub-crate, threads, broad std use) | 1.0 |
| partial: no dictionary support | 0.789 |
| partial: no checksum verification | 0.961 |
| amd64 (emulated) nop / oracle | 0.0 / 1.0 |
| verifier regression suite (after hardening) | 27/27 scenarios pass |
| hostile decoder: symlink, FIFO and sparse 900 MB outputs, 300 MB stderr floods | no hang; those cases fail cleanly; verifier peak memory ~40 MB; reward 0.879, valid 1 |
| malformed or unsupported `Cargo.toml`, nested `.cargo`, symlinks, device nodes, oversized tree | reward 0.0, valid 1 |
| planted `/.cargo` and `/tmp/.cargo` configs | moved aside during the build and restored; reward 1.0 |
| `rust-toolchain.toml` pointing at a fake toolchain, in the package or in `/tmp` | ignored (`RUSTUP_TOOLCHAIN` pinned); fake rustc never runs; reward 1.0 |
| decoder importing `fork` plus ~3,000 libc/libm symbols (265 KB of `readelf` output) | 0.0 (linkage gate); scored 1.0 before `readelf` output stopped being tail-truncated |
| decoder renaming the root-owned `bin/` and `inputs/` during grading | refused (grading dir now root-owned); reward 1.0; was a verifier fault before |
| planted `/tmp/zgrade` symlink, directories at `/logs/verifier/reward.json` and `evidence.json.tmp` | reward 1.0, valid 1; were verifier faults before |
| source tree with 120,000 files | 0.0 (source gate: more than 100,000 entries) |

The final submission ZIP holds the bundle contents at the zip root: 1,461 entries, 47,788,978
bytes, SHA-256 `1e780006ab9abda5a95660bb0053580e2ad62c5902399cd3a39e6f5f78d77f46`.

Not verifiable locally: the similarity check, the quality review, the adversarial probe and the
capability gap between the two models. Those happen only after submission.
