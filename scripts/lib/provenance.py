"""Provenance record of one script invocation (scripts/lib/common.sh provenance)."""
import datetime
import hashlib
import json
import os
import platform
import subprocess
import sys


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def sh(*args, cwd=None):
    try:
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:
        return ""


def git(repo):
    if not os.path.isdir(repo):
        return None
    return {"commit": sh("git", "rev-parse", "HEAD", cwd=repo),
            "branch": sh("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=repo),
            "dirty": bool(sh("git", "status", "--porcelain", "--untracked-files=no", cwd=repo))}


def cpu():
    c = {"logical_cpus": os.cpu_count(), "affinity": len(os.sched_getaffinity(0))}
    for line in open("/proc/cpuinfo"):
        k, _, v = line.partition(":")
        k = k.strip()
        if k == "model name":
            c.setdefault("model", v.strip())
        elif k == "flags" and "isa" not in c:
            f = set(v.split())
            c["isa"] = [x for x in ("sse4_2", "avx", "avx2", "fma", "avx512f") if x in f]
    return c


def nes(repo, native, bundle, image):
    if native == "1":
        info = {}
        p = os.path.join(bundle, "BUILD_INFO")
        if os.path.exists(p):
            for line in open(p):
                k, _, v = line.strip().partition("=")
                if k:
                    info[k] = v.strip("'\"")
        return {"runtime": "native", "bundle": bundle, "build_info": info}
    return {"runtime": "docker", "image": image,
            "image_id": sh("docker", "image", "inspect", "-f", "{{.Id}}", image), "git": git(repo)}


def stores(args):
    out = {}
    for s in args:
        m = os.path.join(s, "manifest.json")
        if os.path.exists(m):
            raw = open(m, "rb").read()
            out[os.path.realpath(s)] = {"manifest_sha256": hashlib.sha256(raw).hexdigest(),
                                        "frames": len(json.loads(raw)["frames"])}
    return out


def start(path, bench, nes_repo, native, bundle, image, script, *args):
    try:
        import numpy
        import mabench
        py = {"python": platform.python_version(), "numpy": numpy.__version__, "mabench": mabench.__version__}
    except Exception:
        py = {"python": platform.python_version()}
    mem = {}
    for line in open("/proc/meminfo"):
        k = line.split(":")[0]
        if k in ("MemTotal", "MemAvailable"):
            mem[k] = int(line.split()[1]) * 1024
    rec = {"command": " ".join([script, *args]), "started_utc": now(), "host": platform.node(),
           "os": sh("sh", "-c", ". /etc/os-release && echo \"$PRETTY_NAME\""), "kernel": platform.release(),
           "cpu": cpu(), "memory_bytes": mem, "bench": git(bench), "nes": nes(nes_repo, native, bundle, image),
           "java": sh("sh", "-c", "java -version 2>&1 | grep -v '^Picked up' | head -1"), "python": py, "stores": stores(args),
           "settings": os.path.basename(path)[:-5] + ".env"}
    with open(path, "w") as f:
        json.dump(rec, f, indent=2)


def end(path, rc):
    with open(path) as f:
        rec = json.load(f)
    rec.update(finished_utc=now(), exit_status=int(rc))
    with open(path, "w") as f:
        json.dump(rec, f, indent=2)


if __name__ == "__main__":
    {"start": start, "end": end}[sys.argv[1]](*sys.argv[2:])
