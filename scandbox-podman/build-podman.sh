#!/usr/bin/env bash
# build.sh---build scandbox images with podman (or docker as fallback).
#
#   scandbox-gateway       L7 recorder (Go static -> scratch)
#   scandbox-egress        Go allow-list CONNECT proxy (plane 2)
#   scandbox-pi            pi harness (portable, no baked-in UID)
#
# Usage:
#   ./build.sh                                          # gateway + egress only
#   ./build.sh --from docker                            # + pi from harness-pi docker image
#   ./build.sh --from source --pi-src /path/to/pi-build # + pi from source (no docker needed)
#
# Requires: podman (conda activate podman) or docker.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

FROM=""
PI_SRC=""
OUT=""
while [[ $# -gt 0 ]]; do
    case $1 in
        --from)   FROM="$2"; shift 2 ;;
        --pi-src) PI_SRC="$2"; shift 2 ;;
        --out)    OUT="$2"; shift 2 ;;
        *) echo "usage: $0 [--from docker|source] [--pi-src /path/to/pi-build] [--out FILE]" >&2; exit 1 ;;
    esac
done

# --pi-src without --from implies source
if [[ -n "$PI_SRC" && -z "$FROM" ]]; then
    FROM="source"
fi

if command -v podman &>/dev/null; then
    B=podman
elif command -v docker &>/dev/null; then
    B=docker
else
    echo "error: neither podman nor docker found on PATH" >&2
    exit 1
fi
echo "builder: $B"

echo "== scandbox-gateway (Go static -> scratch) =="
$B build -t scandbox-gateway -f "$HERE/gateway/Dockerfile.gateway" "$HERE/gateway"

echo "== scandbox-egress (Go allow-list proxy -> scratch) =="
$B build -t scandbox-egress -f "$HERE/egress/Dockerfile.egress" "$HERE/egress"

case "$FROM" in
    source)
        # Path A: compile pi from source---fully independent, no harness-pi needed
        if [[ -z "$PI_SRC" ]]; then
            echo "error: --from source requires --pi-src /path/to/pi-build" >&2
            exit 1
        fi
        PI_SRC="$(cd "$PI_SRC" && pwd)"
        if [[ ! -d "$PI_SRC/packages/coding-agent" ]]; then
            echo "error: $PI_SRC doesn't look like a pi-build (missing packages/coding-agent)" >&2
            exit 1
        fi
        echo "== scandbox-pi (from source: $PI_SRC) =="
        $B build -t scandbox-pi \
            -f "$HERE/harnesses/pi/Dockerfile" \
            -v "$PI_SRC:/pi-src:ro" \
            "$HERE"
        ;;
    docker)
        # Path B: extract pi binary from harness-pi, strip baked-in user
        if ! $B image exists harness-pi 2>/dev/null; then
            echo "error: harness-pi image not found" >&2
            echo "  import it:  docker save harness-pi | podman load" >&2
            exit 1
        fi
        echo "== scandbox-pi (from harness-pi docker image) =="
        $B build -t scandbox-pi -f "$HERE/harnesses/pi/Dockerfile.from-base" "$HERE/harnesses"
        ;;
    "")
        echo ""
        echo "scandbox-pi: skipped (pass --from to build it)"
        echo "  --from docker                            extract from harness-pi image"
        echo "  --from source --pi-src /path/to/pi-build compile from source"
        ;;
    *)
        echo "error: --from must be 'docker' or 'source', got '$FROM'" >&2
        exit 1
        ;;
esac

echo ""
echo "Done ($B):"
$B images --format '  {{.Repository}}:{{.Tag}}  {{.Size}}' | grep scandbox || true

if [[ -n "$OUT" ]]; then
    IMAGES=("scandbox-gateway" "scandbox-egress")
    if $B image exists scandbox-pi 2>/dev/null; then
        IMAGES+=("scandbox-pi")
    fi
    echo ""
    echo "Saving ${IMAGES[*]} -> $OUT"
    $B save -o "$OUT" "${IMAGES[@]}"
    echo "  $(du -h "$OUT" | cut -f1)  $OUT"
    echo "  load on another host:  podman load -i $OUT"
fi
