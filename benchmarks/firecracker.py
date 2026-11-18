import http.client
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Guest:
    vcpus: int = 1
    mem_mib: int = 512
    boot_args: str = "keep_bootcon console=ttyS0 reboot=k panic=1"
    prompt: str = r"root@[\w-]+:~# "


class Cache:
    def __init__(self, root):
        self.root = Path(root)
        self.kernel = self.root / "base" / "vmlinux"
        self.base_disk = self.root / "base" / "rootfs.ext4"
        self.vms = self.root / "vms"

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


def size_mib(path):
    return path.stat().st_size / 2**20


def allocated_mib(path):
    return path.stat().st_blocks * 512 / 2**20


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
