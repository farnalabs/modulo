"""Focused branch coverage for the S3776 decomposition of ``trigger_apply``.

Covers the list branch of ``_resolve_value`` (success and propagated failure)
and the ambiguous-pipeline reason in ``_trigger_block_reason``.
"""

from modulo.cli.apply.models import TriggerEntity
from modulo.cli.apply.trigger_apply import _resolve_value, _trigger_block_reason


def test_resolve_value_resolves_list_items() -> None:
    resolved, failure = _resolve_value(["${env:HOST}", "literal"], {"HOST": "example.test"})

    assert failure is None
    assert resolved == ["example.test", "literal"]


def test_resolve_value_propagates_first_list_failure() -> None:
    resolved, failure = _resolve_value(["ok", "${env:MISSING}", "${env:OTHER}"], {"OTHER": "x"})

    assert resolved is None
    assert failure == "unresolved env ref: ${env:MISSING} is not set"


def test_trigger_block_reason_reports_ambiguous_pipeline() -> None:
    entity = TriggerEntity(pipeline="nightly", name="hook", trigger_type="manual", config_json={})

    reason = _trigger_block_reason(
        entity,
        current_pipelines={},
        declared_pipelines={"nightly"},
        ambiguous_pipelines={"nightly": 2},
        pipeline_block_reasons={},
    )

    assert reason == "pipeline 'nightly': ambiguous name, 2 matches"
