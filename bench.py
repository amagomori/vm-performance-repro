import argparse
import hashlib
import http.client
import json
import os
import platform
import re
import shutil
import socket
import statistics
import subprocess
import time
from pathlib import Path

ROOT = Path(os.environ.get("FC_ROOT", "/srv/fc"))
BASE = ROOT / "base"
VMS = ROOT / "vms"
SNAPS = ROOT / "snapshots"
BOOT_ARGS = "keep_bootcon console=ttyS0 reboot=k panic=1"
MEM_MIB = 512
SETUP = (
    "dd if=/dev/urandom of=/root/deps.bin bs=1M count=50 status=none && sync"
    " && dd if=/dev/urandom of=/dev/shm/state bs=1M count=200 status=none"
    " && sleep 5 && date +%s > /root/setup-done && echo SETUP-DONE"
)


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost")
        self.sock_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(str(self.sock_path))


class VM:
    def __init__(self, name):
        self.dir = VMS / name
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True)
        self.sock = self.dir / "api.sock"
        self.log = self.dir / "console.log"
        fifo = self.dir / "console.in"
        os.mkfifo(fifo)
        self.stdin = os.open(fifo, os.O_RDWR)
        self.proc = subprocess.Popen(["firecracker", "--api-sock", str(self.sock)], stdin=self.stdin,
                                     stdout=open(self.log, "wb"), stderr=subprocess.STDOUT)
        deadline = time.time() + 5
        while not self.sock.exists():
            if time.time() > deadline:
                raise RuntimeError(f"{name}: API socket never appeared")
            time.sleep(0.005)
        self.offset = 0

    def api(self, method, path, body=None):
        conn = UnixHTTP(self.sock)
        conn.request(method, path, json.dumps(body) if body is not None else None, {"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read().decode()
        if resp.status >= 300:
            raise RuntimeError(f"{method} {path} -> {resp.status} {data}")
        return data

    def send(self, text):
        os.write(self.stdin, text.encode())

    def wait_for(self, pattern, timeout=60):
        deadline = time.time() + timeout
        rx = re.compile(pattern)
        while time.time() < deadline:
            text = self.log.read_bytes()[self.offset:].decode(errors="replace")
            m = rx.search(text)
            if m:
                self.offset += len(text[:m.end()].encode(errors="replace"))
                return
            time.sleep(0.01)
        raise TimeoutError(f"{self.dir.name}: no {pattern!r} after {timeout}s")

    def mem_mib(self):
        rollup = Path(f"/proc/{self.proc.pid}/smaps_rollup").read_text()
        return {k: int(v) / 1024 for k, v in re.findall(r"^(Rss|Pss):\s+(\d+) kB", rollup, re.M)}

    def kill(self):
        self.proc.kill()
        self.proc.wait()
        os.close(self.stdin)


def reflink(src, dst):
    t = time.perf_counter()
    subprocess.run(["cp", "--reflink=always", str(src), str(dst)], check=True)
    return time.perf_counter() - t


def blockdiff(*args):
    t = time.perf_counter()
    subprocess.run(["blockdiff", *map(str, args)], check=True, capture_output=True)
    return time.perf_counter() - t


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(16 * 2**20):
            digest.update(chunk)
    return digest.hexdigest()


def disk_used_mib():
    out = subprocess.run(["df", "-B1", "--output=used", str(ROOT)], capture_output=True, text=True, check=True).stdout
    return int(out.split()[-1]) / 2**20


def run_once(forks):
    m = {}
    src = VM("source")
    m["Reflink copy of the 1 GiB base disk"] = ("s", reflink(BASE / "rootfs.ext4", src.dir / "rootfs.ext4"))
    src.api("PUT", "/boot-source", {"kernel_image_path": str(BASE / "vmlinux"), "boot_args": BOOT_ARGS})
    src.api("PUT", "/drives/rootfs", {"drive_id": "rootfs", "path_on_host": str(src.dir / "rootfs.ext4"),
                                       "is_root_device": True, "is_read_only": False})
    src.api("PUT", "/machine-config", {"vcpu_count": 1, "mem_size_mib": MEM_MIB})
    t = time.perf_counter()
    src.api("PUT", "/actions", {"action_type": "InstanceStart"})
    src.wait_for(r"root@[\w-]+:~# ")
    boot = time.perf_counter() - t
    t = time.perf_counter()
    src.send(SETUP + "\n")
    src.wait_for(r"\nSETUP-DONE", timeout=120)
    setup = time.perf_counter() - t
    src.wait_for(r"# ")
    m["Cold boot to a root shell"] = ("s", boot)
    m["Task setup (50 MB to disk, 200 MB to RAM, wait 5 s)"] = ("s", setup)
    m["Cold path total (boot + setup)"] = ("s", boot + setup)
    m["Memory of the set-up VM (RSS)"] = ("MiB", src.mem_mib()["Rss"])

    snap = SNAPS / "task"
    shutil.rmtree(snap, ignore_errors=True)
    snap.mkdir(parents=True)
    t = time.perf_counter()
    src.api("PATCH", "/vm", {"state": "Paused"})
    src.api("PUT", "/snapshot/create", {"snapshot_type": "Full", "snapshot_path": str(snap / "vmstate"),
                                         "mem_file_path": str(snap / "mem")})
    m[f"Pause + full snapshot ({MEM_MIB} MiB guest)"] = ("s", time.perf_counter() - t)
    src.kill()
    m["Disk checkpoint: blockdiff create"] = ("s", blockdiff("create", snap / "disk.bdiff", src.dir / "rootfs.ext4",
                                                             "--base", BASE / "rootfs.ext4"))
    m["Disk checkpoint size (the full disk is 1024 MiB)"] = ("MiB", (snap / "disk.bdiff").stat().st_size / 2**20)
    m["Stage on a host: blockdiff apply onto the base"] = ("s", blockdiff("apply", snap / "disk.bdiff", snap / "disk.ext4",
                                                                          "--base", BASE / "rootfs.ext4"))
    verified = sha256(snap / "disk.ext4") == sha256(src.dir / "rootfs.ext4")

    for old in VMS.glob("fork-*"):
        shutil.rmtree(old)
    used_before = disk_used_mib()
    resumed = []
    for i in range(1, forks + 1):
        vm = VM(f"fork-{i}")
        copy = reflink(snap / "disk.ext4", vm.dir / "rootfs.ext4")
        t = time.perf_counter()
        vm.api("PUT", "/snapshot/load", {
            "snapshot_path": str(snap / "vmstate"),
            "mem_backend": {"backend_type": "File", "backend_path": str(snap / "mem")},
            "resume_vm": False,
        })
        vm.api("PATCH", "/drives/rootfs", {"drive_id": "rootfs", "path_on_host": str(vm.dir / "rootfs.ext4")})
        vm.api("PATCH", "/vm", {"state": "Resumed"})
        load = time.perf_counter() - t
        vm.send(f"cat /root/setup-done >/dev/null && echo FORK-{i}-OK\n")
        vm.wait_for(rf"\nFORK-{i}-OK", timeout=30)
        resumed.append((vm, copy, load, time.perf_counter() - t))

    m["Fork: reflink disk copy"] = ("s", statistics.median(r[1] for r in resumed))
    m["Fork: snapshot load + resume"] = ("s", statistics.median(r[2] for r in resumed))
    m["Fork: resume to first command answered"] = ("s", statistics.median(r[3] for r in resumed))
    mem = [vm.mem_mib() for vm, *_ in resumed]
    m[f"Memory of {forks} forks (RSS, total)"] = ("MiB", sum(x["Rss"] for x in mem))
    m[f"Memory of {forks} forks (PSS, total)"] = ("MiB", sum(x["Pss"] for x in mem))
    m[f"Extra cache disk used by {forks} forks"] = ("MiB", disk_used_mib() - used_before)
    for vm, *_ in resumed:
        vm.kill()
    return m, verified


def fmt(unit, value):
    if unit == "MiB":
        if value < 0:
            return "≈ 0 MiB"
        if value < 1:
            return f"{value * 1024:.0f} KiB"
        return f"{value:.1f} MiB" if value < 10 else f"{value:.0f} MiB"
    return f"{value * 1000:.0f} ms" if value < 1 else f"{value:.1f} s"


def environment():
    fc = subprocess.run(["firecracker", "--version"], capture_output=True, text=True).stdout.splitlines()[0]
    return f"{fc} · host kernel {platform.release()} · {os.cpu_count()} host vCPUs · guests 1 vCPU / {MEM_MIB} MiB"


def main():
    parser = argparse.ArgumentParser(description="Benchmark Firecracker cold boot against snapshot forks.")
    parser.add_argument("--forks", type=int, default=5, help="Forks resumed from one snapshot per run.")
    parser.add_argument("--runs", type=int, default=3, help="Full runs to take the median over.")
    args = parser.parse_args()
    runs = []
    matches = 0
    for i in range(args.runs):
        metrics, verified = run_once(args.forks)
        runs.append(metrics)
        matches += verified
        print(f"run {i + 1}/{args.runs} done", flush=True)
    print(f"\n{environment()} · {args.runs} runs · median and range across runs")
    print(f"Staged disk matches the source disk byte for byte (SHA-256) in {matches}/{args.runs} runs\n")
    print("| Step | Median | Range |\n|---|---|---|")
    for name, (unit, _) in runs[0].items():
        values = [r[name][1] for r in runs]
        print(f"| {name} | {fmt(unit, statistics.median(values))} | {fmt(unit, min(values))} – {fmt(unit, max(values))} |")


if __name__ == "__main__":
    main()
