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

3. Run the benchmark:

   ```bash
   limactl shell fc-host -- python3 bench.py --forks 5 --runs 3
   ```

4. Stop the VM when you're done:

   ```bash
   limactl stop fc-host
   ```

## Run it on EC2

`ec2/run.py` runs the same benchmark on a fresh EC2 host, so you can measure the architecture your machine doesn't have. It prints the results table and terminates the host.

| `--arch` | Default host | Why |
|---|---|---|
| `x86_64` | `c8i.xlarge` | Intel with nested virtualization, and 4 vCPUs and 8 GiB like the Lima VM |
| `arm64` | `c8g.metal-24xl` | Graviton has no nested virtualization, so Firecracker needs bare metal |

Run it from a shell without `.env` loaded, signed in to the AWS account you want to use:

```bash
AWS_PROFILE=your-profile uv run python ec2/run.py --arch x86_64
```

- **What it needs:** a default VPC with internet access, and permission to use EC2, S3 and SSM parameters.
- **Other hosts:** pass `--instance-type` instead of `--arch` with any `.metal` type, or any type with nested virtualization.
- **Benchmark options:** other arguments, such as `--forks 10`, go to `bench.py`.
- **Temporary resources only:** each run makes one host, its disk and one S3 bucket, and nothing else. Files move through the bucket with presigned URLs, so the host needs no IAM role or open port.
- **Normal cleanup:** when the script ends, including on Ctrl-C or `kill`, it terminates the host and deletes the bucket. The disk is deleted with the host.
- **If the script dies:** the host shuts itself down and terminates after `--timeout` minutes (30 by default). The host and bucket also carry an expiry, and every run first removes any expired ones an earlier run left behind.

## Results

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
| `bench.py` | Runs the cold path, memory and disk checkpoints, stage and forks, then prints the results table |
| `host-setup.sh` | Prepares a Firecracker host: an XFS reflink cache, Firecracker and `snapshot-editor`, `blockdiff`, and a pinned guest kernel and disk |
| `lima/fc-host.yaml` | Ubuntu 24.04 VM with nested virtualization |
| `aws/main.tf` | Terraform stack: S3 bucket, DynamoDB table, SQS queue and an EventBridge rule into the queue |
| `aws/check.py` | boto3 checks against the stack's Terraform outputs |
| `ec2/run.py` | Launches a fresh arm64 or x86_64 EC2 host, prints its results, then cleans up |
| `ec2/user-data.sh` | Runs `host-setup.sh` and `bench.py` on that host and uploads the results |
| `.env` | Floci settings and workarounds |
| `docs/architecture.png` | The diagram above, drawn in Paper |

## Known limitations

- Firecracker 1.18.0 can't point a restored drive at a new file during snapshot load. `bench.py` loads the snapshot paused, repoints the drive with `PATCH /drives`, then resumes.
- [`CognitionAI/blockdiff`](https://github.com/CognitionAI/blockdiff) has no license file, so this repo doesn't include its code. `host-setup.sh` builds it from GitHub at a pinned commit. Using it beyond this experiment needs Cognition's permission or a rewrite.
- Diff memory snapshots are in Firecracker's developer preview, so their behavior may change between releases. Dirty page tracking also slows guest memory writes and cancels most of the benefit of huge pages.
- A snapshot resumes only on the CPU model that made it, so a fleet needs one set of snapshots per host type.
