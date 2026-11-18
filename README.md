# vm-performance-repro

Measures how much faster a Firecracker microVM starts from a memory snapshot than from a cold boot, and how much memory and disk the forks of one snapshot share. Disks are copied with XFS reflinks, the method Cognition describes for Devin's VMs, and AWS is emulated locally with [Floci](https://github.com/floci-io/floci).

![Architecture: bench.py drives Firecracker VMs inside a Lima VM on the Mac, using an XFS reflink cache for disks and snapshots, while Terraform and boto3 checks run against Floci in Docker](docs/architecture.png)

## What the benchmark does

1. **Cold path:** boots a VM from a reflink copy of a base disk, with dirty page tracking on, then runs a task setup that writes 50 MB to disk, fills 200 MB of guest RAM and waits 5 s.
2. **Boot snapshot:** right after boot, a diff snapshot saves only the memory pages touched so far. It's the shared base for every task.
3. **Setup snapshot:** after setup, a second diff snapshot saves only the pages the setup changed. A full snapshot of the same state is also taken, for comparison.
4. **Disk checkpoint:** `blockdiff create` stores only the disk blocks that differ from the base disk. `blockdiff` also packs the sparse memory layer for upload. These files are what a fleet would keep in S3.
5. **Stage:** as a host would once per task, the memory layer is unpacked and merged onto the boot snapshot with `snapshot-editor`, and `blockdiff apply` rebuilds the disk. SHA-256 checks confirm the memory matches the full snapshot and the disk matches the source disk, byte for byte.
6. **Forks:** resumes several VMs from the rebuilt snapshot, each on its own reflink copy of the staged disk with memory mapped copy-on-write. Each fork must answer a command and find the setup's marker file.

`bench.py` repeats this and reports the median and range across runs.

## Requirements

- An Apple silicon Mac with an M3 or newer chip on macOS 15 or later, for nested virtualization. A Linux host with `/dev/kvm` also works.
- [Lima](https://lima-vm.io), [Docker](https://www.docker.com), [uv](https://docs.astral.sh/uv/) and [Terraform](https://developer.hashicorp.com/terraform/install) 1.6 or later.
- The Lima config mounts `~/Documents/vm-performance-repro`. Clone the repo there, or change `location` in `lima/fc-host.yaml`.

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

3. Run the benchmark:

   ```bash
   limactl shell fc-host -- python3 bench.py --forks 5 --runs 3
   ```

4. Stop the VM when you're done:

   ```bash
   limactl stop fc-host
   ```

## Results

Firecracker v1.18.0 and `blockdiff` at commit `8d10a60`, on an M4 Mac with 16 GB of RAM, inside a Lima VM with nested virtualization, 4 vCPUs and 8 GiB. Guests have 1 vCPU and 512 MiB. Median and range across 3 runs of 5 forks. The rebased memory matched a full snapshot, and the staged disk matched the source disk, byte for byte in 3 of 3 runs.

| Step | Median | Range |
|---|---|---|
| Reflink copy of the 1 GiB base disk | 1 ms | 1 ms – 1 ms |
| Cold boot to a root shell | 9.5 s | 9.4 s – 9.8 s |
| Task setup (50 MB to disk, 200 MB to RAM, wait 5 s) | 10.2 s | 10.1 s – 10.4 s |
| **Cold path total (boot + setup)** | **19.9 s** | 19.6 s – 19.9 s |
| Memory of the set-up VM (RSS) | 357 MiB | 356 MiB – 357 MiB |
| Boot snapshot: pause + diff since boot + resume | 261 ms | 205 ms – 399 ms |
| Setup snapshot: pause + diff since the boot snapshot | 935 ms | 637 ms – 1.4 s |
| For comparison: full snapshot of the same state | 1.4 s | 1.4 s – 1.5 s |
| Memory file: full snapshot | 512 MiB | 512 MiB – 512 MiB |
| Memory file: boot snapshot (sparse) | 109 MiB | 109 MiB – 109 MiB |
| Memory file: setup layer (sparse) | 265 MiB | 264 MiB – 265 MiB |
| Memory layer packed with `blockdiff` | 265 MiB | 264 MiB – 265 MiB |
| Stage memory: unpack + rebase onto the boot snapshot | 351 ms | 282 ms – 464 ms |
| Disk checkpoint: `blockdiff create` | 5 ms | 3 ms – 10 ms |
| Disk checkpoint size (the full disk is 1024 MiB) | 51 MiB | 51 MiB – 51 MiB |
| Stage disk: `blockdiff apply` onto the base | 1 ms | 1 ms – 2 ms |
| **Upload per task: memory layer + disk diff (full: 1536 MiB)** | **316 MiB** | 316 MiB – 316 MiB |
| Fork: reflink disk copy | 2 ms | 1 ms – 2 ms |
| Fork: snapshot load + resume | 3 ms | 2 ms – 4 ms |
| **Fork: resume to first command answered** | **393 ms** | 340 ms – 472 ms |
| Memory of 5 forks (RSS, total) | 175 MiB | 173 MiB – 177 MiB |
| **Memory of 5 forks (PSS, total)** | **47 MiB** | 46 MiB – 48 MiB |
| Extra cache disk used by 5 forks | ≈ 0 MiB | ≈ 0 MiB – 140 KiB |

- **Start time:** a fork answers in 393 ms against 19.9 s for boot plus setup, about 50× faster, and it skips the setup entirely.
- **Upload per task:** 316 MiB (a 265 MiB memory layer plus a 51 MiB disk diff), against 1536 MiB for a full memory snapshot plus a full disk, about 21%. The 109 MiB boot snapshot is stored once and shared by every task.
- **Memory layer size:** this setup fills 200 MB of RAM on purpose, so its layer is about half the guest's memory. A setup that mostly installs packages would dirty far fewer pages.
- **Fork memory:** five forks hold 47 MiB of their own memory in total (PSS), against 357 MiB for one cold VM after setup. Pages the guest never touched are holes in the rebuilt memory file, so forks hold less than they did with a full snapshot (67 MiB in earlier runs).
- **Cost:** dirty page tracking slowed the setup to 10.2 s, from 8.2 s in earlier runs without it, because KVM logs every guest memory write.
- **Disk speed:** `blockdiff create` and `apply` take milliseconds because, on the same XFS filesystem, they clone blocks instead of copying them. On a real host, the cost of staging is downloading the diffs.
- **Caveat:** these numbers come from nested virtualization on a Mac. They show the method works, not how fast an EC2 host would be.

## Local AWS with Floci

The control-plane pieces (S3, DynamoDB, SQS, EventBridge) are a Terraform stack in `aws/`, applied to Floci, a free AWS emulator. The provider block has nothing Floci-specific: `.env` points the AWS SDKs and Terraform at Floci and explains each macOS workaround. Without `.env`, the same stack applies to real AWS.

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
| `bench.py` | Runs the cold path, memory and disk checkpoints, stage and forks, then prints the results table |
| `host-setup.sh` | Prepares a Firecracker host, installs `snapshot-editor` and builds `blockdiff`. Set `FC_DEVICE` to use a real disk, such as EC2 NVMe, instead of a loop file. |
| `lima/fc-host.yaml` | Ubuntu 24.04 VM with nested virtualization |
| `aws/main.tf` | Terraform stack: S3 bucket, DynamoDB table, SQS queue and an EventBridge rule into the queue |
| `aws/check.py` | boto3 checks against the stack's Terraform outputs |
| `.env` | Floci settings and workarounds |
| `docs/architecture.png` | The diagram above, drawn in Paper |

## Known limitations

- Firecracker 1.18.0 can't point a restored drive at a new file during snapshot load. `bench.py` loads the snapshot paused, repoints the drive with `PATCH /drives`, then resumes.
- [`CognitionAI/blockdiff`](https://github.com/CognitionAI/blockdiff) has no license file, so this repo doesn't include its code. `host-setup.sh` builds it from GitHub at a pinned commit. Using it beyond this experiment needs Cognition's permission or a rewrite.
- Diff memory snapshots are in Firecracker's developer preview, so their behavior may change between releases. Dirty page tracking also slows guest memory writes and cancels most of the benefit of huge pages.
- Snapshots taken on Arm don't predict Intel hosts, and a snapshot resumes only on the CPU model that made it.
- `terraform init` downloads the AWS provider, about 730 MB unpacked, into `aws/.terraform/`.
