# scandbox-pi (podman)---portable, no baked-in UID/GID/HOME.
#
# At runtime, podman --userns=keep-id maps the calling user's identity into the
# container. An ephemeral per-run home is mounted at /home/user. Any user runs
# the same image without a rebuild.
#
# Build (from-source, needs the pi-build source tree):
#   podman build -t scandbox-pi -f harnesses/pi/Dockerfile \
#     -v /path/to/pi-build:/pi-src:ro .
#
# Build (from existing harness-pi---fast, reuses compiled binary):
#   podman build -t scandbox-pi -f harnesses/pi/Dockerfile.from-base .

# Stage 1: compile pi binary from mounted or copied source
FROM node:22-bookworm-slim AS compile
RUN npm install -g bun
ARG PI_SRC=/pi-src
COPY . /build-ctx
RUN if [ -d "$PI_SRC/packages/coding-agent" ]; then \
      cp -a "$PI_SRC" /build; \
    elif [ -d "/build-ctx/pi-build/packages/coding-agent" ]; then \
      cp -a /build-ctx/pi-build /build; \
    else \
      echo "error: pi source not found---mount via -v or set PI_SRC" >&2; exit 1; \
    fi
WORKDIR /build/packages/coding-agent
RUN bun build --compile --no-compile-autoload-bunfig \
    ./dist/bun/cli.js ./src/utils/image-resize-worker.ts \
    --outfile /pi

# Stage 2: clean runtime---NO baked-in user
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl jq git ca-certificates ripgrep bash \
    && rm -rf /var/lib/apt/lists/*
COPY --from=compile /pi /usr/local/bin/pi
COPY --from=compile /build/packages/coding-agent/dist/modes/interactive/theme /usr/local/bin/theme

RUN mkdir -p /home/user

# scandbox self-wiring entrypoint + manifest
COPY harnesses/entrypoint.sh /usr/local/bin/scandbox-entrypoint
COPY harnesses/pi/harness.json /etc/scandbox/harness.json
RUN mkdir -p /etc/scandbox && \
    chmod 0755 /usr/local/bin/scandbox-entrypoint && \
    chmod 0644 /etc/scandbox/harness.json

ENTRYPOINT ["/usr/local/bin/scandbox-entrypoint"]
