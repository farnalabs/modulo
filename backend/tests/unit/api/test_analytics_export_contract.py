"""Export-contract guard for the raw fact export (FAR-1421).

``run_daily_facts`` grows an enrichment column roughly once per feature, and
the export carries it through TWO independent lists: ``_EXPORT_COLUMNS`` in
``core/analytics/service.py`` (drives the CSV header, the NDJSON scan row and
the serialiser's key set) and ``AnalyticsExportItem`` in
``api/routes/analytics.py`` (the JSON response model — extra keys are silently
dropped by pydantic, so a column present in one list but not the other is
invisible on the other surface).

That duplication has already drifted: ``_EXPORT_COLUMNS`` claims to carry
"all fact columns" and did not. These tests make the contract structural —
a new model column must be exported (or explicitly classified as internal)
and both lists must stay in lockstep, so a future enrichment cannot silently
miss the export again.
"""

from __future__ import annotations

from modulo.api.routes.analytics import AnalyticsExportItem
from modulo.core.analytics.service import EXPORT_COLUMN_NAMES
from modulo.db.models.run_daily_facts import RunDailyFact

# Deliberately NOT an analytics export column: internal plumbing, not run
# data. Everything else on the fact is exportable by contract.
_INTERNAL_FACT_COLUMNS = frozenset({"id", "organisation_id", "updated_at"})

# FAR-1421 claim→dispatch latency provenance — the columns that triggered
# this guard.
_PROVENANCE_COLUMNS = frozenset({"trigger_id", "dispatch_phase", "dispatch_phase_entered_at"})


class TestExportColumnContract:
    def test_every_fact_column_is_exported(self) -> None:
        model_columns = {c.name for c in RunDailyFact.__table__.columns}
        exported = set(EXPORT_COLUMN_NAMES)
        missing = model_columns - _INTERNAL_FACT_COLUMNS - exported
        assert not missing, (
            "run_daily_facts carries columns the raw export does not: "
            f"{sorted(missing)} — add them to _EXPORT_COLUMNS (or to "
            "_INTERNAL_FACT_COLUMNS with a reason if they are plumbing)"
        )

    def test_json_export_item_covers_every_export_column(self) -> None:
        # The JSON surface validates through AnalyticsExportItem, so a column
        # in _EXPORT_COLUMNS but not in the model is dropped on JSON while
        # appearing in CSV/NDJSON — the same value, two different shapes.
        assert set(AnalyticsExportItem.model_fields) == set(EXPORT_COLUMN_NAMES)

    def test_claim_dispatch_provenance_is_exported(self) -> None:
        # A consumer must be able to export trigger_id next to trigger_type
        # and read the fields the avg_dispatch_latency_ms metric is bucketed
        # from.
        exported = set(EXPORT_COLUMN_NAMES)
        assert exported >= _PROVENANCE_COLUMNS
        assert set(AnalyticsExportItem.model_fields) >= _PROVENANCE_COLUMNS
