#!/usr/bin/env bash
# Evidence and gates only; compute_reward.py decides the score.
set -uo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
unset PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONINSPECT LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT \
      BASH_ENV ENV CDPATH GCONV_PATH PROMPT_COMMAND \
      RUSTFLAGS CARGO_BUILD_RUSTFLAGS CARGO_ENCODED_RUSTFLAGS RUSTC RUSTC_WRAPPER \
      RUSTC_WORKSPACE_WRAPPER CARGO_TARGET_DIR CARGO_BUILD_TARGET RUSTDOCFLAGS
mkdir -p /logs/verifier
rm -rf /logs/verifier/evidence.json /logs/verifier/evidence.json.tmp /logs/verifier/reward.txt \
       /logs/verifier/reward.json /logs/verifier/details.json

python3 -E -s /tests/collect.py > /logs/verifier/collect.log 2>&1

exec python3 -E -s /tests/compute_reward.py
