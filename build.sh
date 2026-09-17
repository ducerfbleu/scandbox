#!/usr/bin/env bash
# build.sh---build the shared scandbox images (recorder + egress proxy).
#   scandbox-gateway       L7 recorder (Go static -> scratch)
#   scandbox-egress        Go CONNECT allow-list proxy
#   scandbox-egress-proxy  squid domain-allowlist proxy (alternative; used by --runtime docker)
# Agent images build separately (see harnesses/).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "== scandbox-gateway (L7 recorder; Go static -> scratch) =="
docker build -t scandbox-gateway -f "$HERE/gateway/Dockerfile.gateway" "$HERE/gateway"

echo "== scandbox-egress (Go CONNECT proxy) =="
docker build -t scandbox-egress -f "$HERE/egress/Dockerfile.egress" "$HERE/egress"

echo "== scandbox-egress-proxy (squid; --runtime docker --egress) =="
docker build -t scandbox-egress-proxy -f "$HERE/Dockerfile.proxy" "$HERE"

echo "Done: scandbox-gateway + scandbox-egress + scandbox-egress-proxy"
