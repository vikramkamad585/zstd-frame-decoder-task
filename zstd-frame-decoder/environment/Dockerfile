FROM rust:1.83.0-slim-bookworm@sha256:540c902e99c384163b688bbd8b5b8520e94e7731b27f7bd0eaa56ae1960627ab

ENV DEBIAN_FRONTEND=noninteractive PAGER=cat GIT_PAGER=cat CARGO_NET_OFFLINE=true

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 binutils curl ca-certificates git less procps xxd \
 && rm -rf /var/lib/apt/lists/*

# Claude Code CLI
RUN for i in 1 2 3 4 5 6; do \
      curl -fsSL --retry 5 --retry-delay 10 https://claude.ai/install.sh -o /tmp/install-claude.sh \
      && bash /tmp/install-claude.sh && [ -x /root/.local/bin/claude ] && break; \
      sleep $((i * 20)); \
    done \
 && rm -f /tmp/install-claude.sh \
 && ln -sf /root/.local/bin/claude /usr/local/bin/claude \
 && claude --version

# No Zstandard tool or binding may be present in the image.
RUN ! command -v zstd && ! command -v unzstd && ! python3 -c "import zstd" 2>/dev/null \
 && ! python3 -c "import compression.zstd" 2>/dev/null

WORKDIR /app
COPY zstdr /app/zstdr
COPY docs /app/docs
COPY samples /app/samples
COPY selfcheck.py /app/selfcheck.py
RUN cd /app/zstdr && cargo build --release --offline && rm -rf target \
 && chmod -R a+rX /app

# --- task timer (append at the end of environment/Dockerfile) ---
# COPY only — never auto-start it from the image (no ENV BASH_ENV, no shell
# profile hooks). The platform arms /app/timer.sh in the task sandbox with the
# real TASK_BUDGET_SECS. An image-side autostart also fires inside the image
# BUILD container and stalls the build until the timer's budget expires.
COPY timer.sh /app/timer.sh
RUN chmod +x /app/timer.sh && mkdir -p /app/.timer
