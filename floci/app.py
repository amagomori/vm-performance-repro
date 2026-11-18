import aws_cdk as cdk
from aws_cdk import aws_dynamodb as ddb, aws_events as events, aws_events_targets as targets, aws_s3 as s3, aws_sqs as sqs

app = cdk.App()
stack = cdk.Stack(app, "FlociCheck", env=cdk.Environment(account="000000000000", region="us-east-1"))
bucket = s3.Bucket(stack, "Snapshots", removal_policy=cdk.RemovalPolicy.DESTROY)
table = ddb.Table(stack, "Hosts", partition_key=ddb.Attribute(name="host_id", type=ddb.AttributeType.STRING),
                  billing_mode=ddb.BillingMode.PAY_PER_REQUEST, removal_policy=cdk.RemovalPolicy.DESTROY)
queue = sqs.Queue(stack, "PoolEvents")
events.Rule(stack, "TaskStateRule",
            event_pattern=events.EventPattern(source=["vmrepro.test"], detail_type=["Task State Change"],
                                              detail={"lastStatus": ["RUNNING", "STOPPED"]}),
            targets=[targets.SqsQueue(queue)])
for name, value in [("Bucket", bucket.bucket_name), ("Table", table.table_name), ("QueueUrl", queue.queue_url)]:
    cdk.CfnOutput(stack, name, value=value)
app.synth()
