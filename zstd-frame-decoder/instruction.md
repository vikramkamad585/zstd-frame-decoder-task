# Zstandard decoder in std-only Rust

Our build pipeline has to decompress `.zst` artifacts inside sandboxes where no C toolchain or shared
libraries can be loaded. You are writing the decoder: a complete, strict implementation of the
Zstandard format as published in RFC 8878, in Rust with no external crates. It must decode everything
the reference `zstd` command-line tool (v1.5.6) produces, across all compression levels, advanced
parameters, long-distance windows, trained and raw dictionaries, multi-frame and skippable-frame
streams. It must also reject malformed input exactly where the contract below says to.

The starting tree builds and parses the command line, but `frame::decode_all` returns an error for
every input. Everything below the command line is yours to write: bit readers, FSE and Huffman
tables, literals and sequences sections, sequence execution with repeat offsets, window and dictionary
handling, and XXH64.

## Workspace

| Path | What it is |
|---|---|
| `/app/zstdr/` | The Cargo package you deliver. `Cargo.toml` defines the binary `zstdr`; `src/main.rs` already implements the CLI below; `src/frame.rs` holds the `decode_all` stub and a suggested module layout. Restructure freely. |
| `/app/docs/rfc8878.txt` | The full text of RFC 8878 (*Zstandard Compression and the application/zstd Media Type*). This is the normative format description; there is no network access. |
| `/app/samples/` | 132 sample cases with expected results: `manifest.json`, `cases/NNNNN.zst` (input), `cases/NNNNN.out` (expected output for success cases), `dicts/` (dictionaries referenced by cases). |
| `/app/selfcheck.py` | Builds your decoder exactly the way grading does, applies the same gates, runs every sample and prints the score formula on the samples. |
| `/app/.timer/` | Remaining time budget (see *Time budget*). |

Each `manifest.json` entry has `id`, `category`, `dict` (a file name in `dicts/` or `null`),
`expect` (`"ok"` or `"error"`), `sha256` and `size` of the expected output for `"ok"` cases, and a
`note` describing how the input was produced (for example `-19 --zstd=strategy=9,wlog=21` or
`truncated at 811/2048`).

Toolchain: Rust 1.83 stable (edition 2021), Python 3.11, `readelf`, `xxd`. The cargo registry is
empty and the network is off, so only `std` is available. There is no `zstd` binary or library
you can call; you can craft extra test inputs yourself, for example by mutating samples or by
hand-assembling frames from Raw and RLE blocks in Python.

## Deliverable

`/app/zstdr` must build with

```
cargo build --release --offline --bin zstdr
```

and the resulting binary is invoked as

```
zstdr [-D DICTIONARY] INPUT OUTPUT
```

- `INPUT` is a file containing zero or more skippable frames and one or more Zstandard frames,
  concatenated. `OUTPUT` is the path to write.
- `-D DICTIONARY`, when present, comes first and names a dictionary file (rules below). The same
  dictionary applies to every frame in the input.
- **Success:** write the concatenated decoded content of every Zstandard frame, in order, to
  `OUTPUT` as a regular file (skippable frames contribute nothing) and exit with status 0. A
  symlink, FIFO or other non-regular file at `OUTPUT` counts as a failure. An input whose frames decode
  to zero bytes produces an empty `OUTPUT`.
- **Rejection:** exit with status **1**. The contents of `OUTPUT` do not matter after a rejection.
  A panic, an abort, any other exit status, a signal or a timeout counts as neither a successful
  decode nor a rejection.
- Status 2 is reserved for command-line usage errors (already implemented).

### Format rules

Follow RFC 8878 section 3 (frames) and section 5 (dictionary format). The points below are where
a decoder has a choice, or where the RFC leaves something to the implementation. They are part of
the contract.

1. **Magic numbers.** `0xFD2FB528` starts a Zstandard frame. `0x184D2A50`–`0x184D2A5F` start a
   skippable frame: a 4-byte little-endian size followed by that many bytes, which are ignored.
   Anything else where a frame should start is an error. That includes the legacy pre-1.0 magics,
   an empty input file, and trailing bytes after the last frame that do not form a complete frame.
2. **Frame header.** The *Reserved* bit (bit 3 of Frame_Header_Descriptor) must be zero, or the
   input is rejected. The *Unused* bit (bit 4) must be ignored: a frame with it set is valid.
3. **Window limit.** Support every Window_Size up to and including 2^27 bytes (128 MiB), and reject
   any frame whose Window_Size is larger. For single-segment frames, Window_Size is
   Frame_Content_Size.
4. **Blocks.** Block type 3 (Reserved) is an error. Block_Maximum_Size is min(Window_Size, 128 KiB).
   A block whose Block_Size, or whose decompressed size, exceeds it is an error.
5. **Content size.** When Frame_Content_Size is present, the frame must decode to exactly that many
   bytes, or it is rejected.
6. **Checksum.** When Content_Checksum_Flag is set, verify it: the low 32 bits of XXH64 (seed 0)
   over the frame's decoded content. A mismatch is a rejection.
7. **Truncation.** Input that ends inside a frame header, a block header, a block or a checksum is
   an error.
8. **Corruption.** Everything the RFC calls corrupt is an error:
   - invalid FSE or Huffman table descriptions
   - a bitstream that is not consumed exactly
   - a sequence that consumes more literals than were decoded
   - a match offset that reaches before the available history
   - Repeat or Treeless modes with no previous table
   - a non-zero reserved field in the sequences section
9. **Dictionaries.** A `-D` file of at least 8 bytes that begins with `0xEC30A437`
   (little-endian) is a zstd-format dictionary (RFC 8878 §5). It supplies an ID, the Huffman and
   FSE tables that seed Treeless/Repeat modes, three repeat offsets, and content. Any other file is
   a *raw content* dictionary: all of its bytes are content, its ID is 0, it has no entropy tables,
   and the repeat offsets start at 1, 4, 8.
   - A frame with a non-zero Dictionary_ID needs a dictionary with that same ID. If no `-D` is
     given, or the given dictionary's ID differs, reject.
   - A frame with Dictionary_ID 0 (field absent or zero) uses the `-D` dictionary when one is
     given.
   - Dictionary content sits logically before the frame's first byte. Matches may reach into it
     while the frame's decoded size so far is at most Window_Size, as RFC 8878 §5 describes.
   - Each frame starts fresh from the dictionary state (or from the defaults when there is none).
     Entropy tables, repeat offsets and history never carry over from one frame to the next.

## Grading

The grading corpus has about 1,100 cases. They come from the same generator families as the
samples, but with different seeds, content and parameters, and they include larger and
longer-range inputs than the samples. Every case is decoded by your release binary, built fresh from
a copy of `/app/zstdr`.

### Hard gates: any failure scores 0

1. **Source gate.** The tree is copied (skipping `target/` and `.git/` directories at any depth,
   at most 256 MiB in total) and these checks apply to the copy:
   - only `std`: every dependency in any `Cargo.toml` must be a `path` dependency inside the
     package, or `{ workspace = true }` resolving to one
   - no `build.rs`, no `package.build`, no `package.links`, no proc-macro crates, no `[patch]` or
     `[replace]`
   - no `.cargo/` directory at any depth
   - no C/C++ sources or headers, no object files, static or shared libraries, no ELF file, and
     no symlink, FIFO or device file anywhere in the tree
   - every `Cargo.toml` must parse; a manifest that does not parse, or uses an unsupported
     structure, fails this gate
2. **Build gate.** The build runs as a non-root user with a clean environment, a fresh
   `CARGO_HOME`, no `RUSTFLAGS` and no network, with a 20-minute limit. `cargo build --release
   --offline --bin zstdr` must succeed.
3. **Linkage gate.** The binary must be dynamically linked only against `libc.so.6`, `libm.so.6`,
   `libgcc_s.so.1`, `libpthread.so.0`, `librt.so.1` and the dynamic loader. It must not import any
   of `dlopen dlmopen execve execv execvp execvpe execl execlp execle fexecve
   posix_spawn posix_spawnp fork vfork system popen`. In other words: no loading libraries and no
   running other programs. A plain std-only Rust binary passes; `std::process::Command` does not.

`python3 /app/selfcheck.py` runs these same three gates, with the same code, before it runs any
sample, and prints which gate failed.

### Per-case execution

- Each case runs with up to 8 in parallel, as an unprivileged user, with an empty `PATH`.
- Limits per case: 20 seconds wall clock, 60 seconds CPU, 4 GiB address space, 1 GiB output file.
- The system's shared zstd library is unreadable while cases run.
- The largest outputs are around 120 MB, so a straightforward but not wasteful implementation is
  fast enough. The reference finishes every case in well under a second.

A success case passes when the exit status is 0 and the SHA-256 and length of `OUTPUT` match. A
rejection case passes when the exit status is exactly 1. A case that crashes, times out or is
skipped fails. The denominator is fixed.

### Score

Cases fall into ten categories. Nine are success categories:

| Category | What it exercises |
|---|---|
| `levels` | `zstd -1` … `-19`, `--ultra -20` … `-22` |
| `fast` | negative levels, `--fast=1` … `--fast=200` |
| `params` | random advanced parameters: every strategy (fast … btultra2), window/hash/chain/search logs, min match, target length |
| `long` | `--long` and large `wlog`: window descriptors up to 2^27, matches tens of MiB back |
| `dict` | trained zstd-format dictionaries, with and without the Dictionary_ID field |
| `rawdict` | raw content dictionaries, including patch-style deltas against a large reference |
| `multiframe` | concatenated frames of mixed settings, with skippable frames anywhere |
| `stream` | frames from streaming input (no content size), multi-threaded producer, rsyncable, small target block sizes, checksum on or off |
| `edge` | boundary sizes (0, 1, block-size ± 1, …), hand-assembled Raw/RLE frames with unusual window descriptors and 8-byte content-size fields, Unused header bit set |

The tenth, `errors`, is the rejection category: truncation, checksum mismatches, reserved bits and
block types, bad magic and trailing garbage, oversized windows and blocks, content-size mismatches,
and missing or wrong dictionaries.

With *p_c* the fraction of cases passed in category *c*:

```
S     = mean of p_c over the nine success categories   (each category weighs 1/9)
E     = p_errors
score = S × (0.8 + 0.2 × E)          if all three gates pass, else 0
```

The score is continuous in [0, 1]. A decoder that only rejects scores 0, since rejection credit
multiplies S. A decoder that decodes everything but never rejects scores 0.8. A complete, strict
decoder scores 1.0. `selfcheck.py` prints S, E and the score over the samples. The grading corpus
is about eight times larger and more varied, so treat the sample score as an estimate, not a
guarantee.

## Time budget

You have **8 hours** of wall-clock time. The system clock is not a reliable guide to how much of it
is left; `/app/.timer` is.

- `cat /app/.timer/remaining_secs` shows the seconds left, and `elapsed_secs` the seconds used.
- `alert_30min`, `alert_10min` and `alert_5min` files appear as the budget runs down.

Check it before any long debugging excursion, and schedule against it.

A sensible order is:

1. Frame and block layer with Raw/RLE blocks and XXH64.
2. The literals section (raw, RLE, Huffman with 1 and 4 streams, treeless).
3. FSE and the sequences section with all four table modes.
4. Execution and repeat offsets.
5. Dictionaries.
6. Strict rejection rules, then efficiency.

Every stage you finish lifts several categories at once.

## Working rules

- No one is available to answer questions; decide from the RFC and this contract, and keep going.
- Keep `/app/zstdr` building at every checkpoint. A tree that fails any gate scores 0 regardless of
  how much of the decoder is written. Run `python3 /app/selfcheck.py` after each meaningful change,
  and use `-v` to list failing samples or `-k CATEGORY` to focus on one category.
- When a category is stuck, find the smallest failing sample (`note` tells you how it was made),
  and compare your intermediate state against the RFC's algorithm text, step by step. The RFC
  sections on FSE table construction, Huffman weight decoding and repeat offsets are precise. Follow
  them literally.
- The samples are a small slice. Don't special-case anything about them; implement the format.
- Leave the last 20–30 minutes for a clean rebuild and a full selfcheck run. A half-finished
  refactor at the deadline is worth less than the last version that passed.
