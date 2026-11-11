# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the reader Lambda handler (Connect flow module event shape).

Loaded via ``importlib.util.spec_from_file_location`` under an explicit module name
(``memory_reader_lambda_function``), the same convention ``tests/test_writer_lambda.py``
uses, which avoids module name collisions in ``sys.modules`` when multiple Lambda handler
files share the same ``lambda_function`` filename.

Mocks ``connect_memory.reader``'s public functions and asserts the handler's
Connect-specific glue (event shape in, response shape out) rather than re-testing
retrieval logic already covered by ``tests/test_reader.py``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

import pytest

READER_DIR = Path(__file__).parent.parent / "lambdas" / "reader"
MEMORY_ID = "example_memory-AbCdEf1234"

from connect_memory.reader import MemoryContext  # noqa: E402

FOUND = MemoryContext(actor_id="p", available=True, records=("a fact", "another fact"))
NOT_FOUND = MemoryContext(actor_id=None, available=False, skip_reason="no_profile_id")


@pytest.fixture()
def handler_module(monkeypatch) -> ModuleType:
    """Import reader/lambda_function.py fresh, with boto3.client faked out (mirrors
    tests/test_writer_lambda.py's fixture for the same module-level-client-construction
    pattern)."""
    monkeypatch.setenv("MEMORY_ID", MEMORY_ID)

    import boto3  # the real package; only .client is monkeypatched below

    monkeypatch.setattr(boto3, "client", lambda name, *a, **k: object())

    module_name = "memory_reader_lambda_function"
    sys.modules.pop(module_name, None)  # in case a previous test left it cached
    spec = importlib.util.spec_from_file_location(module_name, READER_DIR / "lambda_function.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(module_name, None)


ASSISTANT_ID = "11111111-2222-3333-4444-111122223333"
SESSION_ID = "5e551011-0000-4000-8000-000000000001"
SESSION_ARN = f"arn:aws:wisdom:eu-central-1:123456789012:session/{ASSISTANT_ID}/{SESSION_ID}"


def _flow_event(
    profile_id: str | None,
    *,
    wisdom_assistant_arn: str | None = None,
    wisdom_session_arn: str | None = None,
) -> dict[str, Any]:
    """A Connect Contact Flow "Invoke AWS Lambda function" module event, trimmed to the
    fields this handler reads. Real events carry many more Details.ContactData fields;
    only Parameters.CustomerProfileId/WisdomAssistantArn/WisdomSessionArn are read."""
    parameters = {}
    if profile_id is not None:
        parameters["CustomerProfileId"] = profile_id
    if wisdom_assistant_arn is not None:
        parameters["WisdomAssistantArn"] = wisdom_assistant_arn
    if wisdom_session_arn is not None:
        parameters["WisdomSessionArn"] = wisdom_session_arn
    return {"Details": {"Parameters": parameters, "ContactData": {}}}


def test_extracts_profile_id_from_details_parameters(handler_module):
    with patch.object(handler_module, "retrieve_memory_context") as mock_retrieve, \
         patch.object(handler_module, "format_memory_context", return_value=""):
        handler_module.lambda_handler(_flow_event("some-profile-id"), context=None)
    assert mock_retrieve.call_args.args[0] == "some-profile-id"


def test_missing_parameters_block_passes_none_profile_id(handler_module):
    event = {"Details": {"ContactData": {}}}  # no "Parameters" key at all
    with patch.object(handler_module, "retrieve_memory_context") as mock_retrieve, \
         patch.object(handler_module, "format_memory_context", return_value=""):
        handler_module.lambda_handler(event, context=None)
    assert mock_retrieve.call_args.args[0] is None


def test_response_is_a_flat_string_map_of_flags_without_memory_text(handler_module):
    with patch.object(handler_module, "retrieve_memory_context", return_value=FOUND), \
         patch.object(handler_module, "format_memory_context", return_value="- a fact"):
        response = handler_module.lambda_handler(_flow_event("p"), context=None)
    assert response == {
        "memoryAvailable": "true",
        "memoryRecordCount": "2",
        "memoryDelivered": "false",  # no assistant/session parameters in this event
    }
    assert all(isinstance(v, str) for v in response.values())
    assert "a fact" not in str(response), "memory text must never be returned to the flow"


def test_no_memory_available_returns_false_flags(handler_module):
    with patch.object(handler_module, "retrieve_memory_context", return_value=NOT_FOUND), \
         patch.object(handler_module, "format_memory_context", return_value=""):
        response = handler_module.lambda_handler(_flow_event(None), context=None)
    assert response == {
        "memoryAvailable": "false",
        "memoryRecordCount": "0",
        "memoryDelivered": "false",
    }


def test_passes_memory_id_and_configured_limits_through(handler_module):
    with patch.object(handler_module, "retrieve_memory_context") as mock_retrieve, \
         patch.object(handler_module, "format_memory_context", return_value=""):
        handler_module.lambda_handler(_flow_event("p"), context=None)
    _, kwargs = mock_retrieve.call_args
    assert kwargs["memory_id"] == MEMORY_ID
    assert kwargs["max_results_per_namespace"] == handler_module.MAX_RESULTS_PER_NAMESPACE
    assert kwargs["timeout_seconds"] == handler_module.TIMEOUT_SECONDS


def test_does_not_swallow_an_unexpected_exception_from_retrieve_memory_context(handler_module):
    # retrieve_memory_context is documented to never raise (tests/test_reader.py pins
    # that). This handler deliberately has NO try/except around the call - if that
    # contract were ever violated, the exception should propagate loudly rather than be
    # hidden by a defensive catch here that would mask a real regression in the contract.
    with patch.object(
        handler_module, "retrieve_memory_context", side_effect=RuntimeError("contract violated")
    ):
        with pytest.raises(RuntimeError, match="contract violated"):
            handler_module.lambda_handler(_flow_event("p"), context=None)


# --- UpdateSessionData side effect -------------------------------------


def test_parses_session_id_out_of_a_wisdom_session_arn(handler_module):
    assert handler_module._parse_session_id(SESSION_ARN) == SESSION_ID


@pytest.mark.parametrize(
    "malformed",
    [
        None,
        "",
        "not-an-arn",
        "arn:aws:wisdom:eu-central-1:123456789012:assistant/" + ASSISTANT_ID,  # wrong resource type
        "arn:aws:wisdom:eu-central-1:123456789012:session/" + ASSISTANT_ID,  # missing session segment
    ],
)
def test_parse_session_id_returns_none_without_raising_on_malformed_input(
    handler_module, malformed
):
    assert handler_module._parse_session_id(malformed) is None


def test_calls_update_session_data_with_the_formatted_context_when_memory_is_available(
    handler_module,
):
    event = _flow_event(
        "p", wisdom_assistant_arn=ASSISTANT_ID, wisdom_session_arn=SESSION_ARN
    )
    with patch.object(handler_module, "retrieve_memory_context"), \
         patch.object(handler_module, "format_memory_context", return_value="- a fact"), \
         patch.object(handler_module, "_qconnect") as mock_qconnect:
        handler_module.lambda_handler(event, context=None)

    mock_qconnect.update_session_data.assert_called_once_with(
        assistantId=ASSISTANT_ID,
        sessionId=SESSION_ID,
        data=[{"key": "MemoryContext", "value": {"stringValue": "- a fact"}}],
    )


def test_skips_update_session_data_when_no_memory_context_is_available(handler_module):
    event = _flow_event(
        "p", wisdom_assistant_arn=ASSISTANT_ID, wisdom_session_arn=SESSION_ARN
    )
    with patch.object(handler_module, "retrieve_memory_context"), \
         patch.object(handler_module, "format_memory_context", return_value=""), \
         patch.object(handler_module, "_qconnect") as mock_qconnect:
        handler_module.lambda_handler(event, context=None)

    mock_qconnect.update_session_data.assert_not_called()


def test_skips_update_session_data_when_wisdom_assistant_arn_parameter_is_missing(
    handler_module,
):
    # No WisdomAssistantArn parameter at all - flow wasn't configured to pass it.
    event = _flow_event("p", wisdom_session_arn=SESSION_ARN)
    with patch.object(handler_module, "retrieve_memory_context"), \
         patch.object(handler_module, "format_memory_context", return_value="- a fact"), \
         patch.object(handler_module, "_qconnect") as mock_qconnect:
        handler_module.lambda_handler(event, context=None)

    mock_qconnect.update_session_data.assert_not_called()


def test_skips_update_session_data_when_wisdom_session_arn_does_not_parse(handler_module):
    event = _flow_event(
        "p", wisdom_assistant_arn=ASSISTANT_ID, wisdom_session_arn="garbage"
    )
    with patch.object(handler_module, "retrieve_memory_context"), \
         patch.object(handler_module, "format_memory_context", return_value="- a fact"), \
         patch.object(handler_module, "_qconnect") as mock_qconnect:
        handler_module.lambda_handler(event, context=None)

    mock_qconnect.update_session_data.assert_not_called()


def test_a_failing_update_session_data_call_does_not_raise_or_change_the_flow_response(
    handler_module,
):
    event = _flow_event(
        "p", wisdom_assistant_arn=ASSISTANT_ID, wisdom_session_arn=SESSION_ARN
    )
    with patch.object(handler_module, "retrieve_memory_context"), \
         patch.object(handler_module, "format_memory_context", return_value="- a fact"), \
         patch.object(handler_module, "_qconnect") as mock_qconnect:
        mock_qconnect.update_session_data.side_effect = RuntimeError("throttled")
        response = handler_module.lambda_handler(event, context=None)

    assert response["memoryDelivered"] == "false"


def test_successful_update_session_data_reports_delivered(handler_module):
    event = _flow_event(
        "p", wisdom_assistant_arn=ASSISTANT_ID, wisdom_session_arn=SESSION_ARN
    )
    with patch.object(handler_module, "retrieve_memory_context", return_value=FOUND), \
         patch.object(handler_module, "format_memory_context", return_value="- a fact"), \
         patch.object(handler_module, "_qconnect"):
        response = handler_module.lambda_handler(event, context=None)

    assert response["memoryDelivered"] == "true"


def test_without_session_parameters_nothing_is_written_to_a_session(handler_module):
    with patch.object(handler_module, "retrieve_memory_context", return_value=NOT_FOUND), \
         patch.object(handler_module, "format_memory_context", return_value=""), \
         patch.object(handler_module, "_qconnect") as mock_qconnect:
        response = handler_module.lambda_handler(_flow_event("p"), context=None)

    assert response["memoryDelivered"] == "false"
    mock_qconnect.update_session_data.assert_not_called()


def test_handler_sets_the_root_log_level_so_info_lines_are_emitted(handler_module):
    import logging

    assert logging.getLogger().level == logging.INFO
