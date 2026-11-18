# vm-performance-repro

Measures how much faster a Firecracker microVM starts from a memory snapshot than from a cold boot, and how much memory and disk the forks of one snapshot share. Disks are copied with XFS reflinks, the method Cognition describes for Devin's VMs, and AWS is emulated locally with [Floci](https://github.com/floci-io/floci).

![Architecture: bench.py drives Firecracker VMs inside a Lima VM on the Mac, using an XFS reflink cache for disks and snapshots, while Terraform and boto3 checks run against Floci in Docker](docs/architecture.png)

## What the benchmarks do

`bench.py` runs one benchmark per call: `python3 bench.py snapshots` or `python3 bench.py task-forks`. Add `--help` after either name for its options.

### `snapshots`

1. **Cold path:** boots a VM from a reflink copy of a base disk, with dirty page tracking on, then runs a task setup that writes 50 MB to disk, fills 200 MB of guest RAM and waits 5 s.
2. **Boot snapshot:** right after boot, a diff snapshot saves only the memory pages touched so far. It's the shared base for every task.
3. **Setup snapshot:** after setup, a second diff snapshot saves only the pages the setup changed. A full snapshot of the same state is also taken, for comparison.
4. **Disk checkpoint:** `blockdiff create` stores only the disk blocks that differ from the base disk. `blockdiff` also packs the sparse memory layer for upload. These files are what a fleet would keep in S3.
5. **Stage:** as a host would once per task, the memory layer is unpacked and merged onto the boot snapshot with `snapshot-editor`, and `blockdiff apply` rebuilds the disk. SHA-256 checks confirm the memory matches the full snapshot and the disk matches the source disk, byte for byte.
6. **Forks:** resumes several VMs from the rebuilt snapshot, each on its own reflink copy of the staged disk with memory mapped copy-on-write. Each fork must answer a command and find the setup's marker file.

It repeats these steps and reports the median and range across runs.

### `task-forks`

1. **Real task:** fetches the [Terminal-Bench 2.0](https://github.com/harbor-framework/terminal-bench-2) task `modernize-scientific-stack` at a pinned commit and builds its image from its Dockerfile. The image has Python 3.13, gcc, gfortran, numpy, pandas, scipy and matplotlib. The guest gets the task's own 1 vCPU and 2 GiB.
2. **Layered disk:** turns the image's base, `python:3.13-slim-bookworm`, into a shared base disk. The task image is copied onto a reflink copy of it, and `blockdiff` measures the task's own layer.
3. **Cold path:** boots the task disk and runs a typical first command: import numpy, pandas, scipy and matplotlib, then read the task's CSV. It runs the command twice to show the warm-cache time too.
4. **Snapshot:** pauses the warm VM and saves every memory page touched since boot.
5. **Concurrent forks:** starts 1, 8 and 16 forks at once by default, each on its own reflink disk, and runs the same command in each. It reports wall time, the median and slowest restore and command, and host memory per fork.

With `--service`, the snapshot holds a running app instead:

- **Image:** FastAPI and uvicorn are layered on the task image, with a small app that imports the same libraries and loads the task's CSV at startup.
- **Snapshot:** the cold VM starts the app and waits until `/health` answers, so the app is already listening in every fork.
- **Forks:** each fork sends one `GET /stats` to that app from inside the guest, so no fork needs a network.

With `--memory`, forks restore their memory in up to four ways, compared side by side:

| Mode | How a fork gets its memory |
|---|---|
| `file` | Firecracker maps the snapshot file, shared by every fork. Each page loads the first time the guest touches it. This is the default. |
| `file-thp` | The same, with transparent huge pages requested |
| `uffd` | A page-fault handler in `benchmarks/uffd.py` copies every page the snapshot holds into the fork before it resumes, then hands the rest back to the kernel. Memory is private to each fork. |
| `uffd-2m` | The same, into 2 MiB huge pages, so the VM's page tables need far fewer entries |

Each mode also reports host page faults and KVM exits per fork, read from `/proc` and KVM's debugfs.

## Requirements

- An Apple silicon Mac with an M3 or newer chip on macOS 15 or later, for nested virtualization. A Linux host with `/dev/kvm` also works.
- [Lima](https://lima-vm.io), [Docker](https://www.docker.com), [uv](https://docs.astral.sh/uv/) and [Terraform](https://developer.hashicorp.com/terraform/install) 1.6 or later.
- The Lima config mounts `~/Documents/vm-performance-repro`. Clone the repo there, or change `location` in `lima/fc-host.yaml`.
- An AWS account, only for running on EC2.

## Run the benchmark

Run every command from the repo root.

1. Create and start the Linux VM that hosts Firecracker:

   ```bash
   limactl create --name=fc-host lima/fc-host.yaml && limactl start fc-host
   ```

2. Install Firecracker, `snapshot-editor`, `blockdiff`, the XFS reflink cache, and the base kernel and disk:

   ```bash
   limactl shell fc-host -- sudo ./host-setup.sh
   ```

3. Run a benchmark. `task-forks` needs root, to run Docker and mount the task disk, and runs one fork here because forks are slow under the Mac's nested virtualization:

   ```bash
   limactl shell fc-host -- python3 bench.py snapshots --forks 5 --runs 3
   ```

   ```bash
   limactl shell fc-host -- sudo python3 bench.py task-forks --forks 1
   ```

4. Stop the VM when you're done:

   ```bash
   limactl stop fc-host
   ```

## Run it on EC2

`--ec2` runs either benchmark on a fresh EC2 host, so you can measure the architecture your machine doesn't have. It prints the results table and terminates the host.

| `--ec2` | Host | Why |
|---|---|---|
| `x86_64` | `c8i.xlarge` | Intel with nested virtualization, and 4 vCPUs and 8 GiB like the Lima VM |
| `arm64` | `c8g.metal-24xl` | Graviton has no nested virtualization, so Firecracker needs bare metal |

Run it from a shell without `.env` loaded, signed in to the AWS account you want to use:

```bash
AWS_PROFILE=your-profile uv run python bench.py --ec2 x86_64 snapshots
```

- **What it needs:** a default VPC with internet access, and permission to use EC2, S3 and SSM parameters.
- **Other hosts:** `--ec2` also takes an instance type: any `.metal` type, or any type with nested virtualization.
- **Benchmark options:** options after the benchmark name, such as `task-forks --forks 1,16,64`, run on the host as given.
- **Temporary resources only:** each run makes one host, its disk and one S3 bucket, and nothing else. Files move through the bucket with presigned URLs, so the host needs no IAM role or open port.
- **Normal cleanup:** when the script ends, including on Ctrl-C or `kill`, it terminates the host and deletes the bucket. The disk is deleted with the host.
- **If the script dies:** the host shuts itself down and terminates after `--timeout` minutes (30 by default). The host and bucket also carry an expiry, and every run first removes any expired ones an earlier run left behind.

## Results: `snapshots`

Firecracker v1.18.0, `blockdiff` at commit `8d10a60`, and the pinned guest kernel 6.18.62 with an Ubuntu 24.04 disk. Guests have 1 vCPU and 512 MiB, and each number is the median of 3 runs of 5 forks.

| Host | Arch | Virtualization | Host size |
|---|---|---|---|
| Mac: Lima VM on an M4 with 16 GB | arm64 | Nested | 4 vCPUs, 8 GiB |
| EC2 `c8i.xlarge` | x86_64 | Nested | 4 vCPUs, 8 GiB |
| EC2 `c8g.metal-24xl` | arm64 | Bare metal | 96 vCPUs, 192 GiB |

On every host, the rebased memory matched a full snapshot and the staged disk matched the source disk, byte for byte in 3 of 3 runs.

| Step | Mac | `c8i.xlarge` | `c8g.metal-24xl` |
|---|---|---|---|
| Reflink copy of the 1 GiB base disk | 1 ms | 2 ms | 1 ms |
| Cold boot to a root shell | 9.5 s | 2.2 s | 675 ms |
| Task setup (50 MB to disk, 200 MB to RAM, wait 5 s) | 10.2 s | 6.3 s | 5.6 s |
| **Cold path total (boot + setup)** | **19.9 s** | **8.5 s** | **6.3 s** |
| Memory of the set-up VM (RSS) | 357 MiB | 366 MiB | 361 MiB |
| Boot snapshot: pause + diff since boot + resume | 261 ms | 349 ms | 224 ms |
| Setup snapshot: pause + diff since the boot snapshot | 935 ms | 1.6 s | 1.6 s |
| For comparison: full snapshot of the same state | 1.4 s | 4.1 s | 4.1 s |
| Memory file: full snapshot | 512 MiB | 512 MiB | 512 MiB |
| Memory file: boot snapshot (sparse) | 109 MiB | 117 MiB | 111 MiB |
| Memory file: setup layer (sparse) | 265 MiB | 266 MiB | 266 MiB |
| Memory layer packed with `blockdiff` | 265 MiB | 266 MiB | 266 MiB |
| Stage memory: unpack + rebase onto the boot snapshot | 351 ms | 141 ms | 63 ms |
| Disk checkpoint: `blockdiff create` | 5 ms | 9 ms | 6 ms |
| Disk checkpoint size (the full disk is 1024 MiB) | 51 MiB | 51 MiB | 51 MiB |
| Stage disk: `blockdiff apply` onto the base | 1 ms | 2 ms | 2 ms |
| **Upload per task: memory layer + disk diff (full: 1536 MiB)** | **316 MiB** | **317 MiB** | **318 MiB** |
| Fork: reflink disk copy | 2 ms | 2 ms | 2 ms |
| Fork: snapshot load + resume | 3 ms | 4 ms | 4 ms |
| **Fork: resume to first command answered** | **393 ms** | **65 ms** | **14 ms** |
| Memory of 5 forks (RSS, total) | 175 MiB | 221 MiB | 200 MiB |
| **Memory of 5 forks (PSS, total)** | **47 MiB** | **58 MiB** | **52 MiB** |
| Extra cache disk used by 5 forks | ≈ 0 MiB | ≈ 0 MiB | ≈ 0 MiB |

- **Start time:** a fork answers in 14 ms on bare metal, 65 ms on nested x86_64 and 393 ms on the Mac. Boot plus setup takes 6.3 s, 8.5 s and 19.9 s.
- **Nested virtualization:** cold boot takes 675 ms on bare metal, 2.2 s nested on EC2 and 9.5 s nested on the Mac. The hosts also differ in CPU, so this doesn't isolate the cost of nesting.
- **Snapshot writes on EC2:** a full snapshot takes 4.1 s on both EC2 hosts, against 1.4 s on the Mac. The cache sits on a gp3 volume, and its default 125 MiB/s caps a 512 MiB write at about 4 s. Local NVMe or more provisioned throughput would cut it.
- **Upload per task:** about 317 MiB (a 266 MiB memory layer plus a 51 MiB disk diff), against 1536 MiB for a full memory snapshot plus a full disk, about 21%. The boot snapshot is stored once and shared by every task.
- **Memory layer size:** this setup fills 200 MB of RAM on purpose, so its layer is about half the guest's memory. A setup that mostly installs packages would dirty far fewer pages.
- **Fork memory:** five forks hold 47 to 58 MiB of their own memory in total (PSS), against about 360 MiB for one cold VM after setup. Pages the guest never touched are holes in the rebuilt memory file.
- **Cost:** dirty page tracking slowed the Mac's setup to 10.2 s, from 8.2 s in earlier runs without it, because KVM logs every guest memory write.
- **Disk speed:** `blockdiff create` and `apply` take milliseconds because, on the same XFS filesystem, they clone blocks instead of copying them. On a real host, the cost of staging is downloading the diffs.

## Results: `task-forks`

Task `modernize-scientific-stack` at commit `2fd12b8`, in guests with the task's own 1 vCPU and 2 GiB. One run per host, with the same Firecracker, kernel and `blockdiff` as above.

| Step | `c8i.4xlarge`: x86_64, nested, 16 vCPUs | `c8g.metal-24xl`: arm64, bare metal, 96 vCPUs |
|---|---|---|
| Build the task image from its Dockerfile | 70.6 s | 65.9 s |
| Shared base disk from `python:3.13-slim-bookworm` | 197 MiB | 193 MiB |
| Task layer over the shared base | 904 MiB | 858 MiB |
| Cold boot to a shell | 1.3 s | 171 ms |
| First command in the cold VM: imports + read the CSV | 1.8 s | 865 ms |
| Same command again in the cold VM, cache warm | 659 ms | 573 ms |
| Snapshot of the warm VM | 1.3 s | 1.3 s |
| Memory file (sparse, of 2048 MiB) | 277 MiB | 277 MiB |

| Forks started at once | 1 | 16 | 64 |
|---|---|---|---|
| **`c8i.4xlarge`: all forks answered** | **1.7 s** | **3.2 s** | **15.6 s** |
| Restore, median / slowest | 6 ms / 6 ms | 6 ms / 14 ms | 27 ms / 65 ms |
| First command, median / slowest | 1.7 s / 1.7 s | 3.1 s / 3.1 s | 14.0 s / 15.1 s |
| Host memory per fork | 98 MiB | 94 MiB | 83 MiB |
| **`c8g.metal-24xl`: all forks answered** | **729 ms** | **921 ms** | **2.0 s** |
| Restore, median / slowest | 4 ms / 4 ms | 3 ms / 4 ms | 4 ms / 6 ms |
| First command, median / slowest | 714 ms / 714 ms | 845 ms / 856 ms | 1.6 s / 1.6 s |
| Host memory per fork | 83 MiB | 90 MiB | 80 MiB |

- **Snapshot size:** a warm snapshot of a real task is 277 MiB of its 2 GiB guest. Pages the guest never touched stay holes, as in `snapshots`.
- **Concurrency on bare metal:** 64 forks restore in 4 to 6 ms each and all answer within 2.0 s. One fork alone answers in 714 ms.
- **Nesting costs on first touch:** each memory page a fork reads for the first time is a page fault. Under nesting, the outer hypervisor handles that fault too, so a single nested fork takes 1.7 s against 659 ms warm in the cold VM. 64 nested forks on 16 vCPUs take 15.6 s.
- **Memory per fork:** each fork adds 80 to 98 MiB of host memory, measured as the drop in `MemAvailable`. That is mostly the new Python process's own memory. The snapshot itself is shared, so CPU runs out before memory does.
- **Disk:** the task's own layer is about 880 MiB on a shared base of about 195 MiB, and forks add almost no disk.
- **What the snapshot saves:** each command here starts a new Python process, so a fork skips the boot and gets a warm page cache, but repeats the imports. Snapshots save more when the setup leaves something running, as the `--service` results below show.
- **On the Mac:** use EC2 for `task-forks`. Under Lima, one fork's first command took 14.8 s against 2.2 s warm, and 8 forks at once didn't finish within 10 minutes.

### With `--service`: forks of a running FastAPI app

Same task, hosts and settings. The snapshot is taken with the app already started, and each fork sends it one request.

| Step | `c8i.4xlarge`: x86_64, nested | `c8g.metal-24xl`: arm64, bare metal |
|---|---|---|
| Cold boot to a shell | 1.3 s | 169 ms |
| Start the app and wait until `/health` answers | 2.4 s | 1.2 s |
| First request in the cold VM | 20 ms | 10 ms |
| Snapshot of the VM with the app running | 1.7 s | 1.7 s |
| Memory file (sparse, of 2048 MiB) | 318 MiB | 318 MiB |

| Forks started at once | 1 | 16 | 64 |
|---|---|---|---|
| **`c8i.4xlarge`: all forks answered** | **239 ms** | **902 ms** | **3.8 s** |
| Restore, median / slowest | 3 ms / 3 ms | 6 ms / 12 ms | 26 ms / 48 ms |
| First request, median / slowest | 222 ms / 222 ms | 740 ms / 769 ms | 2.0 s / 2.9 s |
| Host memory per fork | 7.4 MiB | 12 MiB | 13 MiB |
| **`c8g.metal-24xl`: all forks answered** | **46 ms** | **137 ms** | **446 ms** |
| Restore, median / slowest | 4 ms / 4 ms | 4 ms / 5 ms | 4 ms / 6 ms |
| First request, median / slowest | 30 ms / 30 ms | 40 ms / 40 ms | 40 ms / 41 ms |
| Host memory per fork | 4.6 MiB | 12 MiB | 10 MiB |

- **The app is already running:** a fork answers in 30 ms on bare metal against 1.4 s to boot, start the app and answer cold. Nothing starts in the fork: uvicorn resumes where it was paused, with its imports and data in memory.
- **64 copies of a running app:** on bare metal all 64 answer within 446 ms. On 16 nested vCPUs they take 3.8 s, because each fork still pays the nested cost the first time it touches each page.
- **Memory per fork drops to about 12 MiB:** forks share the app's whole memory and only copy the pages they change. With a new Python process per command, each fork cost 80 to 98 MiB.
- **What a real service would still need:** a fork keeps the app's open connections, secrets and random state from snapshot time. See the limitation on per-fork identity below.

### With `--memory`: where a fork's time goes

Same task and hosts, with `--service`. The median fork's restore plus first request:

| Forks at once | Host | `file` | `file-thp` | `uffd` | `uffd-2m` |
|---|---|---|---|---|---|
| 1 | `c8i.4xlarge`, nested | 226 ms | 226 ms | 180 ms | **98 ms** |
| 16 | `c8i.4xlarge`, nested | 742 ms | 737 ms | 254 ms | **99 ms** |
| 64 | `c8i.4xlarge`, nested | 2.0 s | 2.0 s | **562 ms** | 1.6 s |
| 1 | `c8g.metal-24xl` | **34 ms** | 34 ms | 81 ms | 38 ms |
| 16 | `c8g.metal-24xl` | 39 ms | **34 ms** | 82 ms | 40 ms |
| 64 | `c8g.metal-24xl` | **34 ms** | 34 ms | 135 ms | 41 ms |

Per fork, with 16 forks at once, on the nested host and then on bare metal:

| | `file` | `file-thp` | `uffd` | `uffd-2m` |
|---|---|---|---|---|
| Host page faults | 3,350 / 3,158 | 3,351 / 3,158 | 131 / 101 | 129 / 98 |
| KVM exits | 12,828 / 11,504 | 12,800 / 11,504 | 8,242 / 7,872 | 505 / 408 |
| Copied in before resume | 0 | 0 | 319 MiB | 328 MiB |
| First request | 734 / 35 ms | 731 / 30 ms | 143 / 20 ms | 20 / 10 ms |
| Host memory per fork | 13 / 11 MiB | 7 MiB / ≈ 0 | 310 / 316 MiB | 321 / 323 MiB |

- **KVM exits, not page faults:** copying memory in early removes 96% of a fork's host page faults. The fork still needs about 8,000 KVM exits to build the VM's own page tables, one for each 4 KiB page it touches. 2 MiB pages cut that to about 500.
- **Nested hosts:** each exit is expensive under nesting, so 2 MiB pages take a fork from 742 ms to 99 ms with 16 forks at once. The first request then takes 20 ms, the same as in a warm VM.
- **Bare metal:** exits are cheap, so the shared `file` mode is already as fast, at 34 ms, while using about 25 times less memory. Prefetching only adds the time to copy.
- **The trade-off:** prefetched memory is private, about 320 MiB per fork, against about 12 MiB when forks share the snapshot file.
- **64 nested forks:** copying 21 GiB at once becomes the limit. With 2 MiB pages, the reserved pool left only a few GiB for the page cache on this 32 GiB host, which likely slowed the copy to 1.6 s.
- **Transparent huge pages:** no effect. The shared file mapping still loads in 4 KiB pages.

## Local AWS with Floci

The control-plane pieces (S3, DynamoDB, SQS, EventBridge) are a Terraform stack in `aws/`, applied to Floci, a free AWS emulator. The stack has nothing Floci-specific: `.env` points the AWS SDKs and Terraform at Floci and explains each macOS workaround. Without `.env`, the same stack applies to real AWS.

1. Start Floci:

   ```bash
   docker run -d --name floci -p 127.0.0.1:4566:4566 floci/floci:2.5.0
   ```

2. Load the settings and apply the stack:

   ```bash
   set -a && . ./.env && set +a && terraform -chdir=aws init && terraform -chdir=aws apply -auto-approve
   ```

3. Run the checks:

   ```bash
   uv run python aws/check.py
   ```

4. Remove the stack:

   ```bash
   terraform -chdir=aws destroy -auto-approve
   ```

| Check | Result |
|---|---|
| S3: upload and download 8 MiB | Passes, about 170 ms |
| DynamoDB: two conditional READY → CLAIMED writes on one item | Passes: the first wins, the second gets `ConditionalCheckFailedException` |
| EventBridge rule into SQS | Passes: RUNNING and STOPPED arrive, PENDING is filtered out |

## Layout

| Path | Purpose |
|---|---|
| `bench.py` | Runs a benchmark here, or on EC2 with `--ec2` |
| `benchmarks/firecracker.py` | Firecracker VM control and the shared guest and cache settings |
| `benchmarks/snapshots.py` | The `snapshots` benchmark: cold path, memory and disk checkpoints, stage and forks |
| `benchmarks/task_forks.py` | The `task-forks` benchmark: a real task image, a warm snapshot and concurrent forks |
| `benchmarks/uffd.py` | A userfaultfd handler that copies a fork's memory in before it resumes |
| `host-setup.sh` | Prepares a Firecracker host: an XFS reflink cache, Firecracker and `snapshot-editor`, `blockdiff`, and a pinned guest kernel and disk |
| `lima/fc-host.yaml` | Ubuntu 24.04 VM with nested virtualization |
| `aws/main.tf` | Terraform stack: S3 bucket, DynamoDB table, SQS queue and an EventBridge rule into the queue |
| `aws/check.py` | boto3 checks against the stack's Terraform outputs |
| `ec2/runner.py` | Launches a fresh arm64 or x86_64 EC2 host for `--ec2`, prints its results, then cleans up |
| `ec2/user-data.sh` | Runs `host-setup.sh` and `bench.py` on that host and uploads the results |
| `.env` | Floci settings and workarounds |
| `docs/architecture.png` | The diagram above, drawn in Paper |

## Known limitations

- Firecracker 1.18.0 can't point a restored drive at a new file during snapshot load. `bench.py` loads the snapshot paused, repoints the drive with `PATCH /drives`, then resumes.
- [`CognitionAI/blockdiff`](https://github.com/CognitionAI/blockdiff) has no license file, so this repo doesn't include its code. `host-setup.sh` builds it from GitHub at a pinned commit. Using it beyond this experiment needs Cognition's permission or a rewrite.
- Diff memory snapshots are in Firecracker's developer preview, so their behavior may change between releases. Dirty page tracking also slows guest memory writes and cancels most of the benefit of huge pages.
- A snapshot resumes only on the CPU model that made it, so a fleet needs one set of snapshots per host type.
- Forks have no network, and they start with the same hostname, open connections, secrets and random state as the snapshot. Per-fork networking and identity aren't tested yet.
- `task-forks` sends commands over the serial console. Kernel messages printed right after a resume could make the emulated UART miss the receive interrupt, which hung some forks, so the guest's init keeps kernel messages off the console. A real sandbox would use vsock.
