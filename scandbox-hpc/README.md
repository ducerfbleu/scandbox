# scandbox-hpc---usage

Run the **pi** agent on HPC (Apptainer, rootless, no daemon), sandboxed to a work dir and fully
recorded, against a remote LLM. Each run is fresh/ephemeral---built for benchmarks. Only the
recorder dials the LLM; the agent is airgapped and never reaches it directly. **A URL + key is the
whole LLM contract.**

## What's here

```
scandbox-run              apptainer-only launcher (Python, stdlib only)
netns-run                 rootless airgap wrapper (unshare + slirp4netns)
scandbox-pi.sif           the pi agent (CPU)
scandbox-pi-cuda.sif      the pi agent (GPU, CUDA toolkit baked in)
bin/scandbox-gateway      L7 recorder (records plane1.jsonl)  ---prebuilt static x86-64
bin/scandbox-egress       egress allow-list proxy             ---prebuilt static x86-64
gateway/ egress/          recorder + egress source (rebuild: CGO_ENABLED=0 go build -o ../bin/<name> .)
show-log.py               read logs + throughput (tk/s)
bpe_tokenizer.py          offline BPE tokenizer for exact reasoning/writing token split
tokenizers/               local tokenizer data (auto-matched to model by show-log.py)
examples/gemm.cu          sample CUDA GEMM (benchmark work item)
build-pi-cuda.sh          build scandbox-pi-cuda.sif from the docker image (on a docker+apptainer host)
tips-launching-hpc.md     troubleshooting---ports, proxies, TLS, netns
```

## 0. Load the runtime

```bash
ml apptainer
ml slirp4netns          # optional; needed only for --netns (enforced airgap)
command -v apptainer slirp4netns
```

## 1. Sanity-check + preview

```bash
cd /path/to/scandbox-hpc          # wherever you scp'd it

apptainer inspect scandbox-pi.sif
apptainer inspect --runscript scandbox-pi.sif

# preview the EXACT plan without running (works even without apptainer loaded)
mkdir -p work
./scandbox-run --agent scandbox-pi.sif --port 8090 --llm http://127.0.0.1:8080 --llm-key testkey \
  --netns --egress pypi --work "$PWD/work" -p "hello" --dry-run
```

## 2. Run

```bash
# 2a. shared / broadcast endpoint (URL + key you were given)
./scandbox-run --agent scandbox-pi.sif \
  --llm https://llm.cluster.example:8443 --llm-key "$LLM_KEY" --llm-verify \
  --netns --work "$PWD/work" -p "your benchmark task"

# 2b. an LLM you deployed on THIS compute node
#     (localhost works---the recorder is a host process, not a container)
#     e.g. first:  llama-server -m model.gguf --host 127.0.0.1 --port 8080 --api-key testkey
./scandbox-run --agent scandbox-pi.sif \
  --port 8090 --llm http://127.0.0.1:8080 --llm-key testkey \
  --netns --work "$PWD/work" -p "your benchmark task"
```

## Interactive shell (run pi by hand inside the sandbox)

Use `--shell` (and omit `-p`) to self-wire, then drop into a bash **inside** the airgapped, recorded
sandbox---instead of running a single prompt:

```bash
./scandbox-run --agent scandbox-pi.sif --port 8090 --llm http://127.0.0.1:8080 --llm-key testkey \
  --netns --work "$PWD/work" --shell
```

It writes `~/.pi/agent/models.json` (pi → recorder), prints the exact `pi …` command, then hands you a
shell. Run pi yourself---still through the recorder + airgap:

```bash
pi --provider llamacpp -p "your task"      # or just: pi --provider llamacpp  (interactive)
```

(`apptainer shell scandbox-pi.sif` directly would BYPASS the recorder---no wiring, no airgap, no
logging. Use `--shell` instead.)

## 3. Read the logs

The run prints its audit dir on the `audit:` line. Then:

```bash
RUN=$(ls -td ~/.local/state/scandbox/*/ | head -1)   # newest run
python3 show-log.py "$RUN/plane1.jsonl"              # prompts/completions + tk/s
./bin/scandbox-gateway -verify "$RUN/plane1.jsonl"   # hash-chain integrity
cat "$RUN/index.json"                                # provenance summary
```

Each run dir holds: `plane1.jsonl` (LLM I/O, hash-chained), `access.log` (egress, with `--egress`),
`plane3.json` (fs changes), `index.json` (provenance), `recorder.log` / `egress.log`.

## Example---a CUDA coding task

`examples/gemm.cu` is a self-contained CUDA GEMM (a naive and a shared-memory-tiled kernel; it checks
correctness against a CPU reference and reports GFLOP/s, `./gemm [N]`). Use it as a benchmark work item.

```bash
mkdir -p work && cp examples/gemm.cu work/
```

Two ways to run it, depending on whether the **agent** should build/run on the GPU itself.

### A. Agent builds + runs on the GPU inside its sandbox (self-contained loop)

Needs a **CUDA-capable pi SIF** (`scandbox-pi-cuda.sif`, CUDA toolkit baked in) and **`--gpu nvidia`**
(→ apptainer `--nv`: binds the host driver + `/dev/nvidia*`; the toolkit comes from the SIF). GPU
composes with `--netns`---the netns only blocks network, not the local GPU:

```bash
./scandbox-run --agent scandbox-pi-cuda.sif --gpu nvidia \
  --port 8090 --llm http://127.0.0.1:8080 --llm-key testkey \
  --netns --work "$PWD/work" \
  -p "In work/gemm.cu speed up gemm_tiled; build with 'nvcc -O3 gemm.cu -o gemm' and run './gemm 1024' to confirm it still PASSes and GFLOP/s went up."
```

The agent now compiles + runs CUDA *within* the recorded, airgapped sandbox---a real write→build→run
loop (the built binary + outputs show up in `plane3.json`).

**Get the CUDA SIF** (none ships here). On a host with docker + apptainer, run the bundled builder —
the `scandbox-pi-cuda` image already exists, so it just converts it:

```bash
./build-pi-cuda.sh      # docker-daemon://scandbox-pi-cuda → ./scandbox-pi-cuda.sif (+ .provenance.json), then verifies
```

Use it in place, or if you built elsewhere: `scp scandbox-pi-cuda.sif <cluster>:/path/to/scandbox-hpc/`.
(Need to rebuild the image first? `docker compose -f scandbox/docker-compose.pi-cuda.yml build` from the pi-agent repo root.)

### B. Agent edits only; you build/run via a CUDA apptainer (no CUDA SIF needed)

Keep the CPU `scandbox-pi.sif`; the agent edits the kernel (recorded), and you compile + run it in a
CUDA container as a separate verification step:

```bash
./scandbox-run --agent scandbox-pi.sif --port 8090 --llm http://127.0.0.1:8080 --llm-key testkey \
  --netns --work "$PWD/work" \
  -p "In work/gemm.cu, make gemm_tiled faster without breaking the correctness check or the ./gemm [N] CLI."

apptainer exec --nv docker://nvidia/cuda:12.8.1-devel-ubuntu24.04 \
  bash -c 'cd work && nvcc -O3 gemm.cu -o gemm && ./gemm 1024'
#   (or a local CUDA .sif instead of docker://…, or the host toolchain: `ml cuda && nvcc …`)
```

Either way the LLM I/O is captured in `plane1.jsonl`; in **A** the compile/run also happens inside the
recorded sandbox, in **B** it's external.

## Rebuild the binaries (optional---only for a different CPU arch)

`bin/` ships prebuilt x86-64 static recorders. To build from source (e.g. a non-x86 cluster), load Go
and rebuild---stdlib-only, no network needed:

```bash
ml go                 # or: module load go   (check: module avail go)
cd gateway && CGO_ENABLED=0 go build -o ../bin/scandbox-gateway . && cd ..
cd egress  && CGO_ENABLED=0 go build -o ../bin/scandbox-egress  . && cd ..
```

## Notes

- **`--llm-verify`**---only for a real TLS cert; **drop it** for plain-`http` or self-signed (off by default).
- **No `slirp4netns`?** Drop `--netns`---it still runs and records, but the airgap becomes *policy-only*
  (the SIF is handed the recorder URL, never `--llm`) instead of kernel-enforced. The run prints a WARNING
  telling you which mode you're in.
- **`--egress`** is optional; omit it for a fully airgapped agent (reaches only the LLM). Add
  `--egress pypi,gh` (groups: `gh`, `hf`, `pypi`, or `all`) only if the task needs web access.
- **Benchmark loop:** call `scandbox-run` once per task; each gets its own `<run_id>/` dir.
- **Endpoint must be OpenAI-compatible** (llama.cpp / vLLM / TGI / SGLang). The SIF speaks
  OpenAI-completions to the upstream.
- **Troubleshooting:** watch the `recorder:` / `egress:` startup lines and the SIF-signature warning
  (expected---unsigned). If the agent can't reach the model, it's almost always the recorder→endpoint
  hop (compute-node egress or wrong URL), not the agent. Re-check the plan with `--dry-run`.
