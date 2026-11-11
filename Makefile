.PHONY: bundle lock synth deploy destroy test preflight

# Vendor boto3/botocore into each Lambda's deployment package (pip install -t, NOT Docker -
# see scripts/bundle_lambdas.sh's docstring for rationale).
# Required before `synth`/`deploy` - the stacks raise a RuntimeError at construction time
# if dist/writer or dist/reader don't exist yet.
bundle:
	./scripts/bundle_lambdas.sh

# Regenerate the hash-locked Lambda requirements after changing a requirements.in file.
# Requires pip-tools (pip install pip-tools).
lock:
	cd lambdas/writer && CUSTOM_COMPILE_COMMAND="make lock" pip-compile --quiet --generate-hashes --allow-unsafe --strip-extras --no-emit-index-url --output-file requirements.txt requirements.in
	cd lambdas/reader && CUSTOM_COMPILE_COMMAND="make lock" pip-compile --quiet --generate-hashes --allow-unsafe --strip-extras --no-emit-index-url --output-file requirements.txt requirements.in

# `cdk synth` fails fast and credential-free on a missing/placeholder/malformed cdk.json
# context value (see app.py's validation) - this target does not depend on `bundle` so
# CI can exercise that fast-fail path without a network-dependent pip install first, but
# a real synth against non-placeholder context does need `make bundle` run first.
synth:
	cdk synth

# `scripts/preflight.py` is a separate, AWS-credentialed validation step (log group
# exists, subscription-filter slot free, memory ACTIVE, Wisdom assistant resolves, CDK
# bootstrap present) - run it before `deploy`, not as part of this target, so a failing
# check is visible on its own rather than folded into `cdk deploy`'s own output.
deploy:
	cdk deploy --all

destroy:
	cdk destroy --all

# Runs the full pytest suite (unit tests and CDK stack tests).
# tests/conftest.py's minimal_lambda_bundles fixture creates a hermetic stand-in dist/
# bundle for the CDK tests, so `make test` does NOT require `make bundle` to have been
# run first - only a real `synth`/`deploy` against this repo's own dist/ output does.
test:
	python3 -m pytest

# Pass extra args via PREFLIGHT_ARGS, e.g.:
#   make preflight PREFLIGHT_ARGS="--profile my-aws-profile"
preflight:
	python3 scripts/preflight.py $(PREFLIGHT_ARGS)
