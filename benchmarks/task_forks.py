import functools
import re
import shutil
import statistics
import subprocess
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from benchmarks.firecracker import VM, Guest, Stopwatch, allocated_mib, environment, fmt, reflink, run, size_mib
from benchmarks.uffd import Prefetcher, Source

HELP = "Build a real task image into a VM disk, snapshot it warm, and start many forks at once."
TASKS_REPO = "https://github.com/harbor-framework/terminal-bench-2"
TASKS_REV = "2fd12b88aafdd04a52c298e3940bcb189f9766d6"
IMAGE = "vmrepro-task"
SERVICE_IMAGE = "vmrepro-service"
INIT = """#!/bin/sh
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev 2>/dev/null
mkdir -p /dev/pts /dev/shm
mount -t devpts devpts /dev/pts
mount -t tmpfs tmpfs /dev/shm
mount -t tmpfs tmpfs /run
mount -t tmpfs tmpfs /tmp
echo 1 > /proc/sys/kernel/printk
hostname task
ip link set lo up 2>/dev/null
export HOME=/root PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin PS1='root@task:~# '
cd /app 2>/dev/null
exec /bin/sh -i </dev/ttyS0 >/dev/ttyS0 2>&1
"""
SERVICE_DOCKERFILE = f"""FROM {IMAGE}
RUN pip install --no-cache-dir fastapi==0.162.0 pydantic==2.14.1 uvicorn==0.54.0 \\
    && apt-get update && apt-get install -y --no-install-recommends iproute2 && rm -rf /var/lib/apt/lists/*
COPY service.py /app/service.py
"""
SERVICE_APP = """import json

import matplotlib
import numpy
import pandas
import scipy.stats
from fastapi import FastAPI

data = pandas.read_csv("climate_analyzer/sample_data/climate_data.csv")
app = FastAPI()


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/stats")
def stats():
    return json.loads(data.describe().to_json())
"""


@dataclass(frozen=True)
class Workload:
    noun: str
    detail: str
    setup: str | None
    text: str


COMMAND = Workload("command", "imports + read the CSV", None,
                   "python3 -c \"import numpy, pandas, scipy.stats, matplotlib; "
                   "print(pandas.read_csv('climate_analyzer/sample_data/climate_data.csv').describe().shape)\"")
REQUEST = Workload("request", "GET /stats from the running FastAPI app",
                   "uvicorn service:app --port 8000 >/tmp/app.log 2>&1 & "
                   "until curl -sf localhost:8000/health >/dev/null; do sleep 0.05; done",
                   "curl -sf localhost:8000/stats >/dev/null")


@dataclass(frozen=True)
class Task:
    name: str
    folder: Path
    base_image: str
    guest: Guest
    storage_mib: int

    @classmethod
    def fetch(cls, name, repo):
        if not (repo / name / "task.toml").exists():
            shutil.rmtree(repo, ignore_errors=True)
            run("git", "clone", "--quiet", "--filter=blob:none", "--no-checkout", TASKS_REPO, repo)
            run("git", "-C", repo, "sparse-checkout", "set", name)
            run("git", "-C", repo, "checkout", "--quiet", TASKS_REV)
        env = tomllib.loads((repo / name / "task.toml").read_text())["environment"]
        dockerfile = (repo / name / "environment" / "Dockerfile").read_text()
        guest = Guest(vcpus=env["cpus"], mem_mib=env["memory_mb"], boot_args=f"{Guest.boot_args} init=/vm-init")
        return cls(name, repo / name, re.search(r"^FROM\s+(\S+)", dockerfile, re.M).group(1), guest,
                   env["storage_mb"])


def output(*argv):
    return subprocess.run(argv, check=True, capture_output=True, text=True).stdout.strip()


def export(image, dest):
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    container = output("docker", "create", image)
    try:
        subprocess.run(f"docker export {container} | tar -x -C {dest}", shell=True, check=True)
    finally:
        run("docker", "rm", container)


def build_disks(task, work, workload):
    m = {}
    m["Build the task image from its Dockerfile"] = ("s", run("docker", "build", "-q", "-t", IMAGE,
                                                              task.folder / "environment"))
    image = IMAGE
    if workload.setup:
        context = work / "service"
        context.mkdir(exist_ok=True)
        (context / "Dockerfile").write_text(SERVICE_DOCKERFILE)
        (context / "service.py").write_text(SERVICE_APP)
        m["Add FastAPI, uvicorn and the app on top"] = ("s", run("docker", "build", "-q", "-t", SERVICE_IMAGE,
                                                                 context))
        image = SERVICE_IMAGE
    base_disk, task_disk, mnt = work / "base.ext4", work / "task.ext4", work / "mnt"
    export(task.base_image, work / "base")
    (work / "base" / "vm-init").write_text(INIT)
    (work / "base" / "vm-init").chmod(0o755)
    base_disk.unlink(missing_ok=True)
    run("truncate", "-s", f"{task.storage_mib}M", base_disk)
    run("mkfs.ext4", "-q", "-F", "-d", work / "base", base_disk)
    m[f"Shared base disk from {task.base_image} (allocated)"] = ("MiB", allocated_mib(base_disk))

    export(image, work / "image")
    task_disk.unlink(missing_ok=True)
    reflink(base_disk, task_disk)
    mnt.mkdir(exist_ok=True)
    run("mount", "-o", "loop", task_disk, mnt)
    try:
        run("rsync", "-aHAX", "--numeric-ids", "--delete", "--exclude", "/vm-init", f"{work / 'image'}/", mnt)
    finally:
        run("umount", mnt)
    run("blockdiff", "create", work / "task.bdiff", task_disk, "--base", base_disk)
    m["Task layer over the shared base (blockdiff)"] = ("MiB", size_mib(work / "task.bdiff"))
    shutil.rmtree(work / "base")
    shutil.rmtree(work / "image")
    return m, task_disk


def command(vm, text, i, timeout=600):
    with Stopwatch() as watch:
        vm.send(f"{text} && echo DONE-$((0+{i}))\n")
        vm.wait_for(rf"\nDONE-{i}\r?\n", timeout=timeout)
    vm.wait_for(r"# ")
    return watch.seconds


def cold_path(cache, task, disk, snap, workload):
    m = {}
    src = VM(cache.vms / "task-source")
    reflink(disk, src.disk)
    src.configure(task.guest, cache.kernel)
    with Stopwatch() as boot:
        src.start(task.guest)
    m["Cold boot to a shell"] = ("s", boot.seconds)
    if workload.setup:
        m["Start the app and wait until /health answers"] = ("s", command(src, workload.setup, 1))
    m[f"First {workload.noun} in the cold VM: {workload.detail}"] = ("s", command(src, workload.text, 2))
    m[f"Same {workload.noun} again in the cold VM, warm"] = ("s", command(src, workload.text, 3))
    with Stopwatch() as snapshot:
        src.pause()
        src.snapshot("Diff", snap / "vmstate", snap / "mem")
    src.kill()
    reflink(src.disk, snap / "disk.ext4")
    m["Snapshot: pause + every page touched since boot"] = ("s", snapshot.seconds)
    m[f"Memory file (sparse, guest has {task.guest.mem_mib} MiB)"] = ("MiB", allocated_mib(snap / "mem"))
    return m


@dataclass(frozen=True)
class Memory:
    backend: str
    huge_pages: str | None


MEMORY = {
    "file": Memory("File", None),
    "file-thp": Memory("File", "Transparent"),
    "uffd": Memory("Uffd", None),
    "uffd-2m": Memory("Uffd", "2M"),
}
HUGE_PAGE = 2 * 2**20


def fork(cache, snap, workload, memory, source, vms, i):
    vm = VM(cache.vms / f"task-fork-{i}")
    vms.append(vm)
    reflink(snap / "disk.ext4", vm.disk)
    faults = vm.faults()
    prefetcher = Prefetcher(vm.dir / "uffd.sock", source) if source else None
    with Stopwatch() as restore:
        if prefetcher:
            prefetcher.start()
        vm.load(snap / "vmstate", memory.backend, vm.dir / "uffd.sock" if prefetcher else snap / "mem",
                memory.huge_pages)
        if prefetcher:
            prefetcher.join()
            if prefetcher.error:
                raise prefetcher.error
        vm.resume()
    answer = command(vm, workload.text, i, timeout=120)
    return vm, restore.seconds, answer, vm.faults() - faults, vm.kvm_exits(), prefetcher.copied if prefetcher else 0


def meminfo(key):
    return int(re.search(rf"^{key}:\s+(\d+)", Path("/proc/meminfo").read_text(), re.M).group(1))


def set_huge_pages(count):
    Path("/proc/sys/vm/compact_memory").write_text("1")
    Path("/proc/sys/vm/nr_hugepages").write_text(str(count))
    return meminfo("HugePages_Total")


def memory_used_mib():
    return (meminfo("MemTotal") - meminfo("MemAvailable")) / 1024 + (
        meminfo("HugePages_Total") - meminfo("HugePages_Free")) * HUGE_PAGE / 2**20


def fork_level(cache, snap, workload, mode, n):
    memory = MEMORY[mode]
    for old in cache.vms.glob("task-fork-*"):
        shutil.rmtree(old)
    source = Source(snap / "mem") if memory.backend == "Uffd" else None
    if memory.huge_pages == "2M":
        need = n * (sum(b - a for a, b in source.extents_for(HUGE_PAGE)) // HUGE_PAGE + 64)
        if set_huge_pages(need) < need:
            print(f"{mode}, {n} forks: skipped, the host can't reserve {need} huge pages", flush=True)
            set_huge_pages(0)
            source.close()
            return None
    vms = []
    try:
        before = memory_used_mib()
        with Stopwatch() as wall:
            with ThreadPoolExecutor(n) as pool:
                results = list(pool.map(functools.partial(fork, cache, snap, workload, memory, source, vms),
                                        range(1, n + 1)))
        used = memory_used_mib() - before
    except Exception as e:
        report(mode, n, e, vms)
        return {}
    finally:
        for vm in vms:
            vm.kill()
        if source:
            source.close()
        if memory.huge_pages == "2M":
            set_huge_pages(0)
    print(f"{mode}, {n} forks: done in {wall.seconds:.1f} s", flush=True)
    _, restores, answers, faults, exits, copied = zip(*results)
    return {
        f"All forks answered one {workload.noun}": ("s", wall.seconds),
        "Restore, median": ("s", statistics.median(restores)),
        f"First {workload.noun}, median": ("s", statistics.median(answers)),
        f"Restore + first {workload.noun}, median": ("s", statistics.median(r + a for r, a in zip(restores, answers))),
        "Host page faults per fork, median": ("count", statistics.median(faults)),
        "KVM exits per fork, median": ("count", statistics.median(exits) if None not in exits else None),
        "Copied in before resume, per fork": ("MiB", statistics.median(copied) / 2**20),
        "Host memory per fork": ("MiB", used / n),
    }


def report(mode, n, error, vms):
    print(f"{mode}, {n} forks: failed: {error}", flush=True)
    for vm in vms:
        if vm.proc.poll() is not None or vm.dir.name in str(error):
            print(f"  {vm.dir.name}: firecracker exit code {vm.proc.poll()}, console ends:")
            for line in vm.log.read_text(errors="replace").splitlines()[-8:]:
                print(f"    {line}")
    print("  kernel log ends:")
    for line in subprocess.run(["dmesg"], capture_output=True, text=True).stdout.splitlines()[-8:]:
        print(f"    {line}")


def cell(result, name, unit):
    if result is None:
        return "n/a"
    return fmt(unit, result[name][1]) if result else "failed"


def table(title, columns, results):
    rows = next((r for r in results if r), None)
    if rows:
        print(f"\n| {title} | " + " | ".join(columns) + " |")
        print("|---|" + "---|" * len(columns))
        for name, (unit, _) in rows.items():
            print(f"| {name} | " + " | ".join(cell(r, name, unit) for r in results) + " |")


def configure(parser):
    parser.add_argument("--task", default="modernize-scientific-stack", help="Terminal-Bench 2.0 task folder name.")
    parser.add_argument("--forks", default="1,8,16", help="Comma-separated numbers of forks to start at once.")
    parser.add_argument("--service", action="store_true",
                        help="Snapshot a running FastAPI app, and send it one request from each fork.")
    parser.add_argument("--memory", default="file",
                        help=f"Comma-separated ways to restore fork memory: {', '.join(MEMORY)}. Default: file.")


def main(args, cache):
    work = cache.root / "task-bench"
    snap = work / "snapshot"
    shutil.rmtree(snap, ignore_errors=True)
    snap.mkdir(parents=True)
    task = Task.fetch(args.task, work / "tasks")
    workload = REQUEST if args.service else COMMAND
    rows, disk = build_disks(task, work, workload)
    rows |= cold_path(cache, task, disk, snap, workload)
    levels = [int(n) for n in args.forks.split(",")]
    modes = args.memory.split(",")
    results = {(mode, n): fork_level(cache, snap, workload, mode, n) for mode in modes for n in levels}

    print(f"\n{environment(task.guest)} · task {task.name}@{TASKS_REV[:7]} · each fork sends one {workload.noun}\n")
    print("| Step | Result |\n|---|---|")
    for name, (unit, value) in rows.items():
        print(f"| {name} | {fmt(unit, value)} |")
    if len(modes) == 1:
        table("Forks started at once", [str(n) for n in levels], [results[modes[0], n] for n in levels])
    for n in levels if len(modes) > 1 else []:
        table(f"{n} forks at once", modes, [results[mode, n] for mode in modes])
