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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Guest:
    vcpus: int = 1
    mem_mib: int = 512
    boot_args: str = "keep_bootcon console=ttyS0 reboot=k panic=1"
    prompt: str = r"root@[\w-]+:~# "
    setup: str = (
        "dd if=/dev/urandom of=/root/deps.bin bs=1M count=50 status=none && sync"
        " && dd if=/dev/urandom of=/dev/shm/state bs=1M count=200 status=none"
        " && sleep 5 && date +%s > /root/setup-done && echo SETUP-DONE"
    )


class Cache:
    def __init__(self, root):
        self.root = Path(root)
        self.kernel = self.root / "base" / "vmlinux"
        self.base_disk = self.root / "base" / "rootfs.ext4"
        self.vms = self.root / "vms"
        self.task = self.root / "snapshots" / "task"

    def used_mib(self):
        return shutil.disk_usage(self.root).used / 2**20


class Stopwatch:
    def __enter__(self):
        self.start = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.seconds = time.perf_counter() - self.start


class VM:
    def __init__(self, path):
        self.dir = path
        shutil.rmtree(path, ignore_errors=True)
        path.mkdir(parents=True)
        self.disk = path / "rootfs.ext4"
        self.sock = path / "api.sock"
        self.log = path / "console.log"
        self.offset = 0
        os.mkfifo(path / "console.in")
        self.stdin = os.open(path / "console.in", os.O_RDWR)
        self.proc = subprocess.Popen(["firecracker", "--api-sock", self.sock], stdin=self.stdin,
                                     stdout=open(self.log, "wb"), stderr=subprocess.STDOUT)
        deadline = time.time() + 5
        while not self.sock.exists():
            if time.time() > deadline:
                raise RuntimeError(f"{path.name}: API socket never appeared")
            time.sleep(0.005)

    def api(self, method, path, body):
        conn = http.client.HTTPConnection("localhost")
        conn.sock = socket.socket(socket.AF_UNIX)
        conn.sock.settimeout(30)
        conn.sock.connect(str(self.sock))
        conn.request(method, path, json.dumps(body), {"Content-Type": "application/json"})
        resp = conn.getresponse()
        if resp.status >= 300:
            raise RuntimeError(f"{method} {path} -> {resp.status} {resp.read().decode()}")

    def configure(self, guest, kernel):
        self.api("PUT", "/boot-source", {"kernel_image_path": str(kernel), "boot_args": guest.boot_args})
        self.api("PUT", "/drives/rootfs", {"drive_id": "rootfs", "path_on_host": str(self.disk),
                                           "is_root_device": True, "is_read_only": False})
        self.api("PUT", "/machine-config", {"vcpu_count": guest.vcpus, "mem_size_mib": guest.mem_mib,
                                            "track_dirty_pages": True})

    def start(self, guest):
        self.api("PUT", "/actions", {"action_type": "InstanceStart"})
        self.wait_for(guest.prompt)

    def pause(self):
        self.api("PATCH", "/vm", {"state": "Paused"})

    def resume(self):
        self.api("PATCH", "/vm", {"state": "Resumed"})

    def snapshot(self, kind, vmstate, mem):
        self.api("PUT", "/snapshot/create", {"snapshot_type": kind, "snapshot_path": str(vmstate),
                                              "mem_file_path": str(mem)})

    def restore(self, vmstate, mem):
        self.api("PUT", "/snapshot/load", {"snapshot_path": str(vmstate), "resume_vm": False,
                                            "mem_backend": {"backend_type": "File", "backend_path": str(mem)}})
        self.api("PATCH", "/drives/rootfs", {"drive_id": "rootfs", "path_on_host": str(self.disk)})
        self.resume()

    def send(self, text):
        os.write(self.stdin, text.encode())

    def wait_for(self, pattern, timeout=60):
        deadline = time.time() + timeout
        rx = re.compile(pattern.encode())
        while time.time() < deadline:
            if m := rx.search(self.log.read_bytes(), self.offset):
                self.offset = m.end()
                return
            time.sleep(0.01)
        raise TimeoutError(f"{self.dir.name}: no {pattern!r} after {timeout}s")

    def mem_mib(self):
        return {k: int(v) / 1024 for k, v in re.findall(r"^(Rss|Pss):\s+(\d+) kB",
                                                        Path(f"/proc/{self.proc.pid}/smaps_rollup").read_text(), re.M)}

    def kill(self):
        self.proc.kill()
        self.proc.wait()
        os.close(self.stdin)


def run(*argv):
    with Stopwatch() as watch:
        subprocess.run(argv, check=True, stdout=subprocess.DEVNULL)
    return watch.seconds


def reflink(src, dst):
    return run("cp", "--reflink=always", src, dst)


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def identical(a, b):
    with ThreadPoolExecutor(2) as pool:
        first, second = pool.map(sha256, (a, b))
    return first == second


def size_mib(path):
    return path.stat().st_size / 2**20


def allocated_mib(path):
    return path.stat().st_blocks * 512 / 2**20


def run_once(cache, guest, forks):
    m = {}
    snap = cache.task
    shutil.rmtree(snap, ignore_errors=True)
    snap.mkdir(parents=True)
    src = VM(cache.vms / "source")
    m["Reflink copy of the 1 GiB base disk"] = ("s", reflink(cache.base_disk, src.disk))
    src.configure(guest, cache.kernel)
    with Stopwatch() as boot:
        src.start(guest)
    with Stopwatch() as boot_snap:
        src.pause()
        src.snapshot("Diff", snap / "boot.vmstate", snap / "boot.mem")
        src.resume()
    with Stopwatch() as setup:
        src.send(guest.setup + "\n")
        src.wait_for(r"\nSETUP-DONE", timeout=120)
    src.wait_for(r"# ")
    m["Cold boot to a root shell"] = ("s", boot.seconds)
    m["Task setup (50 MB to disk, 200 MB to RAM, wait 5 s)"] = ("s", setup.seconds)
    m["Cold path total (boot + setup)"] = ("s", boot.seconds + setup.seconds)
    m["Memory of the set-up VM (RSS)"] = ("MiB", src.mem_mib()["Rss"])
    m["Boot snapshot: pause + diff since boot + resume"] = ("s", boot_snap.seconds)

    with Stopwatch() as layer:
        src.pause()
        src.snapshot("Diff", snap / "vmstate", snap / "layer.mem")
    with Stopwatch() as full:
        src.snapshot("Full", snap / "full.vmstate", snap / "full.mem")
    src.kill()
    m["Setup snapshot: pause + diff since the boot snapshot"] = ("s", layer.seconds)
    m["For comparison: full snapshot of the same state"] = ("s", full.seconds)
    m["Memory file: full snapshot"] = ("MiB", allocated_mib(snap / "full.mem"))
    m["Memory file: boot snapshot (sparse)"] = ("MiB", allocated_mib(snap / "boot.mem"))
    m["Memory file: setup layer (sparse)"] = ("MiB", allocated_mib(snap / "layer.mem"))
    run("blockdiff", "create", snap / "layer.bdiff", snap / "layer.mem")
    m["Memory layer packed with blockdiff"] = ("MiB", size_mib(snap / "layer.bdiff"))

    m["Stage memory: unpack + rebase onto the boot snapshot"] = ("s", sum([
        run("blockdiff", "apply", snap / "layer.bdiff", snap / "layer.unpacked.mem"),
        reflink(snap / "boot.mem", snap / "mem"),
        run("snapshot-editor", "edit-memory", "rebase", "--memory-path", snap / "mem",
            "--diff-path", snap / "layer.unpacked.mem"),
    ]))
    mem_ok = identical(snap / "mem", snap / "full.mem")

    m["Disk checkpoint: blockdiff create"] = ("s", run("blockdiff", "create", snap / "disk.bdiff", src.disk,
                                                       "--base", cache.base_disk))
    m["Disk checkpoint size (the full disk is 1024 MiB)"] = ("MiB", size_mib(snap / "disk.bdiff"))
    m["Stage disk: blockdiff apply onto the base"] = ("s", run("blockdiff", "apply", snap / "disk.bdiff",
                                                               snap / "disk.ext4", "--base", cache.base_disk))
    disk_ok = identical(snap / "disk.ext4", src.disk)
    m[f"Upload per task: memory layer + disk diff (full: {guest.mem_mib + 1024} MiB)"] = (
        "MiB", size_mib(snap / "layer.bdiff") + size_mib(snap / "disk.bdiff"))

    for old in cache.vms.glob("fork-*"):
        shutil.rmtree(old)
    used_before = cache.used_mib()
    resumed = []
    for i in range(1, forks + 1):
        vm = VM(cache.vms / f"fork-{i}")
        copy = reflink(snap / "disk.ext4", vm.disk)
        with Stopwatch() as answer:
            with Stopwatch() as load:
                vm.restore(snap / "vmstate", snap / "mem")
            vm.send(f"cat /root/setup-done >/dev/null && echo FORK-{i}-OK\n")
            vm.wait_for(rf"\nFORK-{i}-OK", timeout=30)
        resumed.append((vm, copy, load.seconds, answer.seconds))

    vms, copies, loads, answers = zip(*resumed)
    m["Fork: reflink disk copy"] = ("s", statistics.median(copies))
    m["Fork: snapshot load + resume"] = ("s", statistics.median(loads))
    m["Fork: resume to first command answered"] = ("s", statistics.median(answers))
    mem = [vm.mem_mib() for vm in vms]
    m[f"Memory of {forks} forks (RSS, total)"] = ("MiB", sum(x["Rss"] for x in mem))
    m[f"Memory of {forks} forks (PSS, total)"] = ("MiB", sum(x["Pss"] for x in mem))
    m[f"Extra cache disk used by {forks} forks"] = ("MiB", cache.used_mib() - used_before)
    for vm in vms:
        vm.kill()
    return m, disk_ok, mem_ok


def fmt(unit, value):
    if unit == "MiB":
        if value < 0:
            return "≈ 0 MiB"
        if value < 1:
            return f"{value * 1024:.0f} KiB"
        return f"{value:.1f} MiB" if value < 10 else f"{value:.0f} MiB"
    return f"{value * 1000:.0f} ms" if value < 1 else f"{value:.1f} s"


def environment(guest):
    fc = subprocess.run(["firecracker", "--version"], capture_output=True, text=True).stdout.splitlines()[0]
    return (f"{fc} · {platform.machine()} host kernel {platform.release()} · {os.cpu_count()} host vCPUs"
            f" · guests {guest.vcpus} vCPU / {guest.mem_mib} MiB")


def main():
    parser = argparse.ArgumentParser(description="Benchmark Firecracker cold boot against snapshot forks.")
    parser.add_argument("--forks", type=int, default=5, help="Forks resumed from one snapshot per run.")
    parser.add_argument("--runs", type=int, default=3, help="Full runs to take the median over.")
    args = parser.parse_args()
    cache, guest = Cache(os.environ.get("FC_ROOT", "/srv/fc")), Guest()
    runs = []
    disk_matches = mem_matches = 0
    for i in range(args.runs):
        metrics, disk_ok, mem_ok = run_once(cache, guest, args.forks)
        runs.append(metrics)
        disk_matches += disk_ok
        mem_matches += mem_ok
        print(f"run {i + 1}/{args.runs} done", flush=True)
    print(f"\n{environment(guest)} · {args.runs} runs · median and range across runs")
    print(f"Staged disk matches the source disk byte for byte (SHA-256) in {disk_matches}/{args.runs} runs")
    print(f"Rebased memory matches a full snapshot byte for byte (SHA-256) in {mem_matches}/{args.runs} runs\n")
    print("| Step | Median | Range |\n|---|---|---|")
    for name, (unit, _) in runs[0].items():
        values = [r[name][1] for r in runs]
        print(f"| {name} | {fmt(unit, statistics.median(values))} | {fmt(unit, min(values))} – {fmt(unit, max(values))} |")


if __name__ == "__main__":
    main()
