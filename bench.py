import argparse
import os
import sys

from benchmarks import snapshots, task_forks
from benchmarks.firecracker import Cache

BENCHMARKS = {"snapshots": snapshots, "task-forks": task_forks}


def main():
    parser = argparse.ArgumentParser(description="Run a Firecracker benchmark and print its results as Markdown.")
    parser.add_argument("--ec2", metavar="HOST",
                        help="Run on a fresh EC2 host instead of here: arm64, x86_64, or an instance type.")
    parser.add_argument("--timeout", type=int, default=30, help="With --ec2, minutes before the host terminates itself.")
    commands = parser.add_subparsers(dest="benchmark", required=True)
    for name, module in BENCHMARKS.items():
        module.configure(commands.add_parser(name, help=module.HELP, description=module.HELP))
    args = parser.parse_args()
    if args.ec2:
        from ec2 import runner
        runner.run(args.ec2, args.timeout, sys.argv[sys.argv.index(args.benchmark):])
    else:
        BENCHMARKS[args.benchmark].main(args, Cache(os.environ.get("FC_ROOT", "/srv/fc")))


if __name__ == "__main__":
    main()
