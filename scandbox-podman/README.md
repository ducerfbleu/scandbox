# scandbox-podman---usage

Record agent sessions through the L7 recorder, rootless, daemonless, via podman + pasta.

## Prerequisites

- **podman + pasta + netavark**---`ml podman` (Lua module)
- If `/run/user/$UID` is read-only, move podman runtime dirs to `~/.local/`:
  - `~/.config/containers/storage.conf` → `runroot = ~/.local/share/containers/runroot`
  - `~/.config/containers/containers.conf` → `tmp_dir = ~/.local/share/containers/tmp`
  - `~/.config/containers/policy.json` → `default: insecureAcceptAnything` (accept local images)
- **aardvark-dns**: may not be available (D-Bus dependency). Container DNS disabled; recorder reached
  by IP via `--add-host`. Harmless `WARN aardvark-dns binary not found` on every container run.

## Setup

```bash
ml podman

# load shipped images (one-time)
podman load -i scandbox-pi.tar
podman load -i scandbox-gateway.tar
podman load -i scandbox-egress.tar

# or rebuild from source
./build-podman.sh --from source --pi-src /path/to/pi-build

# verify
podman images | grep scandbox
```

## LLM endpoint

Use `host.containers.internal` to reach services on the host from inside podman
containers. **Do not use `0.0.0.0` or `127.0.0.1`**---these don't resolve to the
host from inside rootless podman.

```bash
# LLM running in Docker, published on host port 8080:
--llm https://host.containers.internal:8080

# LLM running directly on host port 8080:
--llm http://host.containers.internal:8080

# inspect the LLM container to find endpoint + key:
./inspect-llm.sh llm
```

The recorder (dual-homed, pasta-connected) resolves `host.containers.internal` to the
host's network stack, then reaches the LLM via its published port. The agent never sees
this---it only knows `llm-gateway:8080` (the recorder's airgap IP).

## Run

```bash
ml podman

# basic---catalogued harness, auto-wiring:
./scandbox-run --agent pi --llm https://host.containers.internal:8080 --llm-key howareyou \
  --work /path/to/project -p "fix the failing test"

# interactive (land in the agent):
./scandbox-run --agent pi --llm https://host.containers.internal:8080 --llm-key howareyou \
  --work /path/to/project

# interactive shell (self-wire, then bash---launch agent as a child job):
./scandbox-run --agent pi --llm https://host.containers.internal:8080 --llm-key howareyou \
  --work /path/to/project --shell

# mount a conda env for the agent to use (read-only):
./scandbox-run --agent pi --llm https://host.containers.internal:8080 --llm-key howareyou \
  --work ./showcase --mount /opt/miniconda3/envs/benchlm:/opt/conda:ro \
  -p "use /opt/conda/bin/python for pandas"

# with web egress (allow-listed domains via Go proxy):
./scandbox-run --agent pi --llm https://host.containers.internal:8080 --llm-key howareyou \
  --egress gh,pypi --work /path/to/project -p "fix it"

# GPU (NVIDIA passthrough):
./scandbox-run --agent pi-cuda --gpu nvidia \
  --mount /opt/miniconda3:/opt/miniconda3 \
  --llm https://host.containers.internal:8080 --llm-key howareyou \
  --work /path/to/project -p "profile the training loop"

# bring-your-own image:
./scandbox-run --agent my-image --llm https://host.containers.internal:8080 --llm-key howareyou \
  -- aider --yes

# dry run (print the plan):
./scandbox-run --agent pi --llm https://host.containers.internal:8080 --dry-run
```

## How it works

```
scandbox-airgap  (podman network create --internal → no internet, no route to --llm)

┌────────────┐  HTTP  <recorder-IP>:8080  ┌───────────────────────┐
│  AGENT     │ ──────────────────────────▶│  RECORDER             │  default podman network
│ scandbox-  │  (--add-host llm-gateway)  │  scandbox-gateway     │ ──pasta──▶ --llm
│   pi       │   ✗ no route to --llm      │  (container,          │    (HTTPS)
│ rootless   │   ✗ no route to internet   │   DUAL-HOMED)         │
└─────┬──────┘                            └──────────┬────────────┘
      │ HTTPS_PROXY  (only with --egress)            │ writes
      ▼  scandbox-net (--internal)                   ▼
┌───────────┐  default 🌐                  ~/.local/state/scandbox/<run_id>/
│  egress   │ ──pasta──▶ allow-listed web    plane1.jsonl · access.log ·
│ scandbox- │                                plane3.json · index.json
│  egress   │   (Go proxy, NOT squid)
└───────────┘
```

The agent sits on an `--internal` podman network with no route out. The recorder is
dual-homed (internal + default)---the only bridge to the LLM, via pasta. Structural
airgap, rootless, no daemon.

## What you get

Every run writes `~/.local/state/scandbox/<run_id>/`:

| file | plane | contents |
|---|---|---|
| `plane1.jsonl` | 1 · LLM | every exchange---prompt / completion / tool-calls / reasoning / usage / timing, hash-chained |
| `access.log` | 2 · internet | egress log---ALLOW/DENY per attempt + per-tunnel sent/recv/dur (only with `--egress`) |
| `plane3.json` | 3 · filesystem | before/after content manifest of the work dirs |
| `index.json` |---| run metadata: runtime=podman, agent image digest, timing, exit, counts, chain anchor |

```bash
./show-log.py [RUN_ID]                   # inspect captured records
./llm-probe.sh --url https://HOST:PORT   # is the LLM serving?
```

## Smoke test

```bash
### 1. load podman
ml podman

### 2. load pre-built images from .tar
podman load -i scandbox-gateway.tar    # recorder
podman load -i scandbox-pi.tar         # agent

### 3. build the tiny-llm stub (podman-only, no docker needed)
cd scandbox/scandbox-podman/examples
podman build -t scandbox-tiny-llm -f tiny-llm/Dockerfile tiny-llm

### 4. run the test
./example-airgap-llm.sh

### 5. view recorded log
../show-log.py
```
If the images were saved together in one tar:
`podman load -i scandbox-images.tar     # loads all images in the archive`
To create the .tar on the build machine:
`podman save -o scandbox-images.tar scandbox-gateway scandbox-pi`


## Layout

```
scandbox-run              podman-only launcher (Python, stdlib only)
build-podman.sh           build images with podman (or docker fallback)
test-podman-run.sh        smoke test (verify capture + hash chain)
llm-probe.sh              LLM health check
show-log.py               inspect captured records
gateway/                  L7 recorder: gateway.go, go.mod, Dockerfile.gateway
egress/                   Go allow-list CONNECT proxy: egress.go, go.mod, Dockerfile.egress
harnesses/                entrypoint.sh (shared self-wiring) + pi/ (Dockerfile, harness.json)
scandbox-pi.tar           agent image (podman load -i)
scandbox-gateway.tar      recorder image
scandbox-egress.tar       egress proxy image
usage.md                  this file
```

## Differences from docker mode

| | docker | podman |
|---|---|---|
| daemon | dockerd required | daemonless |
| rootless | needs docker group | default (pasta) |
| recorder | container, `--user uid:gid` | container, rootless default (root inside = user outside) |
| airgap | `--internal` docker bridge | `--internal` podman network (netavark) |
| DNS | docker DNS (container names) | `--add-host` (aardvark-dns unavailable) |
| egress | squid container | Go `scandbox-egress` container |
| LLM access | `--llm-net` / `host.docker.internal` | pasta → host network stack |
| health check | TCP connect to container IP | `podman inspect` (container IPs not host-routable in rootless) |
