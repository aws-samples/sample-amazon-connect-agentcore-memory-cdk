#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Provision the AgentCore Memory resource and its execution role from scratch.
#
# Outside the CDK lifecycle on purpose: the memory resource holds customer data, so
# `cdk destroy` must never delete it (see docs/teardown.md).
#
# Idempotent: skips the role if it already exists, and refuses to create a second memory
# resource if one with the same name is already present in the target account/region.
#
# All identifiers are parameterized via environment variables (with defaults); no account,
# instance or region value is hardcoded.
set -euo pipefail

PROFILE="${AWS_PROFILE_OVERRIDE:-default}"
REGION="${AWS_REGION_OVERRIDE:?set AWS_REGION_OVERRIDE, e.g. eu-central-1}"
ROLE_NAME="${MEMORY_EXECUTION_ROLE_NAME:-connect-agentcore-memory-execution-role}"
MEMORY_NAME="${MEMORY_RESOURCE_NAME:-connect_ai_agent_memory}"
# The recap strategy's extraction model is a regional inference profile, derived from the
# Region prefix (us., eu., ap., ...). Override with MEMORY_EXTRACTION_MODEL_ID to use a
# different model or inference profile.
EXTRACTION_MODEL_ID="${MEMORY_EXTRACTION_MODEL_ID:-}"
if [[ -z "$EXTRACTION_MODEL_ID" ]]; then
  # Derive the region prefix from REGION (us-east-1 → us, eu-central-1 → eu, etc.)
  REGION_PREFIX="${REGION%%-*}"
  EXTRACTION_MODEL_ID="${REGION_PREFIX}.anthropic.claude-haiku-4-5-20251001-v1:0"
  echo "    auto-derived extraction model: $EXTRACTION_MODEL_ID (from region $REGION)"
fi
EVENT_EXPIRY_DAYS="${MEMORY_EVENT_EXPIRY_DAYS:-90}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

aws_() { aws --profile "$PROFILE" --region "$REGION" "$@"; }

echo "==> account check"
ACCOUNT_ID="$(aws_ sts get-caller-identity --query Account --output text)"
echo "    account: $ACCOUNT_ID  region: $REGION"

# --- execution role trust policy: bedrock-agentcore.amazonaws.com may assume this role,
# scoped to memory resources in this account.  Region is wildcarded so the same role
# works across multiple regions (multi-region deployments share one IAM role). ---------
cat > "$WORKDIR/trust-policy.json" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowAgentCoreMemoryAssume",
      "Effect": "Allow",
      "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {"aws:SourceAccount": "$ACCOUNT_ID"},
        "ArnLike": {"aws:SourceArn": "arn:aws:bedrock-agentcore:*:$ACCOUNT_ID:memory/*"}
      }
    }
  ]
}
EOF

# --- execution role permissions: allow the extraction strategies to invoke foundation
# models and inference profiles in ANY region, so one role serves multi-region deploys. -
cat > "$WORKDIR/execution-policy.json" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowExtractionModelInvoke",
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel", "bedrock:GetInferenceProfile"],
      "Resource": [
        "arn:aws:bedrock:*::foundation-model/*",
        "arn:aws:bedrock:*:$ACCOUNT_ID:inference-profile/*"
      ]
    }
  ]
}
EOF

echo "==> execution role"
if aws iam get-role --profile "$PROFILE" --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  echo "    role $ROLE_NAME already exists, updating trust + inline policies"
  aws iam update-assume-role-policy --profile "$PROFILE" \
    --role-name "$ROLE_NAME" \
    --policy-document "file://$WORKDIR/trust-policy.json"
else
  aws iam create-role --profile "$PROFILE" \
    --role-name "$ROLE_NAME" \
    --assume-role-policy-document "file://$WORKDIR/trust-policy.json" \
    --description "Execution role for AgentCore Memory extraction (Connect AI Agent memory)" \
    --tags Key=project,Value=sample-amazon-connect-agentcore-memory-cdk \
    --query 'Role.Arn' --output text
fi
aws iam put-role-policy --profile "$PROFILE" \
  --role-name "$ROLE_NAME" \
  --policy-name agentcore-memory-extraction \
  --policy-document "file://$WORKDIR/execution-policy.json"
echo "    inline policy applied"

ROLE_ARN="arn:aws:iam::$ACCOUNT_ID:role/$ROLE_NAME"

# --- memory resource: three strategies (SEMANTIC durable facts, CUSTOM contact recap at a
# flat namespace, SUMMARIZATION per contact, which the read path does not query; see
# docs/architecture.md, "Namespaces"). ---------------------------------------------------
cat > "$WORKDIR/memory-resource.json" <<EOF
{
  "name": "$MEMORY_NAME",
  "description": "Cross-contact semantic recall for Amazon Connect AI Agents. actor_id = Customer Profiles profileId, session_id = Connect contactId.",
  "eventExpiryDuration": $EVENT_EXPIRY_DAYS,
  "memoryExecutionRoleArn": "$ROLE_ARN",
  "memoryStrategies": [
    {
      "semanticMemoryStrategy": {
        "name": "connect_customer_semantic",
        "description": "Durable, cross-contact semantic facts about a customer, keyed only on the Customer Profile ID so recall spans every past contact.",
        "namespaces": ["connect/profile/{actorId}/semantic"]
      }
    },
    {
      "customMemoryStrategy": {
        "name": "connect_contact_recap",
        "description": "Recap of each contact at a flat, per-customer namespace, read at contact start.",
        "namespaces": ["connect/profile/{actorId}/recap"],
        "configuration": {
          "semanticOverride": {
            "extraction": {
              "modelId": "$EXTRACTION_MODEL_ID",
              "appendToPrompt": "You are summarising a single customer service contact for an Amazon Connect AI agent that will handle this customer's NEXT contact. Produce at most three records. Each record must be a self-contained recap sentence covering: what the customer contacted us about, what was resolved or promised, and any commitment still outstanding. Always begin each record with 'Previous contact: '. Omit small talk and anything already captured as a durable customer preference or fact."
            }
          }
        }
      }
    },
    {
      "summaryMemoryStrategy": {
        "name": "connect_contact_summary",
        "description": "Built-in summary per contact. The namespace contains the contact id; not read at contact start.",
        "namespaces": ["connect/profile/{actorId}/summary/{sessionId}"]
      }
    }
  ],
  "tags": {"project": "sample-amazon-connect-agentcore-memory-cdk"}
}
EOF

echo "==> memory resource"
EXISTING=$(aws_ bedrock-agentcore-control list-memories \
  --query "memories[?name=='$MEMORY_NAME'].id | [0]" --output text 2>/dev/null || echo "None")
if [[ "$EXISTING" != "None" && -n "$EXISTING" ]]; then
  echo "    memory already exists: $EXISTING - not creating a second one"
  MEMORY_ID="$EXISTING"
else
  # A newly created role can take a few seconds to become usable, and CreateMemory then
  # rejects its trust policy. Retry that case a few times; any other error stops the script.
  CLIENT_TOKEN="provision-$(date +%Y%m%d%H%M%S)"
  for attempt in 1 2 3 4 5 6; do
    if MEMORY_ID=$(aws_ bedrock-agentcore-control create-memory \
        --cli-input-json "file://$WORKDIR/memory-resource.json" \
        --client-token "$CLIENT_TOKEN" \
        --query 'memory.id' --output text 2>"$WORKDIR/create.err"); then
      break
    fi
    if grep -q "valid trust policy" "$WORKDIR/create.err" && [[ $attempt -lt 6 ]]; then
      echo "    execution role not usable yet, retrying in 10s ($attempt/5)"
      sleep 10
      continue
    fi
    cat "$WORKDIR/create.err" >&2
    exit 1
  done
  echo "    created: $MEMORY_ID"
fi

echo "==> waiting for ACTIVE (strategy creation takes ~2-3 min)"
for _ in $(seq 1 60); do
  STATUS=$(aws_ bedrock-agentcore-control get-memory --memory-id "$MEMORY_ID" \
    --query 'memory.status' --output text)
  STRATS=$(aws_ bedrock-agentcore-control get-memory --memory-id "$MEMORY_ID" \
    --query 'memory.strategies[].status' --output text)
  echo "    memory=$STATUS strategies=$STRATS"
  if [[ "$STATUS" == "ACTIVE" && "$STRATS" != *"CREATING"* ]]; then
    break
  fi
  if [[ "$STATUS" == "FAILED" ]]; then
    echo "!!! memory FAILED" >&2
    aws_ bedrock-agentcore-control get-memory --memory-id "$MEMORY_ID" --output json >&2
    exit 1
  fi
  sleep 10
done

echo
echo "memory id : $MEMORY_ID"
echo "memory arn: arn:aws:bedrock-agentcore:$REGION:$ACCOUNT_ID:memory/$MEMORY_ID"
echo "Set cdk.json's context.memoryResourceArn to the ARN above before running cdk deploy."
aws_ bedrock-agentcore-control get-memory --memory-id "$MEMORY_ID" \
  --query 'memory.strategies[].{name:name,type:type,namespaces:namespaces}' --output table
