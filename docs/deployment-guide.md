# Deployment guide

Deploys `MemoryWriterStack` and `MemoryReaderStack` from a fresh clone to a working memory
pipeline. Read [architecture.md](architecture.md) first. Replace every `<placeholder>`
with your own value.

## 0. Prerequisites

- An Amazon Connect instance with an AI agent for chat.
- AI agent logging enabled on the instance. Check, and enable if the value is `false`:
  ```bash
  aws connect list-instance-attributes --instance-id <instance-id> --region <region> \
    | grep AUTOMATED_INTERACTION_LOG
  aws connect update-instance-attribute --instance-id <instance-id> \
    --attribute-type AUTOMATED_INTERACTION_LOG --value true --region <region>
  ```
- The AI agent log group `/aws/connect/ai-agent/<instance-alias>`. If it does not exist
  yet, create it before you deploy:
  ```bash
  aws logs create-log-group --log-group-name /aws/connect/ai-agent/<instance-alias> \
    --region <region>
  ```
- A free subscription filter slot on that log group (step 2 checks it).
- Customer Profiles enabled on the instance, and an AI agent assistant for the instance.
- Python 3.12, `pip`, the AWS CDK CLI v2 and the AWS CLI v2, with credentials for the
  target account.

```bash
git clone https://github.com/aws-samples/sample-amazon-connect-agentcore-memory-cdk.git
cd sample-amazon-connect-agentcore-memory-cdk
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

## 1. Configure `cdk.json`

Replace every `REPLACE_WITH_...` placeholder in the `context` block (see the README's
Configuration table). Fill in `memoryResourceArn` after step 3. You can also leave
`cdk.json` unchanged and pass each value with `-c key=value`.

## 2. Pre-flight checks

`scripts/preflight.py` checks, read-only:

1. The AI agent log group exists.
2. The log group has a free subscription filter slot (it reads your account's quota).
3. The AgentCore Memory resource is ACTIVE and all its strategies are created.
4. The AI agent assistant ARN or id resolves.
5. The CDK is bootstrapped in the account and Region.

```bash
python3 scripts/preflight.py --profile <aws-profile>
# or: make preflight PREFLIGHT_ARGS="--profile <aws-profile>"
```

Each check prints `PASS` or `FAIL` with a message, and the script exits non-zero on any
failure. On a first deployment, checks 3 and 4 fail until step 3 is done.

## 3. Provision the AgentCore Memory resource

Run once per account and Region. The script creates, outside CDK:

- an IAM execution role that AgentCore Memory assumes to run the extraction model (trust
  limited to your account's memory resources), and
- the memory resource with three strategies: SEMANTIC for durable facts, CUSTOM for a
  recap per contact and SUMMARIZATION for a summary per contact (not read at contact
  start; see [architecture.md](architecture.md#namespaces)).

```bash
AWS_REGION_OVERRIDE=<region> AWS_PROFILE_OVERRIDE=<aws-profile> ./scripts/provision_memory.sh
```

The script waits until the resource and its strategies are ACTIVE and prints the ARN:

```
memory id : <memory-id>
memory arn: arn:aws:bedrock-agentcore:<region>:<account-id>:memory/<memory-id>
```

Copy the ARN into `memoryResourceArn` in `cdk.json`. Optional environment variables:
`MEMORY_RESOURCE_NAME` (default `connect_ai_agent_memory`), `MEMORY_EXECUTION_ROLE_NAME`,
`MEMORY_EXTRACTION_MODEL_ID` (a regional inference profile; derived from the Region by
default) and `MEMORY_EVENT_EXPIRY_DAYS` (default 90). The script is idempotent: if a
resource with the same name exists, it prints that resource instead of creating one.
Re-run preflight; checks 3 and 4 now pass.

## 4. Bundle the Lambda packages

```bash
./scripts/bundle_lambdas.sh   # or: make bundle
```

The script copies each handler and `src/connect_memory` into `dist/<lambda>` and installs
the hash-locked `lambdas/<lambda>/requirements.txt` (boto3 and its dependencies) with
`pip install --require-hashes`. The sample bundles its own boto3 so the Lambdas do not
depend on the SDK version of the managed runtime. The stacks fail at synth time if
`dist/` is missing. Re-run after changing `src/connect_memory/` or a requirements file.
To change a Lambda dependency, edit `requirements.in` and run `make lock` (needs
pip-tools).

## 5. Bootstrap the CDK (once per account and Region)

```bash
cdk bootstrap --profile <aws-profile>
```

## 6. Synth and deploy

```bash
cdk synth --profile <aws-profile>
cdk deploy --all --profile <aws-profile>
```

`cdk synth` validates the `cdk.json` values offline and runs cdk-nag. Note the outputs;
you need `ReaderFunctionArn` in step 7:

```
MemoryWriterStack.WriterFunctionArn = arn:aws:lambda:...
MemoryWriterStack.WriterDlqUrl = https://sqs...
MemoryWriterStack.DedupTableName = MemoryWriterStack-DedupTable...
MemoryReaderStack.ReaderFunctionArn = arn:aws:lambda:...
```

## 7. Change the contact flow (manual)

Follow [connect-flow-instructions.md](connect-flow-instructions.md), sections 1 and 2:
bind `customerProfileId` to a verified customer, then add the reader Lambda block.

## 8. Change the AI agent prompt (manual)

Follow [connect-flow-instructions.md](connect-flow-instructions.md), section 3.

## 9. Verification

Use a test customer profile that your flow can look up on a verified identifier.

1. **Write.** Start a chat through the flow as the test customer and state a fact the AI
   agent can store, for example a seating preference. End the chat. The writer's log
   group `/aws/lambda/MemoryWriterStack-writer` shows a `wrote qic_session=...` line for
   the contact.
2. **Extraction.** After a few minutes, list the customer's records:
   ```bash
   aws bedrock-agentcore list-memory-records --memory-id <memory-id> \
     --namespace connect/profile/<customer-profile-id>/semantic \
     --region <region> --profile <aws-profile> \
     --query "memoryRecordSummaries[].content.text"
   ```
3. **Read.** Start a second chat as the same customer and ask about the fact. The reader's
   log group `/aws/lambda/MemoryReaderStack-reader` shows `available=True` and
   `delivered=True`, and the AI agent can use the fact in its answer.
4. **No binding, no memory.** Start a chat that does not match a profile (None Found
   branch). The reader logs `skip_reason=no_profile_id`, and the writer logs
   `no actor_id resolved` for that contact.

When you finish testing, delete the test customer's memory with
`scripts/cleanup_memory.py` ([teardown.md](teardown.md)).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `cdk synth` fails with a `ValueError` about a context key | A `cdk.json` value is missing, still a placeholder or malformed; the message names the key. |
| "Writer/Reader Lambda bundle not found" | Run `./scripts/bundle_lambdas.sh` before synth or deploy. |
| Deploy fails on the AI agent log group | The log group does not exist yet; create it (step 0). |
| Preflight check 2 fails | Every subscription filter slot of the log group is in use. Free one before deploying `MemoryWriterStack`. |
| Preflight check 3 fails | Run `scripts/provision_memory.sh`, or wait until the resource is ACTIVE. |
| `provision_memory.sh` fails with "Invalid Bedrock model ID" | The extraction model must be a regional inference profile (for example `us.anthropic...` in us-east-1). Set `MEMORY_EXTRACTION_MODEL_ID`. |
| Writer never invoked | AI agent logging is off, or the contacts did not use the AI agent. |
| Writer logs `no_profile_attribute` | The flow did not set `customerProfileId` for the contact (step 7). |
| Reader returns `memoryAvailable` `"false"` for a known customer | Extraction has not finished yet, the profile id differs from the one written, or the flow passes the wrong attribute. Check with `list-memory-records` (step 9.2). |
| Reader returns `memoryDelivered` `"false"` with records | `WisdomAssistantArn` or `WisdomSessionArn` is missing or wrong in the Lambda block. |
