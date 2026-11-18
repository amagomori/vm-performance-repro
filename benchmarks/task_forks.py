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

HELP = "Build a real task image into a VM disk, snapshot it warm, and start many forks at once."
TASKS_REPO = "https://github.com/harbor-framework/terminal-bench-2"
TASKS_REV = "2fd12b88aafdd04a52c298e3940bcb189f9766d6"
IMAGE = "vmrepro-task"
INIT = """#!/bin/sh
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev 2>/dev/null
mkdir -p /dev/pts /dev/shm
mount -t devpts devpts /dev/pts
mount -t tmpfs tmpfs /dev/shm
mount -t tmpfs tmpfs /run
mount -t tmpfs tmpfs /tmp
hostname task
export HOME=/root PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin PS1='root@task:~# '
cd /app 2>/dev/null
exec /bin/sh -i </dev/ttyS0 >/dev/ttyS0 2>&1
"""
WORKLOAD = ("python3 -c \"import numpy, pandas, scipy.stats, matplotlib; "
            "print(pandas.read_csv('climate_analyzer/sample_data/climate_data.csv').describe().shape)\"")


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


def build_disks(task, work):
    m = {}
    m["Build the task image from its Dockerfile"] = ("s", run("docker", "build", "-q", "-t", IMAGE,
                                                              task.folder / "environment"))
    base_disk, task_disk, mnt = work / "base.ext4", work / "task.ext4", work / "mnt"
    export(task.base_image, work / "base")
    (work / "base" / "vm-init").write_text(INIT)
    (work / "base" / "vm-init").chmod(0o755)
    base_disk.unlink(missing_ok=True)
    run("truncate", "-s", f"{task.storage_mib}M", base_disk)
    run("mkfs.ext4", "-q", "-F", "-d", work / "base", base_disk)
    m[f"Shared base disk from {task.base_image} (allocated)"] = ("MiB", allocated_mib(base_disk))

    export(IMAGE, work / "image")
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


def command(vm, i):
    with Stopwatch() as watch:
        vm.send(f"{WORKLOAD} && echo DONE-$((0+{i}))\n")
        vm.wait_for(rf"\nDONE-{i}\r?\n", timeout=600)
    vm.wait_for(r"# ")
    return watch.seconds


def cold_path(cache, task, disk, snap):
    m = {}
    src = VM(cache.vms / "task-source")
    reflink(disk, src.disk)
    src.configure(task.guest, cache.kernel)
    with Stopwatch() as boot:
        src.start(task.guest)
    m["Cold boot to a shell"] = ("s", boot.seconds)
    m["First command in the cold VM: imports + read the CSV"] = ("s", command(src, 1))
    m["Same command again in the cold VM, cache warm"] = ("s", command(src, 2))
    with Stopwatch() as snapshot:
        src.pause()
        src.snapshot("Diff", snap / "vmstate", snap / "mem")
    src.kill()
    reflink(src.disk, snap / "disk.ext4")
    m["Snapshot: pause + every page touched since boot"] = ("s", snapshot.seconds)
    m[f"Memory file (sparse, guest has {task.guest.mem_mib} MiB)"] = ("MiB", allocated_mib(snap / "mem"))
    return m


def fork(cache, snap, i):
    vm = VM(cache.vms / f"task-fork-{i}")
    reflink(snap / "disk.ext4", vm.disk)
    with Stopwatch() as restore:
        vm.restore(snap / "vmstate", snap / "mem")
    return vm, restore.seconds, command(vm, i)


def mem_available_mib():
    return int(re.search(r"^MemAvailable:\s+(\d+) kB", Path("/proc/meminfo").read_text(), re.M).group(1)) / 1024


def fork_level(cache, snap, n):
    for old in cache.vms.glob("task-fork-*"):
        shutil.rmtree(old)
    before = mem_available_mib()
    with Stopwatch() as wall:
        with ThreadPoolExecutor(n) as pool:
            vms, restores, commands = zip(*pool.map(functools.partial(fork, cache, snap), range(1, n + 1)))
    used = before - mem_available_mib()
    pss = sum(vm.mem_mib()["Pss"] for vm in vms)
    for vm in vms:
        vm.kill()
    return {
        "Start all forks and answer one command each": ("s", wall.seconds),
        "Restore, median": ("s", statistics.median(restores)),
        "Restore, slowest": ("s", max(restores)),
        "First command, median": ("s", statistics.median(commands)),
        "First command, slowest": ("s", max(commands)),
        "Host memory per fork (PSS)": ("MiB", pss / n),
        "Host memory per fork (drop in MemAvailable)": ("MiB", used / n),
    }


def configure(parser):
    parser.add_argument("--task", default="modernize-scientific-stack", help="Terminal-Bench 2.0 task folder name.")
    parser.add_argument("--forks", default="1,8,16", help="Comma-separated numbers of forks to start at once.")


def main(args, cache):
    work = cache.root / "task-bench"
    snap = work / "snapshot"
    shutil.rmtree(snap, ignore_errors=True)
    snap.mkdir(parents=True)
    task = Task.fetch(args.task, work / "tasks")
    rows, disk = build_disks(task, work)
    rows |= cold_path(cache, task, disk, snap)
    levels = [int(n) for n in args.forks.split(",")]
    results = {n: fork_level(cache, snap, n) for n in levels}

    print(f"\n{environment(task.guest)} · task {task.name}@{TASKS_REV[:7]}\n")
    print("| Step | Result |\n|---|---|")
    for name, (unit, value) in rows.items():
        print(f"| {name} | {fmt(unit, value)} |")
    print("\n| Forks started at once | " + " | ".join(str(n) for n in levels) + " |")
    print("|---|" + "---|" * len(levels))
    for name, (unit, _) in results[levels[0]].items():
        print(f"| {name} | " + " | ".join(fmt(unit, results[n][name][1]) for n in levels) + " |")
