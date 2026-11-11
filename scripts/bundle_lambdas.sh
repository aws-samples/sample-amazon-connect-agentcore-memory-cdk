#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Build deployment packages for the writer and reader Lambdas by vendoring their
# dependencies (including boto3/botocore) via `pip install -t`, NOT a Docker-dependent
# aws_lambda_python_alpha.PythonFunction.
#
# WHY pip install -t INSTEAD OF DOCKER: the managed Python 3.12 Lambda runtime ships an
# OLDER boto3/botocore that predates the `bedrock-agentcore` service model both Lambdas
# depend on (connect_memory.writer / connect_memory.reader call bedrock-agentcore's
# CreateEvent / RetrieveMemoryRecords). A Docker-bundled PythonFunction would work too,
# but requires a running Docker daemon at both `cdk synth` and CI time - a heavier, less
# portable prerequisite than plain pip. `pip install -t` has no such dependency.
#
# Each Lambda's package layout, after this script runs:
#   dist/<lambda>/
#     lambda_function.py         (from lambdas/<lambda>/lambda_function.py)
#     connect_memory/            (shared modules, from src/connect_memory)
#     boto3/, botocore/, ...      (vendored dependencies, from lambdas/<lambda>/requirements.txt)
#
# lambdas/<lambda>/requirements.txt pins every package with hashes (generated from
# requirements.in by `make lock`), and pip installs it with --require-hashes, so every
# build ships the same, verified versions.
#
# Run from the repo root: ./scripts/bundle_lambdas.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="$HERE/dist"

# Only the writer and reader Lambdas are deployed by this CDK app.
LAMBDAS=(writer reader)

echo "==> cleaning $DIST_DIR"
rm -rf "$DIST_DIR"
mkdir -p "$DIST_DIR"

for name in "${LAMBDAS[@]}"; do
  SRC_DIR="$HERE/lambdas/$name"
  OUT_DIR="$DIST_DIR/$name"
  echo "==> bundling $name -> $OUT_DIR"
  mkdir -p "$OUT_DIR"

  cp "$SRC_DIR/lambda_function.py" "$OUT_DIR/"

  # Shared modules, copied (not symlinked) so the deployment package is self-contained.
  cp -R "$HERE/src/connect_memory" "$OUT_DIR/connect_memory"
  find "$OUT_DIR/connect_memory" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

  if [[ -f "$SRC_DIR/requirements.txt" ]]; then
    echo "    vendoring dependencies from $SRC_DIR/requirements.txt"
    pip3 install \
      --requirement "$SRC_DIR/requirements.txt" \
      --target "$OUT_DIR" \
      --platform manylinux2014_x86_64 \
      --implementation cp \
      --python-version 3.12 \
      --only-binary=:all: \
      --require-hashes \
      --no-deps \
      --quiet
  fi

  # Strip anything pip left behind that Lambda never needs and that only bloats the
  # deployment package (dist-info/tests dirs, compiled bytecode caches).
  find "$OUT_DIR" -name "*.dist-info" -type d -exec rm -rf {} + 2>/dev/null || true
  find "$OUT_DIR" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

  echo "    done: $(du -sh "$OUT_DIR" | cut -f1)"
done

echo "==> all Lambda bundles ready under $DIST_DIR"
