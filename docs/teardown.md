# Teardown

Clean teardown procedure for this project's resources, and what is deliberately left
behind.

## Per-customer deletion (delete one customer's memory)

To delete everything the memory resource holds for one customer **without** removing any
infrastructure, run the deletion script. It removes the customer's raw conversation events
(every session) and all long-term records under `connect/profile/<customer-profile-id>/`
(semantic facts, contact recaps and per-contact summaries), then lists again and exits
non-zero if anything is left. Any AWS error stops the script.

```bash
# Preview what would be deleted (no changes made):
python3 scripts/cleanup_memory.py --memory-id <memory-id> --region <region> \
  --aws-profile <aws-profile> --profile-id <customer-profile-id> --dry-run

# Delete (asks for confirmation; add --yes to skip the prompt):
python3 scripts/cleanup_memory.py --memory-id <memory-id> --region <region> \
  --aws-profile <aws-profile> --profile-id <customer-profile-id>

# List the customer profile ids (actor ids) the memory resource holds:
python3 scripts/cleanup_memory.py --memory-id <memory-id> --region <region> \
  --aws-profile <aws-profile> --list-actors
```

Run it after the customer's contacts have ended, because a conversation that is still in
progress can write new events. The script does **not** change the customer's Amazon Connect
Customer Profiles record, the Lambda log groups (which hold profile ids, kept one month) or
the writer's dead-letter queue (which can hold failed batches, kept 14 days).

## Before you destroy the stacks

Remove or disconnect the reader Lambda block in every flow that uses it. After the reader
function is deleted, a flow block that still references it takes its error branch on
every contact.

## What `cdk destroy` removes

```bash
cdk destroy --all --profile <aws-profile>
```

- Both Lambda functions and their log groups
- Both IAM execution roles
- The SQS dead-letter queue
- The subscription filter `memory-writer` on the AI agent log group (the log group itself
  is imported and is not changed)
- The `AWS::Connect::IntegrationAssociation` of the reader Lambda

## What `cdk destroy` leaves in place

- **The AgentCore Memory resource and its execution role.** They are created by
  `scripts/provision_memory.sh`, outside CDK, so that `cdk destroy` never deletes
  customer data.
- **The DynamoDB dedup table.** Its removal policy is `RETAIN`; it holds orchestration
  or AI agent session ids only (90-day TTL), no customer content.
- **Other subscription filters** on the AI agent log group.
- **Your flow and AI agent prompt changes**, which are not managed by CDK.

## Full cleanup (optional)

Delete what `cdk destroy` leaves only after you confirm the data is no longer needed:

```bash
# 1. Dedup table (name from the DedupTableName stack output)
aws dynamodb delete-table --table-name <dedup-table-name> --profile <aws-profile>

# 2. Memory resource. This permanently deletes all stored memory for all customers.
aws bedrock-agentcore-control delete-memory --memory-id <memory-id> --profile <aws-profile>

# 3. Memory execution role (default name; check it matches what you provisioned and
#    that nothing else uses it)
aws iam delete-role-policy --role-name connect-agentcore-memory-execution-role \
  --policy-name agentcore-memory-extraction --profile <aws-profile>
aws iam delete-role --role-name connect-agentcore-memory-execution-role --profile <aws-profile>
```

## Verify the teardown

```bash
aws cloudformation describe-stacks --stack-name MemoryWriterStack --profile <aws-profile>
aws cloudformation describe-stacks --stack-name MemoryReaderStack --profile <aws-profile>
```

Both return a "does not exist" error once the stacks are deleted. If a stack shows
`DELETE_FAILED`, check its events in the CloudFormation console.
