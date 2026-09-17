"""Kata Containers runtime wrapper---thin layer over Docker with --runtime kata-runtime."""
from runtimes.docker import run as docker_run


def run(args):
    args.container_runtime = "kata-runtime"
    return docker_run(args)
