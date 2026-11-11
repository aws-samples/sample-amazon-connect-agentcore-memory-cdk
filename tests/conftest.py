# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared pytest fixtures for the CDK unit/snapshot tests.

``stacks.memory_writer_stack``/``stacks.memory_reader_stack`` both require their Lambda
bundle directory (``dist/writer``, ``dist/reader``) to exist on disk before ``cdk synth``
can run - CDK computes an asset hash from the directory's real contents. The full bundling
step (``scripts/bundle_lambdas.sh``) vendors boto3/botocore via `pip install -t`, which
needs network access and is far more than a CDK unit test needs to verify.

This fixture creates a MINIMAL stand-in bundle (just ``lambda_function.py`` +
``connect_memory``, no vendored dependencies) so `cdk synth`-based tests can run hermetically,
without requiring the real bundling step or network access. It intentionally does not
assert anything about bundle CONTENTS - that is `scripts/bundle_lambdas.sh`'s job, exercised
separately (see the CDK test module's own docstring).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session", autouse=True)
def minimal_lambda_bundles():
    dist_dir = REPO_ROOT / "dist"
    created_dirs = []

    for name in ("writer", "reader"):
        out_dir = dist_dir / name
        if out_dir.exists():
            continue
        out_dir.mkdir(parents=True)
        created_dirs.append(out_dir)
        shutil.copy(REPO_ROOT / "lambdas" / name / "lambda_function.py", out_dir / "lambda_function.py")
        shutil.copytree(REPO_ROOT / "src" / "connect_memory", out_dir / "connect_memory")

    yield

    for out_dir in created_dirs:
        shutil.rmtree(out_dir, ignore_errors=True)
    if dist_dir.exists() and not any(dist_dir.iterdir()):
        dist_dir.rmdir()
