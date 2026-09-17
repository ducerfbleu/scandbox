"""Docker runtime wrapper for scandbox."""
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from recorder import (
    GATEWAY_IMAGE, EGRESS_IMAGE, DEFAULT_PORT,
    die, ensure_net, net_connect, rm_container, image_exists, image_digest,
    resolve_llm_key, recorder_env, egress_for, gpu_args_nvidia_docker,
    create_run, parse_work_data, run_with_provenance,
)

AIRGAP_NET = "scandbox-airgap"
EGRESS_NET = "scandbox-net"
PROXY_NAME = "scandbox-egress-proxy"
HOME = Path.home()


def _gateway_upstream(url):
    """Rewrite loopback to host.docker.internal for the containerized recorder."""
    parts = urlsplit(url)
    if parts.hostname in ("127.0.0.1", "localhost", "0.0.0.0"):
        netloc = "host.docker.internal" + (f":{parts.port}" if parts.port else "")
        return urlunsplit(parts._replace(netloc=netloc))
    return url


def _squid_conf(domains):
    acls = "\n".join(f"acl allowed dstdomain {d}" for d in domains) or "# egress: none allowed"
    return (
        "acl SSL_ports port 443\nacl CONNECT method CONNECT\n\n"
        f"{acls}\n\n"
        "http_port 3128\n"
        "http_access allow CONNECT SSL_ports allowed\nhttp_access deny all\n\n"
        "cache deny all\npid_filename /tmp/squid.pid\ncoredump_dir /tmp\n"
        "logformat r %{%Y-%m-%d %H:%M:%S}tl %6tr %>a %Ss/%03>Hs %<st %rm %ru %Sh/%<a %mt\n"
        "access_log stdio:/var/log/squid/access.log r\ncache_log /dev/null\n"
    )


def _start_proxy(domains, audit_dir, uid_gid):
    rm_container(PROXY_NAME)
    ensure_net(EGRESS_NET, internal=True)
    conf = audit_dir / "squid.conf"
    conf.write_text(_squid_conf(domains))
    subprocess.run([
        "docker", "run", "-d", "--name", PROXY_NAME, "--network", EGRESS_NET,
        "--user", uid_gid, "--security-opt", "no-new-privileges", "--cap-drop", "ALL", "--pids-limit", "4096",
        "-v", f"{conf}:/etc/squid/squid.conf:ro",
        "-v", f"{audit_dir}:/var/log/squid", EGRESS_IMAGE,
    ], check=True)
    net_connect("bridge", PROXY_NAME)
    print(f"proxy: {PROXY_NAME} ({len(domains)} domains allowlisted)")


def run(args):
    agent_cmd = list(args.cmd)
    if agent_cmd and agent_cmd[0] == "--":
        agent_cmd = agent_cmd[1:]
    if args.prompt:
        agent_cmd += ["-p", args.prompt]

    run_id, audit_dir, run_home = create_run(args.audit_root, args.run_id)
    work, data, write_dirs = parse_work_data(args)
    upstream = _gateway_upstream(args.llm)
    verify = args.llm_verify
    egress = egress_for(args.egress)
    uid_gid = f"{os.getuid()}:{os.getgid()}"
    gw = args.recorder_name
    port = DEFAULT_PORT
    llm_leg = getattr(args, "llm_net", None) or "bridge"

    print(f"scandbox-run (docker): agent={args.agent} | llm={args.llm} | api={args.api} | "
          f"gpu={args.gpu or 'none'} | egress={args.egress or 'none'}")
    print(f"  run_id: {run_id}\n  audit:  {audit_dir}")

    # --- gateway env ---
    gw_env = {
        "GATEWAY_LISTEN": f":{port}", "GATEWAY_UPSTREAM": upstream,
        "GATEWAY_TLS_VERIFY": "1" if verify else "0",
        "GATEWAY_LOG": "/logs/plane1.jsonl", "GATEWAY_RUN_ID": run_id,
    }
    gw_cmd = ["docker", "run", "-d", "--name", gw, "--network", AIRGAP_NET, "--user", uid_gid,
              "--security-opt", "no-new-privileges", "--cap-drop", "ALL", "--pids-limit", "4096",
              "--add-host=host.docker.internal:host-gateway", "-v", f"{audit_dir}:/logs"]
    kind, keyval = resolve_llm_key(args.llm_key, getattr(args, "llm_key_file", None),
                                    getattr(args, "llm_key_env", None))
    if kind:
        hdr, pfx = ("x-api-key", "") if args.api == "anthropic" else ("Authorization", "Bearer ")
        gw_env["GATEWAY_UPSTREAM_KEY_HEADER"] = hdr
        gw_env["GATEWAY_UPSTREAM_KEY_PREFIX"] = pfx
        if kind == "file":
            gw_cmd += ["-v", f"{keyval}:/run/secrets/llm_key:ro"]
            gw_env["GATEWAY_UPSTREAM_KEY_FILE"] = "/run/secrets/llm_key"
        else:
            gw_env["GATEWAY_UPSTREAM_KEY"] = keyval
    for k, v in gw_env.items():
        gw_cmd += ["-e", f"{k}={v}"]
    gw_cmd.append(GATEWAY_IMAGE)

    # --- agent container ---
    aenv = recorder_env(gw, port)
    agent_name = f"scandbox-agent-{run_id[:12]}"
    runtime_flag = getattr(args, "container_runtime", None)
    create = ["docker", "create", "--rm", "-i", "--name", agent_name, "--network", AIRGAP_NET]
    if runtime_flag:
        create += ["--runtime", runtime_flag]
    if sys.stdin.isatty():
        create.append("-t")
    if not args.as_root:
        create += ["--user", uid_gid]
    create += ["--security-opt", "no-new-privileges", "--cap-drop", "ALL", "--pids-limit", "4096"]
    create += ["-e", f"HOME={HOME}", "-v", f"{run_home}:{HOME}"]
    for k, v in aenv.items():
        create += ["-e", f"{k}={v}"]
    if egress:
        purl = f"http://{PROXY_NAME}:3128"
        for v in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            create += ["-e", f"{v}={purl}"]
        noproxy = f"{gw},127.0.0.1,localhost"
        for v in ("NO_PROXY", "no_proxy"):
            create += ["-e", f"{v}={noproxy}"]
    for p, m in work:
        create += ["-v", f"{p}:{p}" + (":ro" if m == "ro" else "")]
    for p, m in data:
        create += ["-v", f"{p}:{p}" + (":ro" if m == "ro" else "")]
    for spec in (args.mount or []):
        parts = spec.split(":")
        if len(parts) < 2:
            die(f"--mount must be HOST:CONTAINER[:ro], got '{spec}'")
        parts[0] = str(Path(parts[0]).expanduser().resolve())
        create += ["-v", ":".join(parts)]
    if args.gpu:
        ga, ge = gpu_args_nvidia_docker()
        create += ga + ge
    if args.shell:
        create += ["-e", "SCANDBOX_SHELL=1"]
    create += ["-w", str(work[0][0]) if work else "/workspace", args.agent]
    if agent_cmd:
        create += agent_cmd

    if args.dry_run:
        import shlex
        print("# recorder (dual-homed: %s + %s):\n  %s" % (AIRGAP_NET, llm_leg, " ".join(shlex.quote(c) for c in gw_cmd)))
        if egress:
            print(f"# proxy: docker run -d --name {PROXY_NAME} --network {EGRESS_NET} + bridge  ({len(egress)} domains)")
        print("# agent:\n  " + " ".join(shlex.quote(c) for c in create))
        return 0

    # --- preflight ---
    if not image_exists(args.agent):
        die(f"agent image '{args.agent}' not found")
    if not image_exists(GATEWAY_IMAGE):
        die(f"recorder image '{GATEWAY_IMAGE}' not found---run ./build.sh")
    if egress and not image_exists(EGRESS_IMAGE):
        die(f"proxy image '{EGRESS_IMAGE}' not found---run ./build.sh")

    img_id, repo = image_digest(args.agent)
    print(f"agent: {args.agent}  digest={img_id}")
    if "@sha256:" not in args.agent:
        print("warning: agent image is an unpinned tag---pin as name@sha256:… for a verified run",
              file=sys.stderr)

    # --- start recorder ---
    ensure_net(AIRGAP_NET, internal=True)
    rm_container(gw)
    subprocess.run(gw_cmd, check=True)
    net_connect(llm_leg, gw)
    print(f"recorder: {gw} -> {upstream} (verify={int(verify)}, leg={llm_leg})")
    if egress:
        _start_proxy(egress, audit_dir, uid_gid)

    # --- run agent ---
    cid = subprocess.run(create, capture_output=True, text=True, check=True).stdout.strip()
    if egress:
        net_connect(EGRESS_NET, cid)
    print(f"  shell in (from another terminal): docker exec -it {agent_name} bash")

    def agent_fn():
        return subprocess.run(["docker", "start", "-a", "-i", cid]).returncode

    try:
        rc = run_with_provenance(
            agent_fn, audit_dir=audit_dir, run_id=run_id, runtime="docker",
            args=args, upstream=upstream, work=work, write_dirs=write_dirs,
            no_hash=args.no_hash, agent_image_digest=img_id, agent_repo_digest=repo)
    finally:
        rm_container(gw)
        if egress:
            rm_container(PROXY_NAME)
    return rc
