# Provenance

## Source

- **Specification:** RFC 8878, *Zstandard Compression and the application/zstd Media Type*
  (Y. Collet, M. Kucherawy, IETF, February 2021), <https://www.rfc-editor.org/rfc/rfc8878>.
  - The plain-text RFC ships unmodified at `/app/docs/rfc8878.txt`.
  - It defines the frame, block, literals, sequences, FSE, Huffman and dictionary formats the
    decoder implements.
- **Reference implementation:** facebook/zstd, tag `v1.5.6`.
  - Tag object `35016bc1c0b9a2f7121b7ecc312100aad7d9f2ad`, commit
    `794ea1b0afca0f020f4e57b6732332231fb23c70`.
  - Release tarball `zstd-1.5.6.tar.gz`, SHA-256
    `8c29e06cf42aacc1eafc4077ae2ec6c6fcb96a626157e0593d5e82a34fd403c1`.
  - The `zstd` CLI was built from that tarball (`make -C programs zstd`). It produced every
    compressed input in the bundle, and its own decoder confirmed every expected output.
- **Toolchain image:** `rust:1.83.0-slim-bookworm`
  (`sha256:540c902e99c384163b688bbd8b5b8520e94e7731b27f7bd0eaa56ae1960627ab`).

## Why this is real work

A pure-Rust Zstandard decoder is something teams actually build: for example the `ruzstd` crate,
and decoders embedded in WebAssembly runtimes, bootloaders and sandboxed build systems. These run
where linking the C libzstd is not allowed or not possible. The work is exactly this task:

1. Implement the RFC end to end.
2. Discover where the reference tool's output exercises corners the prose underplays: treeless
   literals, Repeat-mode FSE tables carried across blocks, repeat-offset rules when the literal
   length is 0, dictionary content reachable past Window_Size, multi-frame streams.
3. Validate bit-exactly against what `zstd` actually emits.

Strict rejection matters too: such decoders often sit on untrusted input.

## What the environment ships

- `/app/zstdr` is a Cargo package. Only the command-line front end (`main.rs`: argument parsing,
  file I/O, exit codes) is written; `frame::decode_all` is a stub returning an error.
- All decoding logic was removed: bit readers, FSE, Huffman, literals, sequences, execution,
  window, dictionaries, XXH64.
- No zstd binary, library binding or crate is available in the image. The base image's
  `libzstd.so.1` (a dependency of `dpkg`) is made unreadable while cases run, and the linkage gate
  forbids loading libraries or spawning programs.
- `/app/samples` holds 132 visible cases with full expected outputs. They come from the same
  generator families as the grading corpus, but with a different seed (777 vs. 20261022) and at
  about 9% of its scale. The very large long-distance cases are excluded.

## How the cases were built

An authoring script drove zstd v1.5.6 inside a Debian container. It is not part of the bundle; it
runs only at authoring time.

**Content.** It generates deterministic, seeded content: Zipf-distributed pseudo-text, JSON log
lines, C-like source, packed binary records, incompressible noise, long byte runs, DNA-like
low-entropy text, CSV and mixtures. Sizes are log-uniform from a few bytes to about 2 MiB, plus
long-window inputs of 0.6–115 MB that repeat a random block far apart.

**Compression.** Inputs were compressed with real CLI invocations:
- levels 1–22 and `--fast=N`
- random `--zstd=strategy,wlog,hlog,clog,slog,mml,tlen`
- `--long=24…27`, `-T2`/`-T4`, `--rsyncable`, `--no-content-size`, `--no-check`
- `--target-compressed-block-size`
- streaming stdin input
- `--train` dictionaries with explicit `--dictID`, `-D`, `--no-dictID`
- `--patch-from`

**Hand-assembled frames.** Edge and error frames built from Raw/RLE blocks with explicit window
descriptors, 8-byte content-size fields, the Unused bit set, and wrong content sizes. They use an
independent Python XXH64.

**Acceptance filter.**
- A success case is kept only if `zstd -d` reproduces the original content exactly.
- A rejection case is kept only if `zstd -d` rejects it **and** the contract in `instruction.md`
  requires rejection.
- Bit-flip cases are kept only when the flip lands in the checksum, or when `zstd -d --no-check`
  decodes successfully to different content. The only possible verdict is then a checksum
  mismatch, so any conforming decoder must reject it.
- 11 candidate rejection cases were dropped because zstd 1.5.6 is more lenient than the RFC there:
  - it accepts Raw blocks larger than the window
  - it accepts some Raw blocks just above 128 KiB
  - it caps the effective window at Frame_Content_Size

**Grading corpus.** 1,099 cases: levels 123, fast 50, params 150, long 32, dict 160, rawdict 60,
multiframe 90, stream 110, edge 123, errors 201. Expected outputs are stored as SHA-256 plus
length.

## Reference solution

`solution/zstdr-ref` is a std-only Rust decoder of about 1,100 non-blank lines, written by the task author
directly from RFC 8878. No zstd source code was consulted for the decoding algorithms. It passes all
1,099 grading cases and all 132 samples, and defines the score ceiling of 1.0.
