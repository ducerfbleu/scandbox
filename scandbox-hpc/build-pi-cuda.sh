#!/usr/bin/env bash
# build-pi-cuda.sh---build a verifiable SIF for the CUDA pi harness (scandbox-pi-cuda).
#
# Run on a host with BOTH docker (holding the scandbox-pi-cuda image) and apptainer. It converts the
# local docker image into ./scandbox-pi-cuda.sif (next to this script) + a provenance sidecar, then
# `apptainer verify`s it. scp the resulting .sif to the cluster's scandbox-hpc/ and run it with:
#   ./scandbox-run --agent scandbox-pi-cuda.sif --gpu nvidia --llm <url> --netns --work "$PWD/work" -p "…"
#
# Need the image first?  (from the pi-agent repo root)
#   docker compose -f scandbox/docker-compose.pi-cuda.yml build
#
# Signing is the maintainer's: import/create a PGP key (`apptainer key import` / `apptainer key newpair`)
# BEFORE --sign. Without apptainer on PATH this just prints the pipeline (implicit dry-run).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

IMAGE="scandbox-pi-cuda"; OUT=""; SIGN=0; KEY=""; FAKEROOT=0; FORCE=0; DRYRUN=0

usage() {
  cat >&2 <<'EOF'
usage: build-pi-cuda.sh [--image NAME[@sha256:…]] [--out FILE.sif] [--sign] [--key IDX|FP]
                        [--fakeroot] [--force] [--dry-run]
  --image     source docker image (default: scandbox-pi-cuda); pin as name@sha256:… for a verified build
  --out       output SIF path (default: ./scandbox-pi-cuda.sif, next to this script)
  --sign      apptainer sign the SIF (needs a maintainer PGP key already in the keyring)
  --key       key index or fingerprint to sign with (apptainer --key)
  --fakeroot  apptainer build --fakeroot (if the site needs it)
  --force     overwrite an existing output SIF (apptainer build --force)
  --dry-run   print the pipeline, do nothing
EOF
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --image) IMAGE="$2"; shift 2;;
    --out)   OUT="$2"; shift 2;;
    --sign)  SIGN=1; shift;;
    --key)   KEY="$2"; shift 2;;
    --fakeroot) FAKEROOT=1; shift;;
    --force)    FORCE=1; shift;;
    --dry-run)  DRYRUN=1; shift;;
    -h|--help)  usage 0;;
    *) echo "unknown arg: $1" >&2; usage 1;;
  esac
done

if [ -z "$OUT" ]; then
  base="${IMAGE%@*}"; base="${base##*/}"; OUT="$HERE/${base}.sif"
fi
SRC="docker-daemon://${IMAGE}"

build_flags=(); [ "$FAKEROOT" -eq 1 ] && build_flags+=("--fakeroot"); [ "$FORCE" -eq 1 ] && build_flags+=("--force")
sign_flags=();  [ -n "$KEY" ] && sign_flags+=("--key" "$KEY")

# source docker digest for the provenance sidecar (best-effort)
SRC_DIGEST=""
if command -v docker >/dev/null 2>&1; then
  SRC_DIGEST="$(docker image inspect "${IMAGE%@*}" --format '{{.Id}}' 2>/dev/null || true)"
fi

print_plan() {
  echo "# 1. build:   apptainer build ${build_flags[*]:+${build_flags[*]} }$OUT $SRC"
  [ "$SIGN" -eq 1 ] && \
    echo "# 2. sign:    apptainer sign ${sign_flags[*]:+${sign_flags[*]} }$OUT   (needs a maintainer PGP key)"
  echo "# 3. verify:  apptainer verify $OUT"
}

if ! command -v apptainer >/dev/null 2>&1; then
  echo "apptainer not on PATH---run this pipeline on an apptainer node:" >&2
  print_plan
  exit 0
fi
if [ "$DRYRUN" -eq 1 ]; then print_plan; exit 0; fi

echo "== build: $OUT  <-  $SRC =="
apptainer build "${build_flags[@]}" "$OUT" "$SRC"

if [ "$SIGN" -eq 1 ]; then
  echo "== sign: $OUT =="
  if ! apptainer sign "${sign_flags[@]}" "$OUT"; then
    echo "warning: signing failed---import/create a maintainer PGP key first:" >&2
    echo "         apptainer key import <key.asc>   # or: apptainer key newpair" >&2
  fi
fi

echo "== verify: $OUT =="
VERIFY_OUT="$(apptainer verify "$OUT" 2>&1)" && VSTATUS="verified" || VSTATUS="unverified"
# An unsigned SIF makes `apptainer verify` exit non-zero ("signature not found")---that is the
# EXPECTED result without --sign, not a build failure. Record it as unsigned, not unverified.
case "$VERIFY_OUT" in
  *"signature not found"*|*"no signature"*|*"no signatures"*|*"not signed"*|*"no objects"*)
    VSTATUS="unsigned";;
esac
case "$VSTATUS" in
  unsigned) echo "  unsigned (no signature---expected without --sign; re-run with --sign to sign it)";;
  *)        echo "$VERIFY_OUT";;
esac

SHA="$(sha256sum "$OUT" | awk '{print $1}')"

OUT="$OUT" SRC="$SRC" SRC_DIGEST="$SRC_DIGEST" IMAGE="$IMAGE" \
VSTATUS="$VSTATUS" VERIFY_OUT="$VERIFY_OUT" SHA="$SHA" python3 - <<'PY'
import json, os
prov = {
    "sif": os.environ["OUT"],
    "sif_sha256": os.environ["SHA"],
    "source": os.environ["SRC"],
    "source_image": os.environ["IMAGE"],
    "source_docker_id": os.environ.get("SRC_DIGEST") or None,
    "signature": {"status": os.environ["VSTATUS"], "detail": os.environ["VERIFY_OUT"][:2000]},
}
path = os.environ["OUT"] + ".provenance.json"
with open(path, "w") as f:
    f.write(json.dumps(prov, indent=2))
print("provenance:", path)
print("sif_sha256:", prov["sif_sha256"], "| signature:", prov["signature"]["status"])
PY

echo "Done: $OUT"
