import json
import os
import subprocess
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

STACK_DIR = Path(__file__).parent
OUTPUTS = {k: v["value"] for k, v in json.loads(subprocess.run(
    ["terraform", f"-chdir={STACK_DIR}", "output", "-json"], capture_output=True, text=True, check=True).stdout).items()}
s3, ddb, sqs, events = (boto3.client(name) for name in ("s3", "dynamodb", "sqs", "events"))


def s3_roundtrip():
    body = os.urandom(8 * 1024 * 1024)
    s3.put_object(Bucket=OUTPUTS["bucket"], Key="snapshots/task/mem.bin", Body=body)
    assert s3.get_object(Bucket=OUTPUTS["bucket"], Key="snapshots/task/mem.bin")["Body"].read() == body
    return "8 MiB put and get, bytes match"


def conditional_claim():
    ddb.put_item(TableName=OUTPUTS["table"], Item={"host_id": {"S": "host-1"}, "state": {"S": "READY"}})
    claim = {
        "TableName": OUTPUTS["table"],
        "Key": {"host_id": {"S": "host-1"}},
        "UpdateExpression": "SET #s = :claimed",
        "ConditionExpression": "#s = :ready",
        "ExpressionAttributeNames": {"#s": "state"},
        "ExpressionAttributeValues": {":claimed": {"S": "CLAIMED"}, ":ready": {"S": "READY"}},
    }
    ddb.update_item(**claim)
    try:
        ddb.update_item(**claim)
    except ClientError as e:
        assert e.response["Error"]["Code"] == "ConditionalCheckFailedException"
        return "first claim wins, second gets ConditionalCheckFailedException"
    raise AssertionError("second claim succeeded")


def eventbridge_to_sqs():
    entries = [{"Source": "vmrepro.test", "DetailType": "Task State Change",
                "Detail": json.dumps({"lastStatus": status})} for status in ("RUNNING", "PENDING", "STOPPED")]
    assert events.put_events(Entries=entries)["FailedEntryCount"] == 0
    received = []
    deadline = time.time() + 10
    while time.time() < deadline and len(received) < 2:
        for msg in sqs.receive_message(QueueUrl=OUTPUTS["queue_url"], MaxNumberOfMessages=10, WaitTimeSeconds=1).get("Messages", []):
            received.append(json.loads(msg["Body"])["detail"]["lastStatus"])
            sqs.delete_message(QueueUrl=OUTPUTS["queue_url"], ReceiptHandle=msg["ReceiptHandle"])
    time.sleep(1.5)
    extra = sqs.receive_message(QueueUrl=OUTPUTS["queue_url"], MaxNumberOfMessages=10, WaitTimeSeconds=1).get("Messages", [])
    assert sorted(received) == ["RUNNING", "STOPPED"] and not extra, (received, len(extra))
    return "RUNNING and STOPPED delivered, PENDING filtered out"


def main():
    for name, check in [("S3 put and get", s3_roundtrip), ("DynamoDB conditional claim", conditional_claim),
                        ("EventBridge rule to SQS", eventbridge_to_sqs)]:
        start = time.perf_counter()
        try:
            detail = check()
            print(f"pass  {name}  {(time.perf_counter() - start) * 1000:.0f} ms  {detail}")
        except Exception as e:
            print(f"FAIL  {name}  {e!r}"[:300])


if __name__ == "__main__":
    main()
