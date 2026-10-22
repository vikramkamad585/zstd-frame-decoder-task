#!/usr/bin/env bash
# Install the reference codec into /app/zstdr and build it.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
rm -rf /app/zstdr/src
mkdir -p /app/zstdr/src
cp -r "$HERE"/zstdr-ref/src/. /app/zstdr/src/
cp "$HERE"/zstdr-ref/Cargo.toml /app/zstdr/Cargo.toml
cd /app/zstdr
cargo build --release --offline
python3 /app/selfcheck.py
