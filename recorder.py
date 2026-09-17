"""recorder.py---shared recorder lifecycle for all scandbox runtimes.

Manages the scandbox-gateway (start/stop), audit directory, key custody, provenance
index, egress domain lists, and plane-3 filesystem manifests. Runtime-agnostic: the
gateway can run as a Docker container, Podman container, or host process.

Imported by the runtime wrappers (runtimes/*.py) and by the unified scandbox-run CLI.
Also executable standalone:
    scandbox-recorder start --llm URL [--llm-key-file PATH] [--api openai|anthropic]
    scandbox-recorder stop [--run-id ID]
"""
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

HOME = Path.home()
SCRIPT_DIR = Path(__file__).resolve().parent

EGRESS_DOMAINS = {
    "gh": ["github.com", "api.github.com", "objects.githubusercontent.com", "raw.githubusercontent.com"],
    "hf": ["huggingface.co", "hf.co"],
    "pypi": ["pypi.org", "files.pythonhosted.org"],
}

AGENTS = {
    "pi": {"image": "scandbox-pi"},
    "pi-cuda": {"image": "scandbox-pi-cuda"},
}

DEFAULT_PORT = 8080
DEFAULT_AUDIT_ROOT = HOME / ".local" / "state" / "scandbox"
GATEWAY_IMAGE = "scandbox-gateway"
EGRESS_IMAGE = "scandbox-egress"


def die(m):
    print(f"error: {m}", file=sys.stderr)
    sys.exit(1)


def resolve_dir(p, label):
    d = Path(p).expanduser().resolve()
    if not d.is_dir():
        die(f"{label} '{p}' is not a directory")
    return d


def resolve_agent(name):
    entry = AGENTS.get(name)
    return entry["image"] if entry else name


# ---- key custody -------------------------------------------------------------

def resolve_llm_key(llm_key=None, llm_key_file=None, llm_key_env=None):
    """Where the REAL upstream key comes from. Returns (kind, value):
    ('file', path) | ('value', secret) | (None, None). Fed to the recorder only."""
    if llm_key_file:
        p = Path(llm_key_file).expanduser()
        if not p.is_file():
            die(f"--llm-key-file: not a file: {p}")
        return "file", str(p.resolve())
    if llm_key_env:
        v = os.environ.get(llm_key_env)
        if not v:
            die(f"--llm-key-env {llm_key_env}: not set in the environment")
        return "value", v
    if llm_key:
        print("warning: --llm-key puts the secret on argv (visible in `ps`); prefer --llm-key-file",
              file=sys.stderr)
        return "value", llm_key
    return None, None


# ---- recorder env (agent-facing) ---------------------------------------------

def recorder_env(host, port, key=None):
    """Env vars the agent needs to reach the recorder. The agent gets a dummy key;
    the real key lives in the recorder (key custody)."""
    base = f"http://{host}:{port}"
    k = key or "scandbox"
    env = {
        "PI_OFFLINE": "1", "LLAMA_HOST": host, "LLAMA_PORT": str(port),
        "OPENAI_BASE_URL": f"{base}/v1", "OPENAI_API_BASE": f"{base}/v1", "OPENAI_API_KEY": k,
        "ANTHROPIC_BASE_URL": base, "ANTHROPIC_API_KEY": k,
    }
    if key:
        env["LLAMA_API_KEY"] = key
    return env


# ---- egress domain list -------------------------------------------------------

def egress_for(groups):
    if not groups:
        return []
    req = [g.strip() for g in groups.split(",") if g.strip()]
    if "all" in req:
        req = list(EGRESS_DOMAINS)
    bad = [g for g in req if g not in EGRESS_DOMAINS]
    if bad:
        die(f"unknown egress groups: {', '.join(bad)}; have: {', '.join(EGRESS_DOMAINS)}")
    out = []
    for g in req:
        out += EGRESS_DOMAINS[g]
    return out


# ---- plane-3 filesystem manifest ----------------------------------------------

def manifest(dirs, do_hash=True, cap=50 * 1024 * 1024):
    out = {}
    for d in dirs:
        base = Path(d)
        if not base.exists():
            continue
        for f in base.rglob("*"):
            if not f.is_file() or f.is_symlink():
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            rec = {"size": st.st_size, "mtime": int(st.st_mtime)}
            if do_hash and st.st_size <= cap:
                try:
                    h = hashlib.sha256()
                    with open(f, "rb") as fh:
                        for chunk in iter(lambda: fh.read(1 << 20), b""):
                            h.update(chunk)
                    rec["sha256"] = h.hexdigest()
                except OSError:
                    pass
            out[str(f)] = rec
    return out


def manifest_diff(before, after):
    ch = []
    for p, a in after.items():
        b = before.get(p)
        if b is None:
            ch.append({"path": p, "status": "created", "sha256": a.get("sha256"), "size": a["size"]})
        elif b.get("sha256") != a.get("sha256") or b["size"] != a["size"]:
            ch.append({"path": p, "status": "modified",
                       "sha256_before": b.get("sha256"), "sha256_after": a.get("sha256")})
    for p, b in before.items():
        if p not in after:
            ch.append({"path": p, "status": "deleted", "sha256_before": b.get("sha256")})
    return ch


# ---- provenance index ---------------------------------------------------------

def write_index(audit_dir, run_id, *, runtime, status, agent, api, llm,
                gpu=None, egress=None, work=None, rc=None, started=None, **extra):
    """Write or update the run's index.json. `extra` passes through runtime-specific
    provenance fields (agent_image_digest, sif_sha256, signature, etc.)."""
    plane1 = audit_dir / "plane1.jsonl"
    plane2 = audit_dir / "access.log"
    chain_head, chain_len, _last = "", 0, ""
    if plane1.exists():
        with open(plane1) as _f:
            for _line in _f:
                if _line.strip():
                    chain_len += 1
                    _last = _line
        if _last:
            try:
                chain_head = json.loads(_last).get("record_hash", "")
            except (ValueError, TypeError):
                pass
    idx = {
        "run_id": run_id, "runtime": runtime, "status": status, "agent": agent,
        "api": api, "llm": llm, "gpu": gpu or "none", "egress": egress or "none",
        "started": int(started) if started else None,
        "ended": None if status == "running" else int(time.time()), "exit_code": rc,
        "work": [[str(p), m] for p, m in (work or [])],
        "planes": {"llm_log": str(plane1), "egress_log": str(plane2),
                   "fs_manifest": str(audit_dir / "plane3.json")},
        "counts": {
            "llm_records": chain_len,
            "egress_lines": sum(1 for _ in open(plane2)) if plane2.exists() else 0,
        },
        "chain": {"head": chain_head, "len": chain_len},
        **extra,
    }
    (audit_dir / "index.json").write_text(json.dumps(idx, indent=2))
    return idx


# ---- image helpers (shared by docker/podman/kata) -----------------------------

def image_exists(name, cmd="docker"):
    if cmd == "podman":
        return subprocess.run(["podman", "image", "exists", name], capture_output=True).returncode == 0
    return subprocess.run(["docker", "image", "inspect", name], capture_output=True).returncode == 0


def image_digest(name, cmd="docker"):
    r = subprocess.run([cmd, "inspect", "--format", "{{.Id}}", name], capture_output=True, text=True)
    img_id = r.stdout.strip() if r.returncode == 0 else None
    r2 = subprocess.run([cmd, "inspect", "--format", "{{range .RepoDigests}}{{.}} {{end}}", name],
                        capture_output=True, text=True)
    toks = r2.stdout.split() if r2.returncode == 0 else []
    return img_id, (toks[0] if toks else None)


# ---- network helpers (shared by docker/podman/kata) ---------------------------

def ensure_net(name, internal=True, cmd="docker"):
    if subprocess.run([cmd, "network", "inspect", name], capture_output=True).returncode != 0:
        args = [cmd, "network", "create"] + (["--internal"] if internal else []) + [name]
        subprocess.run(args, check=True)
        print(f"created network: {name}")


def net_connect(net, ctr, cmd="docker"):
    subprocess.run([cmd, "network", "connect", net, ctr], capture_output=True)


def rm_container(name, cmd="docker"):
    subprocess.run([cmd, "rm", "-f", name], capture_output=True)


# ---- GPU helpers --------------------------------------------------------------

def gpu_args_nvidia_docker():
    """NVIDIA GPU for Docker/Kata: explicit device nodes + host driver libs by SONAME.
    WSL2: auto-detect /dev/dxg and use --gpus all (the one exception)."""
    args, env = [], []
    if Path("/dev/dxg").exists():
        return ["--gpus", "all"], []
    for dev in ("/dev/nvidia0", "/dev/nvidiactl", "/dev/nvidia-uvm"):
        if Path(dev).exists():
            args += ["--device", dev]
    libdir = "/usr/lib/x86_64-linux-gnu"
    for so in ("libcuda.so.1", "libnvidia-ml.so.1", "libnvidia-ptxjitcompiler.so.1"):
        src = os.path.realpath(os.path.join(libdir, so))
        if os.path.isfile(src):
            args += ["-v", f"{src}:/opt/nvidia-driver/{so}:ro"]
    env += ["-e", "LD_LIBRARY_PATH=/opt/nvidia-driver:/usr/local/cuda/lib64"]
    return args, env


def gpu_args_podman(kind):
    """GPU passthrough for Podman: NVIDIA and/or Intel."""
    args, env = [], []
    if kind in ("nvidia", "both"):
        for dev in ("/dev/nvidia0", "/dev/nvidiactl", "/dev/nvidia-uvm"):
            if Path(dev).exists():
                args += ["--device", dev]
        libdir = "/usr/lib/x86_64-linux-gnu"
        for so in ("libcuda.so.1", "libnvidia-ml.so.1", "libnvidia-ptxjitcompiler.so.1"):
            src = os.path.realpath(os.path.join(libdir, so))
            if os.path.isfile(src):
                args += ["-v", f"{src}:/opt/nvidia-driver/{so}:ro"]
        env += ["-e", "LD_LIBRARY_PATH=/opt/nvidia-driver:/usr/local/cuda/lib64"]
    if kind in ("intel", "both"):
        for dev in ("/dev/dri/renderD128", "/dev/dri/card0"):
            if Path(dev).exists():
                args += ["--device", dev]
        env += ["-e", "UR_LOADER_USE_LEVEL_ZERO_V2=0"]
    return args, env


# ---- SIF provenance (apptainer) -----------------------------------------------

def sif_provenance(sif_path):
    sha = None
    try:
        h = hashlib.sha256()
        with open(sif_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        sha = h.hexdigest()
    except OSError:
        pass
    return sha, _sif_verify(sif_path)


def _sif_verify(sif_path):
    if not _have("apptainer"):
        return {"status": "unknown", "signer": None, "detail": "apptainer not on PATH"}
    r = subprocess.run(["apptainer", "verify", str(sif_path)], capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    if r.returncode == 0:
        signer = None
        for line in out.splitlines():
            s = line.strip()
            low = s.lower()
            if low.startswith("signing entity"):
                signer = s.split(":", 1)[1].strip()
            elif low.startswith("fingerprint") and not signer:
                signer = s.split(":", 1)[1].strip()
        return {"status": "verified", "signer": signer, "detail": out[:2000]}
    low = out.lower()
    unsigned = any(t in low for t in ("signature not found", "no signatures", "no signature",
                                      "not signed", "no objects"))
    return {"status": "unsigned" if unsigned else "unverified", "signer": None, "detail": out[:2000]}


def _have(prog):
    return subprocess.run(["sh", "-c", f"command -v {prog}"], capture_output=True).returncode == 0


def wait_port(host, port, timeout=10):
    import socket
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


# ---- run lifecycle (used by all runtimes) -------------------------------------

def create_run(audit_root=None, run_id=None):
    run_id = run_id or uuid.uuid4().hex[:16]
    audit_dir = Path(audit_root or DEFAULT_AUDIT_ROOT).expanduser() / run_id
    audit_dir.mkdir(parents=True, exist_ok=True)
    run_home = audit_dir / "home"
    run_home.mkdir(parents=True, exist_ok=True)
    return run_id, audit_dir, run_home


def parse_work_data(args):
    """Parse --work/--work-ro/--data/--data-rw into (work, data, write_dirs)."""
    work = [(resolve_dir(w, "--work"), "rw") for w in (args.work or [])]
    work += [(resolve_dir(w, "--work-ro"), "ro") for w in (args.work_ro or [])]
    data = [(resolve_dir(d, "--data"), "ro") for d in (args.data or [])]
    data += [(resolve_dir(d, "--data-rw"), "rw") for d in (args.data_rw or [])]
    write_dirs = [str(p) for p, m in work if m == "rw"]
    write_dirs += [str(p) for p, m in data if m == "rw"]
    return work, data, write_dirs


def run_with_provenance(run_fn, *, audit_dir, run_id, runtime, args, upstream,
                        work, write_dirs, no_hash=False, **index_extra):
    """Wrap an agent run with before/after manifest and index writes."""
    before = manifest(write_dirs, do_hash=not no_hash)
    started = time.time()
    write_index(audit_dir, run_id, runtime=runtime, status="running", agent=args.agent,
                api=args.api, llm=upstream, gpu=getattr(args, "gpu", None),
                egress=getattr(args, "egress", None), work=work, started=started, **index_extra)
    rc, status = None, "interrupted"
    try:
        rc = run_fn()
        status = "completed"
    finally:
        after = manifest(write_dirs, do_hash=not no_hash)
        (audit_dir / "plane3.json").write_text(json.dumps(
            {"run_id": run_id, "write_dirs": write_dirs,
             "changes": manifest_diff(before, after)}, indent=2))
        idx = write_index(audit_dir, run_id, runtime=runtime, status=status, agent=args.agent,
                          api=args.api, llm=upstream, gpu=getattr(args, "gpu", None),
                          egress=getattr(args, "egress", None), work=work,
                          rc=rc, started=started, **index_extra)
    print(f"\nprovenance: {audit_dir}/index.json  ({idx['counts']['llm_records']} llm records, {status})")
    return rc
