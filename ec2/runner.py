import io
import shlex
import signal
import sys
import tarfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

REPO = Path(__file__).resolve().parent.parent
AWS = Config(signature_version="s3v4", ignore_configured_endpoint_urls=True)
DEFAULT_TYPES = {"arm64": "c8g.metal-24xl", "x86_64": "c8i.xlarge"}
AMI_PARAM = "/aws/service/canonical/ubuntu/server/24.04/stable/current/{}/hvm/ebs-gp3/ami-id"
NAME = "vm-performance-repro"
BUCKET_PREFIX = "vmrepro-ec2-"


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

    def launch(self, ec2, user_data, expires):
        tags = [{"Key": "Name", "Value": NAME}, {"Key": "expires", "Value": str(expires)}]
        return ec2.run_instances(
            ImageId=self.ami, InstanceType=self.instance_type, MinCount=1, MaxCount=1, SubnetId=self.subnet,
            UserData=user_data, InstanceInitiatedShutdownBehavior="terminate",
            BlockDeviceMappings=[{"DeviceName": self.root_device,
                                  "Ebs": {"VolumeSize": 60, "VolumeType": "gp3", "DeleteOnTermination": True}}],
            TagSpecifications=[{"ResourceType": kind, "Tags": tags} for kind in ("instance", "volume")],
            **({"CpuOptions": {"NestedVirtualization": "enabled"}} if self.nested else {}),
        )["Instances"][0]["InstanceId"]


class ScratchBucket:
    def __init__(self, expires):
        self.bucket = boto3.resource("s3", config=AWS).Bucket(f"{BUCKET_PREFIX}{expires}-{uuid.uuid4().hex[:8]}")
        self.expires = expires

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
            method, Params={"Bucket": self.bucket.name, "Key": key}, ExpiresIn=self.expires - int(time.time()))

    def put(self, key, body):
        self.bucket.put_object(Key=key, Body=body)
        return self.url("get_object", key)

    def has(self, key):
        return any(self.bucket.objects.filter(Prefix=key))

    def read(self, key):
        return self.bucket.Object(key).get()["Body"].read().decode()


def sweep(ec2):
    now = time.time()
    leftovers = ec2.describe_instances(Filters=[
        {"Name": "tag:Name", "Values": [NAME]},
        {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
    ])["Reservations"]
    for instance in (i for r in leftovers for i in r["Instances"]):
        if int(next((t["Value"] for t in instance["Tags"] if t["Key"] == "expires"), 0)) < now:
            ec2.terminate_instances(InstanceIds=[instance["InstanceId"]])
            print(f"Terminated expired host {instance['InstanceId']}")
    for bucket in boto3.resource("s3", config=AWS).buckets.filter(Prefix=BUCKET_PREFIX):
        if int(bucket.name.removeprefix(BUCKET_PREFIX).split("-")[0]) < now:
            bucket.objects.all().delete()
            bucket.delete()
            print(f"Deleted expired bucket {bucket.name}")


def stop(signum, frame):
    sys.exit(f"Stopped by signal {signum}")


def bundle():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path in [REPO / "host-setup.sh", REPO / "bench.py", *sorted((REPO / "benchmarks").glob("*.py"))]:
            tar.add(path, arcname=path.relative_to(REPO))
    return buffer.getvalue()


def user_data(files, timeout, bench_args):
    env = {
        "BUNDLE_URL": files.put("bundle.tgz", bundle()),
        "LOG_URL": files.url("put_object", "log.txt"),
        "RESULTS_URL": files.url("put_object", "results.md"),
        "TIMEOUT": str(timeout),
    }
    shebang, body = (REPO / "ec2" / "user-data.sh").read_text().split("\n", 1)
    return "\n".join([shebang, *(f"{key}={shlex.quote(value)}" for key, value in env.items()),
                      f"BENCH_ARGS=({shlex.join(bench_args)})", body])


def state_of(ec2, instance):
    try:
        return ec2.describe_instances(InstanceIds=[instance])["Reservations"][0]["Instances"][0]["State"]["Name"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "InvalidInstanceID.NotFound":
            return "pending"
        raise


def wait_for_log(ec2, instance, files, minutes):
    start, state = time.time(), None
    while not files.has("log.txt"):
        current = state_of(ec2, instance)
        if current != state:
            state = current
            print(f"{(time.time() - start) / 60:4.1f} min: {state}", flush=True)
        if state in ("shutting-down", "terminated") or time.time() - start > minutes * 60:
            return
        time.sleep(15)


def run(target, timeout, bench_args):
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, stop)
    ec2 = boto3.client("ec2", config=AWS)
    sweep(ec2)
    host = Host.lookup(ec2, DEFAULT_TYPES.get(target, target))
    expires = int(time.time()) + (timeout + 30) * 60
    with ScratchBucket(expires) as files:
        instance = host.launch(ec2, user_data(files, timeout, bench_args), expires)
        print(f"{instance}: {host.instance_type} ({host.arch}) in {host.zone}", flush=True)
        try:
            wait_for_log(ec2, instance, files, timeout + 15)
        finally:
            ec2.terminate_instances(InstanceIds=[instance])
        if files.has("results.md"):
            print(files.read("results.md"))
        elif files.has("log.txt"):
            print("\n".join(files.read("log.txt").splitlines()[-40:]))
            sys.exit("The benchmark failed. The last 40 log lines are above.")
        else:
            sys.exit("The host stopped without uploading its log.")
