import json, os, time
import boto3
from botocore.exceptions import ClientError

out = json.load(open("outputs.json"))["FlociCheck"]
s3, ddb, sqs, ev = (boto3.client(n) for n in ("s3", "dynamodb", "sqs", "events"))
results = []

def check(name, fn):
    t = time.perf_counter()
    try:
        detail = fn()
        results.append((name, "pass", f"{(time.perf_counter() - t) * 1000:.0f} ms", detail))
    except Exception as e:
        results.append((name, "FAIL", "", repr(e)[:160]))

def s3_roundtrip():
    body = os.urandom(8 * 1024 * 1024)
    s3.put_object(Bucket=out["Bucket"], Key="snapshots/task-a/mem.bin", Body=body)
    got = s3.get_object(Bucket=out["Bucket"], Key="snapshots/task-a/mem.bin")["Body"].read()
    assert got == body
    return "8 MiB put + get, bytes match"

def ddb_claim():
    t = out["Table"]
    ddb.put_item(TableName=t, Item={"host_id": {"S": "host-1"}, "state": {"S": "READY"}, "version": {"N": "1"}})
    claim = dict(TableName=t, Key={"host_id": {"S": "host-1"}}, UpdateExpression="SET #s = :c",
                 ConditionExpression="#s = :r", ExpressionAttributeNames={"#s": "state"},
                 ExpressionAttributeValues={":c": {"S": "CLAIMED"}, ":r": {"S": "READY"}})
    ddb.update_item(**claim)
    try:
        ddb.update_item(**claim)
    except ClientError as e:
        assert e.response["Error"]["Code"] == "ConditionalCheckFailedException"
        return "first claim wins, second gets ConditionalCheckFailedException"
    raise AssertionError("second claim succeeded")

def eventbridge_to_sqs():
    entries = [{"Source": "vmrepro.test", "DetailType": "Task State Change", "Detail": json.dumps({"lastStatus": s, "taskArn": f"task-{s}"})}
               for s in ("RUNNING", "PENDING", "STOPPED")]
    resp = ev.put_events(Entries=entries)
    assert resp["FailedEntryCount"] == 0
    got = []
    deadline = time.time() + 10
    while time.time() < deadline and len(got) < 2:
        for m in sqs.receive_message(QueueUrl=out["QueueUrl"], MaxNumberOfMessages=10, WaitTimeSeconds=1).get("Messages", []):
            got.append(json.loads(m["Body"])["detail"]["lastStatus"])
            sqs.delete_message(QueueUrl=out["QueueUrl"], ReceiptHandle=m["ReceiptHandle"])
    time.sleep(1.5)
    extra = sqs.receive_message(QueueUrl=out["QueueUrl"], MaxNumberOfMessages=10, WaitTimeSeconds=1).get("Messages", [])
    assert sorted(got) == ["RUNNING", "STOPPED"] and not extra, (got, len(extra))
    return "RUNNING and STOPPED delivered, PENDING filtered out"

check("S3 put and get", s3_roundtrip)
check("DynamoDB conditional claim", ddb_claim)
check("EventBridge rule to SQS", eventbridge_to_sqs)
for r in results:
    print(" | ".join(r))
