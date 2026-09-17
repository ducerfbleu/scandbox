# scandbox-hpc---launching tips & gotchas

Field notes for getting a run to actually connect on HPC. Read the mental model first---most "errors" are just the port/scheme/proxy not lined up, not scandbox itself.

## Mental model (this clears up 80% of confusion)

- The **recorder** is a **host process** on `127.0.0.1:PORT` (default **8080**). It is the *only* thing that dials `--llm`.
- Flow: **agent (SIF) → recorder** (`127.0.0.1:PORT`, or `10.0.2.2:PORT` under `--netns`) **→ `--llm`**.
- So pi hitting `…:PORT/v1/chat/completions` is **correct**---that's the recorder. The LLM is reached *by the recorder*, never by the agent directly. Don't "fix" pi to hit the LLM port.
- Because the recorder is a host process, `--llm http://127.0.0.1:PORT` reaches a host-local LLM **directly** (no `host.docker.internal`---that's a docker-only concern).

## 1. Port: the recorder and the LLM are different endpoints

- Recorder defaults to **8080**. If your LLM (llama-server) is also on 8080 → `recorder exited on start---…already taken`, while the LLM keeps answering the health check. **Move the recorder:** `--port 8090`.
- The recorder port and `--llm` must differ (don't set `--llm …:8090` when `--port 8090`---that's a self-loop).
- Who owns a port: `ss -ltnp | grep ':8080'` (or `lsof -nP -iTCP:8080 -sTCP:LISTEN`).
- A **stale recorder** from a killed run holding the port → `pkill -f bin/scandbox-gateway`.

## 2. `--llm`: match the scheme (http vs https)

- Point `--llm` at what llama-server actually serves. Check:
  ```bash
  curl -s  http://127.0.0.1:8080/health ; echo "http rc=$?"
  curl -sk https://127.0.0.1:8080/health ; echo "https rc=$?"
  ```
- TLS llama-server (log says `listening on https://…`) → `--llm https://127.0.0.1:8080`.
- Self-signed cert → leave **`--llm-verify` OFF** (default). Only add it for a CA-trusted cert.
- A plain-`http` `--llm` against a TLS server (or vice-versa) shows up as `502` / connection errors.

## 3. `--netns` (enforced airgap) has prerequisites

`--netns` gives a kernel-enforced airgap, but needs **both**:
```bash
command -v slirp4netns                              # must be on PATH
unshare --user --map-root --net true ; echo rc=$?   # rc=0 => unprivileged userns allowed
```
- Missing `slirp4netns` → install (`apt install slirp4netns` / `module load …` / `conda install -c conda-forge slirp4netns`).
- `unshare` fails with *Operation not permitted* → the **site disables unprivileged user namespaces**; `--netns` can't work there at all.
- Either way you can **run without `--netns`**---the airgap becomes *policy* (agent is handed only the recorder URL, not `--llm`) instead of enforced, but **everything is still recorded** (plane1, throughput, plane3, egress log). For benchmarking that's usually fine.

## 4. HPC site HTTP proxy leaks into the container

Symptom: a **Squid error page**---`The following error was encountered while trying to retrieve the URL: http://127.0.0.1:PORT/… (111) Connection refused`. Cause: apptainer preserves `http_proxy`/`https_proxy` **even with `--cleanenv`**, so pi routes its *local* recorder call through the site squid, which can't reach `127.0.0.1:PORT` from its own host.

- The launcher now clears these for the agent automatically.
- On an older copy, work around it: `unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY` before `./scandbox-run …` (the agent never needs a proxy---it only talks to the local recorder). Confirm with `env | grep -i proxy`.

## 5. Serving llama-server over TLS (optional)

Only needed if the recorder→LLM hop crosses an untrusted network; for a local/trusted LLM, plain HTTP is fine. If you do want TLS:
```bash
openssl req -x509 -newkey rsa:2048 -nodes -keyout server.key -out server.crt -days 365 -subj "/CN=localhost"
chmod 600 server.key
llama-server -m model.gguf --host 0.0.0.0 --port 8080 --ssl-cert-file server.crt --ssl-key-file server.key
#   log should say:  listening on https://0.0.0.0:8080
```
Needs a llama.cpp built with OpenSSL---check `llama-server --help | grep -i ssl`; if absent, rebuild (`-DLLAMA_OPENSSL=ON`) or just serve HTTP.

## 6. Diagnostics (in order)

```bash
RUN=$(ls -td ~/.local/state/scandbox/*/ | head -1)         # newest run dir
cat "$RUN/recorder.log"                                     # recorder start errors (bind, etc.)
python3 -c "import json;r=json.loads(open('$RUN/plane1.jsonl').readline());print(r.get('status'),'|',r.get('error'),'|',r.get('endpoint'))"
curl -s http://127.0.0.1:8090/v1/models                    # the recorder→LLM path (during a run; recorder is plain HTTP on the agent side)
```
- `502` + `error: …` → recorder can't reach `--llm` (scheme/reachability). Read the error string.
- Squid HTML page → §4 (proxy leak).
- `already taken` → §1 (port).

## Canonical working command (keyless local TLS LLM on 8080)

```bash
./scandbox-run --agent scandbox-pi.sif \
  --port 8090 \                         # recorder (LLM keeps 8080)
  --llm https://127.0.0.1:8080 \        # https, self-signed → verify stays off
  --work "$PWD/work" -p "explain what's in this directory"
#   add --netns only if slirp4netns + userns are available (§3)
#   add --llm-key-file … only if llama-server was started with --api-key
```

## One-line checklist

port (`--port` ≠ LLM) · scheme (`http`/`https` matches) · verify (off for self-signed) · netns (only if slirp4netns+userns) · proxy (cleared/unset) · key (only if the server has `--api-key`).
