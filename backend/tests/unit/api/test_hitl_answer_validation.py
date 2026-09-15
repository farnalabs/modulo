"""Unit tests for FAR-860 shared HITL answer validation.

Covers ``validate_hitl_answer`` — the single source of truth used by both the
REST route (``_validate_choice_answer``) and the MCP tool call
(``_validate_mcp_choice_answer``). Every contract branch is exercised:
missing/None answer, unresolvable config (fail-open), no contract (approval),
kind mismatch, choice option membership, approval option rejection, and an
unsupported kind.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from modulo.api.hitl_answer_validation import (
    AnswerValidationError,
    validate_hitl_answer,
)

_RUN_ID = uuid.uuid4()
_ORG_ID = uuid.uuid4()
_GATE_ID = "hitl_gate_src_tgt"
_SESSION = AsyncMock()


async def _validate(answer: dict[str, Any] | None, config: dict[str, Any] | None) -> dict[str, Any] | None:
    """Run ``validate_hitl_answer`` with ``resolve_hitl_gate_config`` stubbed."""
    with patch(
        "modulo.api.hitl_answer_validation.resolve_hitl_gate_config",
        new=AsyncMock(return_value=config),
    ):
        return await validate_hitl_answer(
            _SESSION,
            run_id=_RUN_ID,
            gate_id=_GATE_ID,
            org_id=_ORG_ID,
            answer=answer,
        )


async def test_none_answer_returns_none():
    assert await _validate(None, {"response_contract": {"kind": "approval"}}) is None


@pytest.mark.parametrize("kind", [1, "", None, [], {"nested": "dict"}])
async def test_missing_or_non_string_kind_rejected(kind):
    with pytest.raises(AnswerValidationError, match="non-empty 'kind' string"):
        await _validate({"kind": kind}, None)


async def test_unresolvable_config_fails_open():
    answer = {"kind": "choice", "option_id": "yes"}
    assert await _validate(answer, None) == answer


async def test_no_contract_approval_passes():
    answer = {"kind": "approval"}
    assert await _validate(answer, {}) == answer


async def test_no_contract_non_approval_rejected():
    with pytest.raises(AnswerValidationError, match="gate has no response_contract"):
        await _validate({"kind": "choice", "option_id": "yes"}, {})


async def test_no_contract_rejects_option_id():
    with pytest.raises(AnswerValidationError, match="option_id is not permitted on an approval gate"):
        await _validate({"kind": "approval", "option_id": "yes"}, {"response_contract": None})


async def test_kind_mismatch_rejected():
    config = {"response_contract": {"kind": "choice", "options": [{"id": "yes"}]}}
    with pytest.raises(AnswerValidationError, match="does not match gate response_contract kind"):
        await _validate({"kind": "approval"}, config)


async def test_choice_missing_option_id_rejected():
    config = {"response_contract": {"kind": "choice", "options": [{"id": "yes"}]}}
    with pytest.raises(AnswerValidationError, match="non-empty 'option_id' string"):
        await _validate({"kind": "choice"}, config)


async def test_choice_non_list_options_rejected():
    config = {"response_contract": {"kind": "choice", "options": "yes,no"}}
    with pytest.raises(AnswerValidationError, match="has no options"):
        await _validate({"kind": "choice", "option_id": "yes"}, config)


async def test_choice_unknown_option_id_rejected():
    config = {
        "response_contract": {
            "kind": "choice",
            "options": [{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}],
        }
    }
    with pytest.raises(AnswerValidationError, match="is not a valid option"):
        await _validate({"kind": "choice", "option_id": "maybe"}, config)


async def test_choice_valid_option_passes_ignoring_non_dict_entries():
    config = {
        "response_contract": {
            "kind": "choice",
            "options": [{"id": "yes", "label": "Yes"}, "malformed", {"label": "no id"}],
        }
    }
    answer = {"kind": "choice", "option_id": "yes"}
    assert await _validate(answer, config) == answer


async def test_approval_rejects_option_id():
    config = {"response_contract": {"kind": "approval"}}
    with pytest.raises(AnswerValidationError, match="option_id is not permitted on an approval gate"):
        await _validate({"kind": "approval", "option_id": "yes"}, config)


async def test_unsupported_contract_kind_rejected():
    config = {"response_contract": {"kind": "ranking"}}
    with pytest.raises(AnswerValidationError, match="unsupported answer kind"):
        await _validate({"kind": "ranking"}, config)


async def test_mcp_adapter_keeps_answer_with_error_key_as_answer():
    """Regression: an answer dict that itself carries an ``error`` key must not
    be mistaken for the MCP error sentinel — the old key-sniffing silently
    skipped ``mgr.approve``. The adapter returns ``(error, answer)``."""
    from modulo.api.mcp_server import _validate_mcp_choice_answer

    answer = {"kind": "choice", "option_id": "yes", "error": "a legitimate answer field"}
    config = {"response_contract": {"kind": "choice", "options": [{"id": "yes", "label": "Yes"}]}}
    with patch(
        "modulo.api.hitl_answer_validation.resolve_hitl_gate_config",
        new=AsyncMock(return_value=config),
    ):
        error, validated = await _validate_mcp_choice_answer(AsyncMock(), _RUN_ID, _GATE_ID, _ORG_ID, answer)
    assert error is None
    assert validated == answer


async def test_mcp_adapter_returns_error_tuple_on_invalid_answer():
    from modulo.api.mcp_server import _validate_mcp_choice_answer

    config = {"response_contract": {"kind": "choice", "options": [{"id": "yes", "label": "Yes"}]}}
    with patch(
        "modulo.api.hitl_answer_validation.resolve_hitl_gate_config",
        new=AsyncMock(return_value=config),
    ):
        error, validated = await _validate_mcp_choice_answer(
            AsyncMock(), _RUN_ID, _GATE_ID, _ORG_ID, {"kind": "choice", "option_id": "maybe"}
        )
    assert error is not None
    assert error["error"] == "invalid_answer"
    assert validated is None
