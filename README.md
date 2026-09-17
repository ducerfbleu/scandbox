# scandbox

Scientifically-verifiable Agent Sandbox

## Overview

Capture-first recorder for AI coding agents. Records every LLM exchange (hash-chained),
egress attempt, and filesystem change---three audit planes---while the agent runs in a
structural airgap. The agent reaches only the recorder; the recorder reaches the LLM.

## Architecture

```
  AIRGAP ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┐
  │                                                    │
  │  ┌─────────────┐         ┌──────────────┐          │
  │  │   AGENT     │  HTTP   │   RECORDER   │          │
  │  │             │ ──────▶ │  scandbox-   │ ────────────────▶  LLM endpoint
  │  │  (pi,       │  :8080  │  gateway      │         │
  │  │   claude,   │         │               │         │
  │  │   codex)    │         │  plane1.jsonl │         │
  │  │             │         │  (hash-chain) │         │
  │  └──────┬──────┘         └──────────────┘          │
  │         │                                          │
  │         │ HTTPS          ┌──────────────┐          │
  │         │ (optional)     │   EGRESS     │          │
  │         └──────────────▶ │  scandbox-    │ ────────────────▶  github, arxiv, pubmed
  │                          │  egress       │         │     (allow-listed only)
  │                          │               │         │
  │                          │  access.log   │         │
  │                          └──────────────┘          │
  │                                                    │
  └ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─  ┘
         agent has NO route outside the boundary
         recorder + egress are the only exits

  Output: ~/.local/state/scandbox/<run_id>/
    plane1.jsonl   LLM I/O (hash-chained)
    access.log     egress allow/deny
    plane3.json    filesystem diff
    index.json     run provenance
```

**Three audit planes:**
1. **Plane 1---LLM**: every prompt, completion, tool call, token usage, timing (SHA-256 hash-chained)
2. **Plane 2---Internet**: egress allow/deny per domain (optional, via `--egress`)
3. **Plane 3---Filesystem**: content-addressed before/after manifest of work directories

## Runtimes

One unified launcher (`scandbox-run`), four pluggable runtimes. The recorder is the
same in all cases---only the agent isolation differs:

| | **Apptainer (HPC)** | **Podman** | **Docker** | **Kata + Docker** |
|---|---|---|---|---|
| isolation | namespace + `--netns` | namespace (rootless) | namespace | hypervisor (separate kernel) |
| daemon | none | none | Docker daemon | Docker daemon |
| rootless | always | default | opt-in (`--user`) | no (KVM needs privileges) |
| recorder | host process | container (dual-homed) | container (dual-homed) | container (dual-homed) |
| GPU | `--nv` (Apptainer hook) | device passthrough | device passthrough + WSL2 | VFIO PCI passthrough |
| target | HPC clusters (SLURM) | workstations, CI | workstations, CI | untrusted agents, dedicated GPU |
| key custody | key forwarded | key forwarded | recorder holds real key | recorder holds real key |
| images | `.sif` (signed) | OCI | OCI | OCI (in microVM) |

**Pick by trust level:**
- **Your own agent, iterating fast** → `--runtime podman`
- **Untrusted agent, long autonomous runs** → `--runtime kata`
- **HPC cluster** → `--runtime apptainer`
- **General workstation use** → `--runtime docker`

## File layout

```
scandbox-run              unified CLI (--runtime docker|podman|apptainer|kata)
recorder.py               shared recorder lifecycle (provenance, manifest, key custody)
runtimes/
  docker.py               Docker agent wrapper
  podman.py               Podman agent wrapper (--wire, --manifest for BYO images)
  apptainer.py            Apptainer/HPC agent wrapper (--netns for enforced airgap)
  kata.py                 Kata wrapper (thin: sets --runtime kata-runtime, delegates to docker.py)
gateway/
  gateway.go              L7 recorder source (Go, stdlib only, hash-chained JSONL)
  Dockerfile.gateway      recorder image (static binary -> scratch)
egress/
  egress.go               Go CONNECT allow-list proxy source
  Dockerfile.egress       egress proxy image
Dockerfile.proxy          squid egress proxy (alternative, used by --runtime docker)
harnesses/
  entrypoint.sh           shared self-wiring entrypoint (baked into agent images)
  pi/                     CPU agent image (Dockerfile + harness.json)
  pi-cuda/                NVIDIA GPU agent image (Ubuntu 22.04 / CUDA 12.8)
build.sh                  build recorder + egress images
show-log.py               read provenance logs + throughput (tk/s)
bpe_tokenizer.py          offline tokenizer for exact reasoning/writing token splits
netns-run                 rootless network namespace helper (apptainer --netns)
bin/                      pre-compiled gateway + egress binaries (HPC, no Docker)
examples/                 CUDA GEMM demo, tiny LLM for testing
```

## Quick start

### 0. Build the recorder + proxy images (once)

```bash
cd scandbox
./build.sh       # → scandbox-gateway, scandbox-egress, scandbox-egress-proxy
```

### 1. Podman (rootless, shared GPU)

```bash
./scandbox-run --runtime podman --agent pi \
  --llm http://127.0.0.1:8014 --llm-key testkey \
  --egress pypi --work "$PWD/work" -p "your task"
```

### 2. Docker (key custody, WSL2 GPU)

```bash
# store the API key in a file (off argv, out of `docker inspect`):
mkdir -p ~/.scandbox && printf %s "$KEY" > ~/.scandbox/llm.key && chmod 600 ~/.scandbox/llm.key

./scandbox-run --runtime docker --agent pi --api anthropic \
  --llm https://api.anthropic.com --llm-verify \
  --llm-key-file ~/.scandbox/llm.key --work "$PWD/work" -p "your task"
```

### 3. Apptainer / HPC (enforced airgap)

```bash
ml apptainer
./scandbox-run --runtime apptainer --agent scandbox-pi.sif \
  --llm http://127.0.0.1:8080 --llm-key "$LLM_KEY" \
  --netns --work "$PWD/work" -p "your task"
```

### 4. Kata (hypervisor isolation)

```bash
./scandbox-run --runtime kata --agent pi \
  --llm http://127.0.0.1:8014 --llm-key testkey \
  --work "$PWD/work" -p "your task"
```

### 5. NVIDIA GPU

```bash
# Podman (shared GPU, host keeps using it):
./scandbox-run --runtime podman --agent pi-cuda --gpu nvidia \
  --llm http://127.0.0.1:8014 --work "$PWD/work" \
  --mount /opt/miniconda3:/opt/miniconda3 -p "profile the kernel"

# Docker (same, plus WSL2 auto-detection):
./scandbox-run --runtime docker --agent pi-cuda --gpu nvidia \
  --llm http://127.0.0.1:8014 --work "$PWD/work" -p "profile the kernel"
```

### 6. Interactive shell

```bash
./scandbox-run --runtime docker --agent pi --shell \
  --llm http://127.0.0.1:8014 --work "$PWD/work"
# self-wires, then drops to bash; run `pi --provider llamacpp ...` by hand---still recorded
```

## Agent images

- **CPU (`scandbox-pi`)**---needs `harness-pi` base:
  ```bash
  docker build -t scandbox-pi -f harnesses/pi/Dockerfile harnesses/
  ```
- **NVIDIA GPU (`scandbox-pi-cuda`, Ubuntu 22.04)**---build from the pi-agent repo root:
  ```bash
  docker build -f scandbox/harnesses/pi-cuda/Dockerfile.pi-cuda-ubuntu22.04 -t scandbox-pi-cuda .
  ```

## Reading the logs

```bash
RUN=$(ls -td ~/.local/state/scandbox/*/ | head -1)
python3 show-log.py "$RUN/plane1.jsonl"              # prompts/completions + tk/s
cat "$RUN/index.json"                                # provenance
```

Verify hash-chain integrity:
```bash
scandbox-gateway -verify "$RUN/plane1.jsonl"
# or from the image:
docker run --rm -v "$RUN:/logs:ro" scandbox-gateway -verify /logs/plane1.jsonl
```

## Key custody (Docker / Kata only)

The recorder holds the real upstream API key; the agent gets a dummy (`scandbox`) and
never sees the secret. Three ways to pass the key:

| Flag | How | Visible in `docker inspect`? |
|---|---|---|
| `--llm-key-file PATH` | mounted read-only into recorder | no |
| `--llm-key-env VAR` | read by scandbox-run, passed to recorder | no |
| `--llm-key VALUE` | inline on argv (warns) | yes |

The recorder strips whatever auth the agent sent and injects the real key upstream.

## Notes

- **LLM addressing (Docker):** `--llm http://127.0.0.1:PORT` is rewritten to
  `host.docker.internal` for the containerized recorder. The LLM must listen on
  `0.0.0.0`, not strictly `127.0.0.1`.
- **`--egress`** groups: `gh`, `hf`, `pypi`, or `all`. Omit for fully airgapped.
- **Each run is fresh**---ephemeral per-run `$HOME` under the audit dir; host
  `~/.pi` / `~/.claude` are never touched.
- **Hardening (Docker/Kata):** all containers run `--user`, `--security-opt=no-new-privileges`,
  `--cap-drop ALL`, `--pids-limit 4096`.
- **`--netns` (Apptainer):** enforced airgap via rootless `unshare` + `slirp4netns`.
  Without it, Apptainer shares the host network (policy-only airgap).
- **`--wire` / `--manifest` (Podman):** runtime-inject the self-wiring entrypoint into
  un-catalogued images (needs `jq`, `curl`, `bash` in the image).
- **Dry run:** `--dry-run` prints the planned commands without executing.

## Progress

### Harnesses

| harness | base OS | apptainer | podman/docker | notes |
|---|---|---|---|---|
| pi | Ubuntu 24.04 | [x] `scandbox-pi.sif` | [x] `scandbox-pi` | from-source build via bun |
| pi-cuda | Ubuntu 22.04 | [ ] | [x] `scandbox-pi-cuda` | CUDA 12.8; DL stack via mounted conda |
| claude-code |---| [ ] | [ ] | needs Node.js + npm; wire via `ANTHROPIC_BASE_URL` |
| codex |---| [ ] | [ ] | needs Node.js; wire via `OPENAI_BASE_URL` |

### Runtimes

| runtime | status | isolation | notes |
|---|---|---|---|
| `--runtime docker` | [x] | namespace (runc) | key custody, WSL2 GPU |
| `--runtime podman` | [x] | namespace (rootless) | `--wire`/`--manifest` for BYO images |
| `--runtime apptainer` | [x] | namespace + `--netns` | HPC/SLURM, SIF signatures |
| `--runtime kata` | [x] | hypervisor (microVM) | untrusted agents, dedicated GPU (VFIO) |
| `--runtime sbx` | [ ] planned | hypervisor (Docker Sandboxes) | Windows-native; proprietary runtime, open recorder |

sbx integration: scandbox-gateway runs as a Docker container on the host; sbx agent's
LLM endpoint points at it. Recorder-only mode---sbx owns the agent lifecycle, scandbox
provides the audit trail. See `which-containers-to-use.md` for the architecture.

### LLM endpoints

| endpoint | status | notes |
|---|---|---|
| OpenAI-compatible | [x] | llama.cpp, vLLM, SGLang, TGI---what the recorder proxies today |
| Anthropic | [ ] | different request/response schema; recorder needs a second codec |
| OpenAI native | [ ] | same schema as compatible, but auth + org headers differ |
