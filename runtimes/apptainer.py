"""Apptainer (HPC) runtime wrapper for scandbox."""
import os
import subprocess
import sys
from pathlib import Path

from recorder import (
    DEFAULT_PORT, SCRIPT_DIR,
    die, recorder_env, egress_for, sif_provenance, _have, wait_port,
    create_run, parse_work_data, run_with_provenance,
)

HOME = Path.home()


def _parse_mount(spec):
    parts = spec.split(":")
    if len(parts) == 2:
        host, ctr, mode = parts[0], parts[1], "rw"
    elif len(parts) == 3:
        host, ctr, mode = parts
        if mode not in ("ro", "rw"):
            die(f"--mount '{spec}': mode must be 'ro' or 'rw', got '{mode}'")
    else:
        die(f"--mount '{spec}': expected HOST:CONTAINER[:ro|rw]")
    if not ctr.startswith("/"):
        die(f"--mount '{spec}': container path must be absolute")
    from recorder import resolve_dir
    return (resolve_dir(host, "--mount"), Path(ctr), mode)


def _policy_note(args):
    if args.netns:
        print("note: --netns → ENFORCED airgap via netns-run (rootless unshare + slirp4netns)",
              file=sys.stderr)
    else:
        print("WARNING: no --netns---Apptainer SHARES THE HOST NETWORK. Airgap is POLICY only.\n"
              "         For real enforcement use --netns + slirp4netns.", file=sys.stderr)


def run(args):
    agent_cmd = list(args.cmd)
    if agent_cmd and agent_cmd[0] == "--":
        agent_cmd = agent_cmd[1:]
    if args.prompt:
        agent_cmd += ["-p", args.prompt]

    run_id, audit_dir, run_home = create_run(args.audit_root, args.run_id)
    run_scratch = audit_dir / "scratch"
    run_scratch.mkdir(parents=True, exist_ok=True)
    work, data, write_dirs = parse_work_data(args)
    mounts = [_parse_mount(m) for m in (args.mount or [])]
    write_dirs += [str(h) for h, _, m in mounts if m == "rw"]
    upstream = args.llm
    verify = args.llm_verify
    egress = egress_for(args.egress)
    port = getattr(args, "port", DEFAULT_PORT)

    print(f"scandbox-run (apptainer/HPC): agent={args.agent} | llm={upstream} | api={args.api} | "
          f"egress={args.egress or 'none'}")
    print(f"  run_id: {run_id}\n  audit:  {audit_dir}")

    listen_host = "127.0.0.1"
    reach_host = getattr(args, "recorder_host", None) or ("10.0.2.2" if args.netns else "127.0.0.1")
    gateway_bin = str(SCRIPT_DIR / "bin" / "scandbox-gateway")
    egress_bin = str(SCRIPT_DIR / "bin" / "scandbox-egress")

    rec_env = {
        "GATEWAY_LISTEN": f"{listen_host}:{port}", "GATEWAY_UPSTREAM": upstream,
        "GATEWAY_TLS_VERIFY": "1" if verify else "0",
        "GATEWAY_LOG": str(audit_dir / "plane1.jsonl"), "GATEWAY_RUN_ID": run_id,
    }
    aenv = recorder_env(reach_host, port, args.llm_key)
    for v in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        aenv[v] = ""
    aenv["NO_PROXY"] = aenv["no_proxy"] = f"{reach_host},127.0.0.1,localhost"

    egr_port = port + 1
    if egress:
        purl = f"http://{reach_host}:{egr_port}"
        for v in ("HTTPS_PROXY", "https_proxy"):
            aenv[v] = purl
        noproxy = f"{reach_host},127.0.0.1,localhost"
        for v in ("NO_PROXY", "no_proxy"):
            aenv[v] = noproxy

    if args.shell:
        aenv["SCANDBOX_SHELL"] = "1"
    app = ["apptainer", "run", "--contain", "--cleanenv",
           "--workdir", str(run_scratch), "--home", f"{run_home}:{HOME}"]
    if args.gpu == "nvidia":
        app += ["--nv"]
    if work:
        app += ["--pwd", str(work[0][0])]
    for k, v in aenv.items():
        app += ["--env", f"{k}={v}"]
    for p, m in work:
        app += ["--bind", f"{p}:{p}" + (":ro" if m == "ro" else "")]
    for p, m in data:
        app += ["--bind", f"{p}:{p}" + (":ro" if m == "ro" else "")]
    for host, ctr, m in mounts:
        app += ["--bind", f"{host}:{ctr}" + (":ro" if m == "ro" else "")]
    app += [args.agent] + agent_cmd
    if args.netns:
        app = [str(SCRIPT_DIR / "netns-run"), "--"] + app

    if args.dry_run:
        envs = " ".join(f"{k}={v}" for k, v in rec_env.items())
        print(f"# recorder (host process):\n  {envs} {gateway_bin}")
        if egress:
            print(f"# egress proxy:\n  EGRESS_LISTEN=127.0.0.1:{egr_port} "
                  f"EGRESS_ALLOW={','.join(egress)} {egress_bin}")
        print("# agent:\n  " + " ".join(app))
        _policy_note(args)
        return 0

    # --- preflight ---
    if not Path(gateway_bin).exists():
        die(f"recorder binary not found: {gateway_bin}")
    if not _have("apptainer"):
        die("apptainer not found on PATH")
    if args.agent.endswith(".sif") and not Path(args.agent).exists():
        die(f"SIF not found: {args.agent}")
    if egress and not Path(egress_bin).exists():
        die(f"egress proxy binary not found: {egress_bin}")

    sif_sha, sig = sif_provenance(args.agent) if Path(args.agent).is_file() else (None, None)
    if sif_sha:
        print(f"agent: {args.agent}  sif_sha256={sif_sha}")
        if sig and sig["status"] == "verified":
            print(f"  SIF verified (signer: {sig['signer']})")
        else:
            st = sig["status"] if sig else "unknown"
            print(f"warning: SIF signature {st}---supply chain not verified", file=sys.stderr)
    _policy_note(args)

    # --- start recorder (host process) ---
    reclog = open(audit_dir / "recorder.log", "w")
    rec = subprocess.Popen([gateway_bin], env={**os.environ, **rec_env}, stdout=reclog, stderr=subprocess.STDOUT)
    egr_proc, egr_log = None, None
    if egress:
        egr_log = open(audit_dir / "egress.log", "w")
        egr_proc = subprocess.Popen(
            [egress_bin], stdout=egr_log, stderr=subprocess.STDOUT,
            env={**os.environ, "EGRESS_LISTEN": f"127.0.0.1:{egr_port}", "EGRESS_ALLOW": ",".join(egress),
                 "EGRESS_PORTS": "443,80", "EGRESS_LOG": str(audit_dir / "access.log")})
        print(f"egress: {len(egress)} domains allow-listed (127.0.0.1:{egr_port})")

    wait_port(listen_host, port, timeout=10)
    if rec.poll() is not None:
        die(f"recorder exited on start---is 127.0.0.1:{port} already taken?")
    if egr_proc is not None:
        wait_port("127.0.0.1", egr_port, timeout=10)
        if egr_proc.poll() is not None:
            die(f"egress proxy exited on start")

    def agent_fn():
        return subprocess.run(app).returncode

    try:
        rc = run_with_provenance(
            agent_fn, audit_dir=audit_dir, run_id=run_id, runtime="apptainer",
            args=args, upstream=upstream, work=work, write_dirs=write_dirs,
            no_hash=args.no_hash, sif_sha256=sif_sha, signature=sig)
    finally:
        rec.terminate()
        try:
            rec.wait(timeout=5)
        except subprocess.TimeoutExpired:
            rec.kill()
        reclog.close()
        if egr_proc is not None:
            egr_proc.terminate()
            try:
                egr_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                egr_proc.kill()
            egr_log.close()
    return rc
