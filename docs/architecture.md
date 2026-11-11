# Architecture

Account, instance and Region values for a deployment live only in `cdk.json` context.

## Where AgentCore Memory fits

| Store | Holds | Scope |
|---|---|---|
| Customer Profiles | Structured facts (name, contact details, order history) | Cross-contact, structured |
| Contact attributes | Per-contact working data | One contact |
| **AgentCore Memory** | What the customer said in earlier conversations | **Cross-contact, unstructured** |

Customer Profiles knows a customer's loyalty tier. AgentCore Memory lets the AI agent
recall "last time you asked for a window seat" weeks later, a preference that was
never structured data anywhere.

## Write path (asynchronous)

`lambdas/writer/lambda_function.py` with `src/connect_memory/writer.py`:

```
CloudWatch Logs subscription filter on /aws/connect/ai-agent/<alias>
  │
  ▼
Writer Lambda
  ├─ decode payload, build turns              (connect_memory.parser)
  ├─ group turns by AI agent session
  │
  ▼ per session
  keep customer and AI agent turns only       (no reasoning, no tool input or output)
  resolve identity                            (below; none found => skip the session)
  dedup.is_written(orchestration_id)?         (DynamoDB, consistent read; yes => skip)
  CreateEvent(actorId = profile id, sessionId = contact id, clientToken = hash of unwritten keys)
  dedup.mark_written(keys)                    (only after CreateEvent succeeded)
```

CloudWatch Logs delivers subscription data at least once, so the same batch can arrive
twice. The dedup table skips sessions that were already written. Keys are marked only
after a successful write, so a failed write is retried rather than skipped. Two
invocations that race on the same batch send the same `clientToken`, which CreateEvent
uses to complete the operation no more than once. A partial redelivery (new turns added
to a session that was partly written) gets a different token, so the new turns are
written.

The handler attempts every session in a batch and re-raises afterwards if any
`CreateEvent` failed. Lambda then retries the asynchronous invocation and, once retries
are exhausted, sends the batch to the SQS dead-letter queue.

### Identity

Trace events do not carry the Customer Profiles profile id. `src/connect_memory/identity.py`
resolves it without new state or flow changes:

```
trace event ──> session_id + assistant_id
  └─ qconnect.GetSession ──> session.name == Connect contact id
       └─ connect.GetContactAttributes ──> Attributes["customerProfileId"] == actor id
```

The contact flow sets `customerProfileId` from `$.Customer.ProfileId` after a verified
**Get profile** match and clears it on every other path
([connect-flow-instructions.md](connect-flow-instructions.md), section 1). A contact
without a valid profile id (32 hexadecimal characters) is skipped. The writer never
substitutes a synthetic actor id: that would create a memory that can never be recalled
on another contact.

## Read path (synchronous, at contact start)

`lambdas/reader/lambda_function.py` with `src/connect_memory/reader.py`:

```
Flow "AWS Lambda function" block
  Parameters: CustomerProfileId, WisdomAssistantArn, WisdomSessionArn
  │
  ▼
Reader Lambda
  ├─ RetrieveMemoryRecords(.../semantic) ─┐ in parallel, one shared 3.5 s budget,
  ├─ RetrieveMemoryRecords(.../recap)    ─┘ top 3 records each
  │
  ├─ format_memory_context()   <customer_memory> block, untrusted-data notice,
  │                            delimiter tags stripped, at most 2,000 characters
  ├─ qconnect.UpdateSessionData(Custom.MemoryContext = block)
  └─ return {memoryAvailable, memoryRecordCount, memoryDelivered}
```

The reader never raises. No profile id, a malformed profile id, a timeout, a service
error or an empty result all resolve to "no memory", and the flow continues. The memory
text is not returned to the flow, so it is never copied into contact attributes. An unset
`Custom.MemoryContext` renders as an empty string in the prompt.

## Namespaces

| Namespace | Strategy | Read at contact start |
|---|---|---|
| `connect/profile/{actorId}/semantic` | SEMANTIC: durable facts | Yes |
| `connect/profile/{actorId}/recap` | CUSTOM (semantic override): one recap per contact | Yes |
| `connect/profile/{actorId}/summary/{sessionId}` | SUMMARIZATION: one summary per contact | No |

`read_namespaces` in `src/connect_memory/identity.py` defines what the reader queries. The
reader issues one call per exact namespace name rather than relying on prefix matching.
The summary namespace contains the contact id, which the reader does not know for earlier
contacts, so the recap strategy provides a flat, per-customer record of previous
contacts. The deletion script removes all three, plus the raw events.

Because the namespaces are keyed on the profile id, a mismatch between what the writer
stores and what the reader queries returns an empty result rather than an error. The
namespace templates in `scripts/provision_memory.sh` and the helpers in `identity.py` must
stay in sync; the unit tests check the helpers.

## Timing

Long-term records are extracted asynchronously after `CreateEvent`. Recall works for the
customer's next contact; facts from the contact in progress are not available during it.
The reader's retrieval budget (3.5 seconds) and the Lambda timeout (8 seconds, the
maximum of a synchronous flow Lambda block) bound the time the flow waits.

## Threat model summary

| Threat | Mitigation | Residual risk |
|---|---|---|
| A contact is bound to another customer's profile id | Flow binding rules (clear, verified lookup, Success branch only); invalid ids skipped | Depends on the deployer's flow |
| Memory poisoning or prompt injection through what customers say | Untrusted-data framing, delimiter stripping, 2,000-character cap, top 3 records per namespace | Add guardrails or redaction before `CreateEvent` for production |
| Customer data kept after a deletion request | `scripts/cleanup_memory.py` deletes events and records and verifies | DLQ (14 days) and logs (profile ids, 1 month) are outside the script |
| Memory copied into contact records | Reader returns flags only | None in the sample |
| Write lost after a transient failure | Keys marked after success, retries, DLQ | None in the sample |
| Supply-chain change in the Lambda bundle | Hash-locked requirements, Dependabot | Review updates before merging |
| Over-broad IAM | Resource-scoped statements per function | Memory execution role allows any model (scope it for production) |
