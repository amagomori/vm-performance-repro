import hashlib
import shutil
import statistics
from concurrent.futures import ThreadPoolExecutor

from benchmarks.firecracker import VM, Guest, Stopwatch, allocated_mib, environment, fmt, reflink, run, size_mib

HELP = "Measure a cold boot against forks of one snapshot, with diff memory layers and disk checkpoints."
SETUP = (
    "dd if=/dev/urandom of=/root/deps.bin bs=1M count=50 status=none && sync"
    " && dd if=/dev/urandom of=/dev/shm/state bs=1M count=200 status=none"
    " && sleep 5 && date +%s > /root/setup-done && echo SETUP-DONE"
)


def configure(parser):
    parser.add_argument("--forks", type=int, default=5, help="Forks resumed from one snapshot per run.")
    parser.add_argument("--runs", type=int, default=3, help="Full runs to take the median over.")


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def identical(a, b):
    with ThreadPoolExecutor(2) as pool:
        first, second = pool.map(sha256, (a, b))
    return first == second


def run_once(cache, guest, forks):
    m = {}
    snap = cache.root / "snapshots" / "task"
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
        src.send(SETUP + "\n")
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


def main(args, cache):
    guest = Guest()
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
