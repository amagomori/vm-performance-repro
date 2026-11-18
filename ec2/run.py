import argparse
import shlex
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.config import Config

HERE = Path(__file__).resolve().parent
AWS = Config(signature_version="s3v4", ignore_configured_endpoint_urls=True)
DEFAULT_TYPES = {"arm64": "c8g.metal-24xl", "x86_64": "c8i.xlarge"}
AMI_PARAM = "/aws/service/canonical/ubuntu/server/24.04/stable/current/{}/hvm/ebs-gp3/ami-id"


@dataclass(frozen=True)
class Host:
    instance_type: str
    arch: str
    nested: bool
    subnet: str
    zone: str
    ami: str
    root_device: str

    @classmethod
    def lookup(cls, ec2, instance_type):
        info = ec2.describe_instance_types(InstanceTypes=[instance_type])["InstanceTypes"][0]
        arch = "arm64" if "arm64" in info["ProcessorInfo"]["SupportedArchitectures"] else "x86_64"
        nested = not info["BareMetal"]
        if nested and "nested-virtualization" not in info["ProcessorInfo"].get("SupportedFeatures", []):
            sys.exit(f"{instance_type} has no /dev/kvm. Pick a .metal type or one with nested virtualization.")
        zones = {o["Location"] for o in ec2.describe_instance_type_offerings(
            LocationType="availability-zone", Filters=[{"Name": "instance-type", "Values": [instance_type]}],
        )["InstanceTypeOfferings"]}
        subnets = [s for s in ec2.describe_subnets(Filters=[{"Name": "default-for-az", "Values": ["true"]}])["Subnets"]
                   if s["AvailabilityZone"] in zones]
        if not subnets:
            sys.exit(f"No default subnet in a zone that offers {instance_type}")
        subnet = min(subnets, key=lambda s: s["AvailabilityZone"])
        ami = boto3.client("ssm", config=AWS).get_parameter(
            Name=AMI_PARAM.format("amd64" if arch == "x86_64" else "arm64"))["Parameter"]["Value"]
        return cls(instance_type, arch, nested, subnet["SubnetId"], subnet["AvailabilityZone"], ami,
                   ec2.describe_images(ImageIds=[ami])["Images"][0]["RootDeviceName"])

    def launch(self, ec2, user_data):
        return ec2.run_instances(
            ImageId=self.ami, InstanceType=self.instance_type, MinCount=1, MaxCount=1, SubnetId=self.subnet,
            UserData=user_data, InstanceInitiatedShutdownBehavior="terminate",
            BlockDeviceMappings=[{"DeviceName": self.root_device, "Ebs": {"VolumeSize": 60, "VolumeType": "gp3"}}],
            TagSpecifications=[{"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": "vm-performance-repro"}]}],
            **({"CpuOptions": {"NestedVirtualization": "enabled"}} if self.nested else {}),
        )["Instances"][0]["InstanceId"]


class ScratchBucket:
    def __init__(self, minutes):
        self.bucket = boto3.resource("s3", config=AWS).Bucket(f"vmrepro-ec2-{uuid.uuid4().hex[:12]}")
        self.expires = minutes * 60

    def __enter__(self):
        region = self.bucket.meta.client.meta.region_name
        self.bucket.create(**({} if region == "us-east-1" else
                              {"CreateBucketConfiguration": {"LocationConstraint": region}}))
        return self

    def __exit__(self, *exc):
        self.bucket.objects.all().delete()
        self.bucket.delete()

    def url(self, method, key):
        return self.bucket.meta.client.generate_presigned_url(
            method, Params={"Bucket": self.bucket.name, "Key": key}, ExpiresIn=self.expires)

    def upload(self, path):
        self.bucket.upload_file(str(path), path.name)
        return self.url("get_object", path.name)

    def has(self, key):
        return any(self.bucket.objects.filter(Prefix=key))

    def read(self, key):
        return self.bucket.Object(key).get()["Body"].read().decode()


def user_data(files, timeout, bench_args):
    env = {
        "HOST_SETUP_URL": files.upload(HERE.parent / "host-setup.sh"),
        "BENCH_URL": files.upload(HERE.parent / "bench.py"),
        "LOG_URL": files.url("put_object", "log.txt"),
        "RESULTS_URL": files.url("put_object", "results.md"),
        "TIMEOUT": str(timeout),
    }
    shebang, body = (HERE / "user-data.sh").read_text().split("\n", 1)
    return "\n".join([shebang, *(f"{key}={shlex.quote(value)}" for key, value in env.items()),
                      f"BENCH_ARGS=({shlex.join(bench_args)})", body])


def wait_for_log(ec2, instance, files, minutes):
    start, state = time.time(), None
    while not files.has("log.txt"):
        current = ec2.describe_instances(InstanceIds=[instance])["Reservations"][0]["Instances"][0]["State"]["Name"]
        if current != state:
            state = current
            print(f"{(time.time() - start) / 60:4.1f} min: {state}", flush=True)
        if state in ("shutting-down", "terminated") or time.time() - start > minutes * 60:
            return
        time.sleep(15)


def main():
    parser = argparse.ArgumentParser(
        description="Run bench.py on a fresh EC2 host and print its results. Other arguments go to bench.py.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--arch", choices=DEFAULT_TYPES,
                        help="Use the default host for this arch: c8g.metal-24xl or c8i.xlarge.")
    target.add_argument("--instance-type", help="Any .metal type, or any type with nested virtualization.")
    parser.add_argument("--timeout", type=int, default=30, help="Minutes before the host shuts down and terminates.")
    args, bench_args = parser.parse_known_args()

    ec2 = boto3.client("ec2", config=AWS)
    host = Host.lookup(ec2, args.instance_type or DEFAULT_TYPES[args.arch])
    with ScratchBucket(args.timeout + 30) as files:
        instance = host.launch(ec2, user_data(files, args.timeout, bench_args))
        print(f"{instance}: {host.instance_type} ({host.arch}) in {host.zone}", flush=True)
        try:
            wait_for_log(ec2, instance, files, args.timeout + 15)
        finally:
            ec2.terminate_instances(InstanceIds=[instance])
        if files.has("results.md"):
            print(files.read("results.md"))
        elif files.has("log.txt"):
            print("\n".join(files.read("log.txt").splitlines()[-40:]))
            sys.exit("bench.py failed. The last 40 log lines are above.")
        else:
            sys.exit("The host stopped without uploading its log.")


if __name__ == "__main__":
    main()
