# scandbox

## Scientifically-verifiable Agent Sandbox 

Capture-first recorder for AI coding agents. Records every LLM exchange (hash-chained),
egress attempt, and filesystem change---three audit planes---while the agent runs in a
structural airgap. The agent reaches only the recorder; the recorder reaches the LLM.

Two standalone runtimes, same recording architecture:

| | **scandbox-hpc** | **scandbox-podman** |
|---|---|---|
| runtime | Apptainer (SIF) | Podman (OCI) |
| target | HPC clusters | workstations, CI, edge |
| airgap | netns-run (rootless unshare + slirp4netns) | `--internal` podman network (netavark) |
| recorder | host process | container (dual-homed) |
| images | `.sif` shipped in dir | `.tar` shipped in dir, `podman load -i` |
| daemon | none | none |
| rootless | always | default (pasta) |

Both are stdlib-only Python + Go (no pip, no go.mod deps). Each directory is
self-contained---scp it to the target and run.

## Quick start

```bash
# HPC
cd scandbox-hpc
ml apptainer
./scandbox-run --agent scandbox-pi.sif --llm http://127.0.0.1:8080 --llm-key KEY \
  --netns --work "$PWD/work" -p "your task"

# Podman
cd scandbox-podman
ml podman
podman load -i scandbox-pi.tar scandbox-gateway.tar scandbox-egress.tar
./scandbox-run --agent pi --llm http://HOST:PORT --llm-key KEY \
  --work "$PWD/work" -p "your task"
```

## What gets recorded

Each run writes `~/.local/state/scandbox/<run_id>/`:

| file | contents |
|---|---|
| `plane1.jsonl` | every LLM exchange---prompt, completion, tool calls, usage, timing---SHA-256 hash-chained |
| `access.log` | egress allow/deny per attempt (with `--egress`) |
| `plane3.json` | filesystem before/after manifest |
| `index.json` | run metadata, image digest, chain anchor |

See `usage.md` in each subdirectory for full options.
