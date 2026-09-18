"""Unit tests for FAR-860 HITL response contract Pydantic models.

Covers ``HitlResponseOption``, ``HitlResponseContract``, and ``HitlGateConfig``
with ``response_contract`` field validation.
"""

import pytest
from pydantic import ValidationError

from modulo.api.routes.pipelines import HitlGateConfig, HitlResponseContract, HitlResponseOption
from modulo.core.graph_validator import HITL_DESCRIPTION_MIN_LENGTH

_VALID_DESCRIPTION = "A" * HITL_DESCRIPTION_MIN_LENGTH


class TestHitlResponseOption:
    def test_valid_option(self):
        opt = HitlResponseOption(id="approve", label="Approve")
        assert opt.id == "approve"
        assert opt.label == "Approve"
        assert opt.description is None

    def test_option_with_description(self):
        opt = HitlResponseOption(id="revise", label="Request revision", description="Send back for changes")
        assert opt.description == "Send back for changes"

    def test_empty_id_rejected(self):
        with pytest.raises(ValidationError):
            HitlResponseOption(id="", label="Approve")

    def test_empty_label_rejected(self):
        with pytest.raises(ValidationError):
            HitlResponseOption(id="approve", label="")


class TestHitlResponseContract:
    def test_approval_kind(self):
        contract = HitlResponseContract(kind="approval")
        assert contract.kind == "approval"
        assert contract.options is None

    def test_choice_kind_with_options(self):
        contract = HitlResponseContract(
            kind="choice",
            options=[
                HitlResponseOption(id="yes", label="Yes"),
                HitlResponseOption(id="no", label="No"),
            ],
        )
        assert contract.kind == "choice"
        assert len(contract.options) == 2

    def test_choice_without_options_rejected(self):
        with pytest.raises(ValidationError, match="requires a non-empty options list"):
            HitlResponseContract(kind="choice")

    def test_choice_with_empty_options_rejected(self):
        with pytest.raises(ValidationError, match="requires a non-empty options list"):
            HitlResponseContract(kind="choice", options=[])

    def test_duplicate_option_ids_rejected(self):
        with pytest.raises(ValidationError, match="option ids must be unique"):
            HitlResponseContract(
                kind="choice",
                options=[
                    HitlResponseOption(id="yes", label="Yes"),
                    HitlResponseOption(id="yes", label="Also yes"),
                ],
            )

    def test_unknown_kind_rejected(self):
        with pytest.raises(ValidationError):
            HitlResponseContract(kind="unknown")


class TestHitlGateConfig:
    def _base_config(self) -> dict:
        return {
            "label": "Review",
            "description": _VALID_DESCRIPTION,
            "claim_expiry_minutes": 15,
        }

    def test_no_response_contract(self):
        cfg = HitlGateConfig(**self._base_config())
        assert cfg.response_contract is None

    def test_approval_response_contract(self):
        cfg = HitlGateConfig(
            **self._base_config(),
            response_contract=HitlResponseContract(kind="approval"),
        )
        assert cfg.response_contract.kind == "approval"

    def test_choice_response_contract(self):
        cfg = HitlGateConfig(
            **self._base_config(),
            response_contract=HitlResponseContract(
                kind="choice",
                options=[
                    HitlResponseOption(id="go", label="Go ahead"),
                    HitlResponseOption(id="stop", label="Stop"),
                ],
            ),
        )
        assert cfg.response_contract.kind == "choice"
        assert len(cfg.response_contract.options) == 2
