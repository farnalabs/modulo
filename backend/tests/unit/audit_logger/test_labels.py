"""Unit tests for audit actor/summary label composition (FAR-728).

The audit chain hashes the serialized payload at write time, so actor labels
and summaries must be composed at the emit site and ride inside the payload.
These tests pin the pure composers: the trigger-started / user-started /
system actor resolution cases and the descriptive ``run_started`` summary
format.
"""

import uuid

from modulo.core.audit_logger.labels import (
    SYSTEM_ACTOR,
    compose_run_started_summary,
    compose_trigger_actor,
    resolve_run_actor,
    short_id,
)

# ---------------------------------------------------------------------------
# short_id
# ---------------------------------------------------------------------------


def test_short_id_truncates_uuid_to_8_chars():
    value = uuid.uuid4()
    assert short_id(value) == str(value)[:8]
    assert len(short_id(value)) == 8


def test_short_id_accepts_plain_strings():
    assert short_id("abcdefgh12345678") == "abcdefgh"


def test_short_id_none_is_none():
    assert short_id(None) is None


# ---------------------------------------------------------------------------
# compose_trigger_actor
# ---------------------------------------------------------------------------


def test_compose_trigger_actor_includes_type_and_short_id():
    trigger_id = uuid.uuid4()
    assert compose_trigger_actor("cron", trigger_id) == f"cron trigger ({str(trigger_id)[:8]})"


def test_compose_trigger_actor_without_id_omits_parenthetical():
    assert compose_trigger_actor("webhook", None) == "webhook trigger"


def test_compose_trigger_actor_without_type_falls_back_to_system():
    assert compose_trigger_actor(None, None) == SYSTEM_ACTOR


# ---------------------------------------------------------------------------
# resolve_run_actor — the three semantic actor cases
# ---------------------------------------------------------------------------


def test_resolve_run_actor_user_started_resolves_to_acting_user():
    account_id = uuid.uuid4()
    actor_user_id, actor_label = resolve_run_actor(trigger_type="manual", trigger_id=None, account_id=account_id)
    assert actor_user_id == account_id
    assert actor_label is None


def test_resolve_run_actor_trigger_started_resolves_to_trigger_identity():
    trigger_id = uuid.uuid4()
    actor_user_id, actor_label = resolve_run_actor(trigger_type="cron", trigger_id=trigger_id, account_id=None)
    assert actor_user_id is None
    assert actor_label == f"cron trigger ({str(trigger_id)[:8]})"


def test_resolve_run_actor_trigger_started_prefers_trigger_over_owner_account():
    """A trigger-started run carries the trigger identity even when the run row
    also holds the trigger owner's account id."""
    trigger_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    actor_user_id, actor_label = resolve_run_actor(trigger_type="webhook", trigger_id=trigger_id, account_id=owner_id)
    assert actor_user_id is None
    assert actor_label == f"webhook trigger ({str(trigger_id)[:8]})"


def test_resolve_run_actor_manual_without_account_resolves_to_system():
    actor_user_id, actor_label = resolve_run_actor(trigger_type="manual", trigger_id=None, account_id=None)
    assert actor_user_id is None
    assert actor_label == SYSTEM_ACTOR


def test_resolve_run_actor_unidentifiable_resolves_to_system():
    actor_user_id, actor_label = resolve_run_actor(trigger_type=None, trigger_id=None, account_id=None)
    assert actor_user_id is None
    assert actor_label == SYSTEM_ACTOR


# ---------------------------------------------------------------------------
# compose_run_started_summary
# ---------------------------------------------------------------------------


def test_compose_run_started_summary_full_form():
    pipeline_id = uuid.uuid4()
    summary = compose_run_started_summary("PR Reviewer Agent", pipeline_id, "webhook")
    assert summary == f'Pipeline "PR Reviewer Agent" ({str(pipeline_id)[:8]}) run triggered by webhook'


def test_compose_run_started_summary_without_pipeline_name():
    pipeline_id = uuid.uuid4()
    summary = compose_run_started_summary(None, pipeline_id, "cron")
    assert summary == f"Pipeline ({str(pipeline_id)[:8]}) run triggered by cron"


def test_compose_run_started_summary_manual_reads_user_request():
    pipeline_id = uuid.uuid4()
    summary = compose_run_started_summary("Deploy", pipeline_id, "manual")
    assert summary == f'Pipeline "Deploy" ({str(pipeline_id)[:8]}) run triggered by user request'


def test_compose_run_started_summary_without_trigger_type_reads_unknown():
    pipeline_id = uuid.uuid4()
    summary = compose_run_started_summary("Deploy", pipeline_id, None)
    assert summary == f'Pipeline "Deploy" ({str(pipeline_id)[:8]}) run triggered by unknown'
