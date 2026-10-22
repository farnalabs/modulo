"""Tests for the shipped compliance control-mapping pack (FAR-2036).

The pack is an audit claim, so these tests pin the two things that make it
trustworthy: the shipped content is complete enough to be useful, and the
loader's honesty rules actually reject a dishonest edit (a satisfied clause
resting on customer material, a framework that claims everything, a dropped
SOC 2 clause).
"""

from __future__ import annotations

from typing import Any

import pytest

import modulo.compliance.pack as pack_module
from modulo.compliance.artifacts import (
    ARTIFACT_CUSTOMER_RECORDS,
    COVERAGE_CUSTOMER_RESPONSIBILITY,
    COVERAGE_GAP,
    COVERAGE_SATISFIED,
    COVERAGE_SUPPORTED,
    INDEXED_ARTIFACT_TYPES,
    NON_SELF_COVERAGE_VALUES,
    PROVENANCE_CUSTOMER_SUPPLIED,
    PROVENANCE_OBSERVED,
)
from modulo.compliance.pack import (
    FRAMEWORK_IDS,
    PACK_VERSION_PATTERN,
    SOC2_REQUIRED_CLAUSES,
    Framework,
    MappingPack,
    MappingPackError,
    MappingRow,
    load_mapping_pack,
    validate_frameworks,
    validate_mapping_rows,
)

_REQUIRED_ROW_TEXT_FIELDS = (
    "clause",
    "clause_intent",
    "modulo_control",
    "customer_location",
    "responsibility",
)

#: The coverage table published in docs/security/compliance-control-mapping.md.
#: Changing an assertion in the pack means updating this table AND the doc in the
#: same change - that is the point of pinning it.
_EXPECTED_COVERAGE: dict[str, dict[str, int]] = {
    "soc2": {
        COVERAGE_SATISFIED: 3,
        COVERAGE_SUPPORTED: 9,
        COVERAGE_CUSTOMER_RESPONSIBILITY: 4,
        COVERAGE_GAP: 0,
    },
    "iso_42001": {
        COVERAGE_SATISFIED: 1,
        COVERAGE_SUPPORTED: 7,
        COVERAGE_CUSTOMER_RESPONSIBILITY: 2,
        COVERAGE_GAP: 0,
    },
    "eu_ai_act": {
        COVERAGE_SATISFIED: 1,
        COVERAGE_SUPPORTED: 4,
        COVERAGE_CUSTOMER_RESPONSIBILITY: 4,
        COVERAGE_GAP: 0,
    },
}


def _row(**overrides: object) -> MappingRow:
    """A minimal valid SOC 2 row, with *overrides* applied."""
    base: dict[str, object] = {
        "id": "test-row-1",
        "framework": "soc2",
        "clause": "CC7.2",
        "clause_intent": "The entity monitors system components.",
        "modulo_control": "Modulo records events.",
        "artifact_type": "audit_chain",
        "customer_location": "GET /api/v1/admin/audit",
        "coverage": COVERAGE_SUPPORTED,
        "provenance": PROVENANCE_OBSERVED,
        "responsibility": "The customer reviews the events and decides what an incident is.",
    }
    base.update(overrides)
    return MappingRow.model_validate(base)


def _framework(framework_id: str, rows: tuple[MappingRow, ...]) -> Framework:
    return Framework(
        id=framework_id,
        name=f"{framework_id} name",
        source=f"{framework_id} source",
        scope_note=f"{framework_id} scope",
        rows=rows,
    )


class TestShippedPackContent:
    def test_pack_declares_exactly_the_three_required_frameworks(self) -> None:
        pack = load_mapping_pack()

        assert pack.framework_ids() == FRAMEWORK_IDS

    def test_pack_version_is_a_release_identifier(self) -> None:
        pack = load_mapping_pack()

        assert PACK_VERSION_PATTERN.match(pack.pack_version), pack.pack_version
        assert pack.published_at

    def test_every_row_states_a_control_and_a_customer_location(self) -> None:
        pack = load_mapping_pack()

        missing = [
            (row.id, field)
            for row in pack.rows
            for field in _REQUIRED_ROW_TEXT_FIELDS
            if not str(getattr(row, field)).strip()
        ]
        assert not missing, missing

    def test_row_ids_are_unique_across_the_pack(self) -> None:
        pack = load_mapping_pack()

        ids = [row.id for row in pack.rows]

        assert len(ids) == len(set(ids))

    def test_every_indexed_artifact_type_is_mapped_to_a_clause(self) -> None:
        pack = load_mapping_pack()

        unmapped = [
            artifact_type for artifact_type in INDEXED_ARTIFACT_TYPES if not pack.rows_for_artifact(artifact_type)
        ]
        assert not unmapped, unmapped

    def test_each_framework_names_something_modulo_does_not_satisfy(self) -> None:
        pack = load_mapping_pack()

        offenders = [
            framework_id
            for framework_id, framework in pack.frameworks.items()
            if not any(row.coverage in NON_SELF_COVERAGE_VALUES for row in framework.rows)
        ]
        assert not offenders, offenders

    def test_soc2_keeps_the_required_clause_set(self) -> None:
        pack = load_mapping_pack()

        clauses = {row.clause for row in pack.frameworks["soc2"].rows}

        assert clauses >= SOC2_REQUIRED_CLAUSES

    def test_iso_and_eu_cover_change_oversight_and_traceability(self) -> None:
        pack = load_mapping_pack()

        iso_clauses = {row.clause for row in pack.frameworks["iso_42001"].rows}
        eu_clauses = {row.clause for row in pack.frameworks["eu_ai_act"].rows}

        assert {"8.1", "9.1", "A.6.2.4", "A.6.2.8"} <= iso_clauses
        assert {"Article 12", "Article 14", "Article 15"} <= eu_clauses

    def test_coverage_counts_match_the_published_table(self) -> None:
        pack = load_mapping_pack()

        assert pack.coverage_counts() == _EXPECTED_COVERAGE

    def test_coverage_is_attributed_per_framework_and_never_collapsed(self) -> None:
        pack = load_mapping_pack()

        for framework_id, framework in pack.frameworks.items():
            counts = pack.coverage_counts()[framework_id]
            # ``counts`` summing to exactly the row count proves no row was
            # silently dropped or double-counted when its attribution was
            # made. (Asserting the dict's KEY SHAPE here would only restate
            # production's ``dict.fromkeys(COVERAGE_VALUES, 0)`` - it can
            # never fail, so it proves nothing.)
            assert sum(counts.values()) == len(framework.rows)

        # A single "compliant" verdict would mean every row in every framework
        # was marked satisfied. That must never be true of this pack.
        total_rows = len(pack.rows)
        total_satisfied = sum(counts[COVERAGE_SATISFIED] for counts in pack.coverage_counts().values())
        assert total_satisfied < total_rows

    def test_every_framework_owns_a_clause_no_modulo_artifact_can_evidence(self) -> None:
        """A framework must keep at least one clause only the customer can evidence.

        The row class that claims "Modulo provides nothing" is the one whose
        evidence is ``customer_records`` - material Modulo never sees. The
        honesty that used to rest on "there is at least one ``gap`` row
        somewhere" is enforced here as label discipline instead, because a
        ``grep`` for zero ``gap`` rows can be made green by relabelling a
        genuine gap:

        * every ``customer_records`` row must be attributed
          ``customer-responsibility`` (never ``gap``, and never a coverage
          value that claims Modulo provides the evidence) and must state, in a
          sentence, what remains the customer's responsibility;
        * every framework must still contain at least one such row, so a
          framework cannot be relabelled into universal self-coverage.
        """
        pack = load_mapping_pack()
        offenders: list[str] = []
        for framework in pack.frameworks.values():
            customer_rows = [row for row in framework.rows if row.artifact_type == ARTIFACT_CUSTOMER_RECORDS]
            # A framework whose every clause is evidenced by an artefact
            # Modulo itself produces is a framework that has stopped admitting
            # anything. One customer-owned row is the floor.
            if not customer_rows:
                offenders.append(f"{framework.id}: no customer_records row")
                continue
            for row in customer_rows:
                if row.coverage != COVERAGE_CUSTOMER_RESPONSIBILITY:
                    offenders.append(f"{row.id}: customer_records row labelled {row.coverage!r}")
                if not row.responsibility.strip():
                    offenders.append(f"{row.id}: customer_records row states no responsibility")
        assert not offenders, offenders

        # And the pack as a whole must never read as universally satisfied.
        unqualified = [
            framework_id
            for framework_id, framework in pack.frameworks.items()
            if not any(row.coverage in NON_SELF_COVERAGE_VALUES for row in framework.rows)
        ]
        assert not unqualified, unqualified

    def test_satisfied_rows_never_rest_on_customer_material(self) -> None:
        pack = load_mapping_pack()

        offenders = [
            row.id
            for row in pack.rows
            if row.coverage == COVERAGE_SATISFIED and row.provenance == PROVENANCE_CUSTOMER_SUPPLIED
        ]
        assert not offenders, offenders

    def test_customer_responsibility_rows_are_customer_supplied(self) -> None:
        pack = load_mapping_pack()

        offenders = [
            row.id
            for row in pack.rows
            if row.coverage == COVERAGE_CUSTOMER_RESPONSIBILITY and row.provenance != PROVENANCE_CUSTOMER_SUPPLIED
        ]
        assert not offenders, offenders


class TestContentHash:
    def test_hash_is_stable_across_loads(self) -> None:
        first = load_mapping_pack()
        load_mapping_pack.cache_clear()
        second = load_mapping_pack()

        # The two loads are genuinely independent rebuilds: ``load_mapping_pack``
        # is ``lru_cache``d, so calling it twice WITHOUT clearing returns the
        # same object and "equality" proves nothing. After a ``cache_clear``
        # both rebuilds must still hash identically.
        assert first is not second
        assert first.content_hash() == second.content_hash()

    def test_hash_changes_when_an_assertion_changes(self) -> None:
        pack = load_mapping_pack()
        target = pack.rows[0].id
        changed_frameworks = {
            framework_id: framework.model_copy(
                update={
                    "rows": tuple(
                        row.model_copy(update={"modulo_control": "a different claim"}) if row.id == target else row
                        for row in framework.rows
                    )
                }
            )
            for framework_id, framework in pack.frameworks.items()
        }
        changed = MappingPack.model_validate({**pack.model_dump(), "frameworks": changed_frameworks})

        assert changed.content_hash() != pack.content_hash()

    def test_hash_changes_when_only_a_notes_field_changes(self) -> None:
        """``notes`` is part of the pack's asserted content.

        The hash must not exclude it: a silent edit to a reviewer-facing note
        is as much an unbumped assertion change as an edit to
        ``modulo_control``.
        """
        pack = load_mapping_pack()
        target = pack.rows[0].id
        changed_frameworks = {
            framework_id: framework.model_copy(
                update={
                    "rows": tuple(
                        row.model_copy(update={"notes": "a reviewer-facing note"}) if row.id == target else row
                        for row in framework.rows
                    )
                }
            )
            for framework_id, framework in pack.frameworks.items()
        }
        changed = MappingPack.model_validate({**pack.model_dump(), "frameworks": changed_frameworks})

        # Precondition: the edit actually landed on the target row's notes.
        assert changed.rows[0].notes == "a reviewer-facing note"
        assert changed.content_hash() != pack.content_hash()


class TestRowValidationRejectsDishonesty:
    def test_accepts_a_valid_row(self) -> None:
        assert validate_mapping_rows((_row(),)) is None

    def test_rejects_a_row_with_an_empty_id(self) -> None:
        with pytest.raises(MappingPackError, match="empty id"):
            validate_mapping_rows((_row(id=""),))

    def test_rejects_a_duplicate_row_id(self) -> None:
        with pytest.raises(MappingPackError, match="duplicate mapping row id"):
            validate_mapping_rows((_row(), _row()))

    def test_rejects_unknown_framework(self) -> None:
        with pytest.raises(MappingPackError, match="unknown framework"):
            validate_mapping_rows((_row(framework="nist-800-53"),))

    def test_rejects_a_missing_responsibility(self) -> None:
        with pytest.raises(MappingPackError, match="responsibility"):
            validate_mapping_rows((_row(responsibility="   "),))

    def test_rejects_a_placeholder_responsibility(self) -> None:
        with pytest.raises(MappingPackError, match="in a sentence"):
            validate_mapping_rows((_row(responsibility="n/a"),))

    def test_rejects_unknown_artifact_type(self) -> None:
        with pytest.raises(MappingPackError, match="unknown artefact type"):
            validate_mapping_rows((_row(artifact_type="vibes"),))

    def test_rejects_unknown_coverage(self) -> None:
        with pytest.raises(MappingPackError, match="unknown coverage"):
            validate_mapping_rows((_row(coverage="probably-fine"),))

    def test_rejects_unknown_provenance(self) -> None:
        with pytest.raises(MappingPackError, match="unknown provenance"):
            validate_mapping_rows((_row(provenance="trust-me"),))

    def test_rejects_a_satisfied_claim_resting_on_customer_material(self) -> None:
        with pytest.raises(MappingPackError, match="cannot be evidenced by customer-supplied"):
            validate_mapping_rows(
                (
                    _row(
                        coverage=COVERAGE_SATISFIED,
                        provenance=PROVENANCE_CUSTOMER_SUPPLIED,
                    ),
                )
            )

    def test_rejects_customer_responsibility_with_observed_evidence(self) -> None:
        with pytest.raises(MappingPackError, match="must be tagged 'customer-supplied'"):
            validate_mapping_rows(
                (
                    _row(
                        coverage=COVERAGE_CUSTOMER_RESPONSIBILITY,
                        provenance=PROVENANCE_OBSERVED,
                    ),
                )
            )

    def test_rejects_supported_claim_on_material_the_instance_cannot_produce(self) -> None:
        with pytest.raises(MappingPackError, match="cannot evidence a satisfied/supported claim"):
            validate_mapping_rows(
                (
                    _row(
                        artifact_type="customer_records",
                        provenance="observed",
                    ),
                )
            )


class TestFrameworkValidation:
    def test_rejects_a_pack_missing_a_framework(self) -> None:
        rows = (_row(),)
        frameworks = {
            "soc2": _framework("soc2", tuple(_row(id=f"r{index}") for index in range(20))),
            "iso_42001": _framework("iso_42001", rows),
        }
        with pytest.raises(MappingPackError, match="pack frameworks must be exactly"):
            validate_frameworks(frameworks)

    def test_rejects_a_framework_whose_key_disagrees_with_its_declared_id(self) -> None:
        frameworks = {
            "soc2": _framework("iso_42001", (_row(),)),
            "iso_42001": _framework("iso_42001", (_row(id="iso-1", framework="iso_42001"),)),
            "eu_ai_act": _framework(
                "eu_ai_act",
                (_row(id="eu-1", framework="eu_ai_act", coverage=COVERAGE_GAP),),
            ),
        }

        with pytest.raises(MappingPackError, match="does not match its declared id"):
            validate_frameworks(frameworks)

    def test_rejects_a_framework_with_no_mapping_rows(self) -> None:
        frameworks = {
            "soc2": _framework("soc2", ()),
            "iso_42001": _framework("iso_42001", (_row(id="iso-1", framework="iso_42001"),)),
            "eu_ai_act": _framework(
                "eu_ai_act",
                (_row(id="eu-1", framework="eu_ai_act", coverage=COVERAGE_GAP),),
            ),
        }

        with pytest.raises(MappingPackError, match="has no mapping rows"):
            validate_frameworks(frameworks)

    def test_rejects_a_row_filed_under_the_wrong_framework(self) -> None:
        frameworks = {
            "soc2": _framework("soc2", (_row(id="misplaced", framework="iso_42001"),)),
            "iso_42001": _framework("iso_42001", (_row(id="iso-1", framework="iso_42001"),)),
            "eu_ai_act": _framework(
                "eu_ai_act",
                (_row(id="eu-1", framework="eu_ai_act", coverage=COVERAGE_GAP),),
            ),
        }

        with pytest.raises(MappingPackError, match="is filed under"):
            validate_frameworks(frameworks)

    def test_rejects_a_framework_that_claims_everything(self) -> None:
        frameworks = {
            "soc2": _framework(
                "soc2",
                tuple(
                    _row(
                        id=f"cc8-{index}",
                        clause=clause,
                        coverage=COVERAGE_SATISFIED,
                    )
                    for index, clause in enumerate(sorted(SOC2_REQUIRED_CLAUSES))
                ),
            ),
            "iso_42001": _framework("iso_42001", (_row(id="iso-1", framework="iso_42001"),)),
            "eu_ai_act": _framework(
                "eu_ai_act",
                (_row(id="eu-1", framework="eu_ai_act", coverage=COVERAGE_GAP),),
            ),
        }
        with pytest.raises(MappingPackError, match="claims every clause as met"):
            validate_frameworks(frameworks)

    def test_rejects_a_soc2_pack_that_drops_a_required_clause(self) -> None:
        frameworks = {
            "soc2": _framework(
                "soc2",
                (
                    _row(
                        id="only-row",
                        clause="CC8.1",
                        coverage=COVERAGE_CUSTOMER_RESPONSIBILITY,
                        provenance=PROVENANCE_CUSTOMER_SUPPLIED,
                    ),
                ),
            ),
            "iso_42001": _framework(
                "iso_42001",
                (
                    _row(
                        id="iso-customer",
                        framework="iso_42001",
                        coverage=COVERAGE_CUSTOMER_RESPONSIBILITY,
                        provenance=PROVENANCE_CUSTOMER_SUPPLIED,
                        artifact_type="customer_records",
                    ),
                    *(
                        _row(
                            id=f"iso-{artifact_type}",
                            framework="iso_42001",
                            artifact_type=artifact_type,
                        )
                        for artifact_type in INDEXED_ARTIFACT_TYPES
                        if artifact_type != "audit_chain"
                    ),
                ),
            ),
            "eu_ai_act": _framework(
                "eu_ai_act",
                (
                    _row(
                        id="eu-1",
                        framework="eu_ai_act",
                        coverage=COVERAGE_CUSTOMER_RESPONSIBILITY,
                        provenance=PROVENANCE_CUSTOMER_SUPPLIED,
                        artifact_type="customer_records",
                    ),
                ),
            ),
        }

        with pytest.raises(MappingPackError, match="missing required clauses"):
            validate_frameworks(frameworks)

    def test_rejects_an_indexed_artefact_type_nobody_maps(self) -> None:
        # Every SOC 2 row uses the audit chain and no other indexed artefact
        # type is referenced anywhere - the pack would be missing evidence the
        # index must account for.
        rows = tuple(
            _row(
                id=f"r{index}",
                clause=clause,
                coverage=COVERAGE_CUSTOMER_RESPONSIBILITY if index == 0 else COVERAGE_SUPPORTED,
                provenance=(PROVENANCE_CUSTOMER_SUPPLIED if index == 0 else PROVENANCE_OBSERVED),
                artifact_type="customer_records" if index == 0 else "audit_chain",
            )
            for index, clause in enumerate(sorted(SOC2_REQUIRED_CLAUSES))
        )
        frameworks = {
            "soc2": _framework("soc2", rows),
            "iso_42001": _framework(
                "iso_42001",
                (
                    _row(
                        id="iso-1",
                        framework="iso_42001",
                        coverage=COVERAGE_CUSTOMER_RESPONSIBILITY,
                        provenance=PROVENANCE_CUSTOMER_SUPPLIED,
                        artifact_type="customer_records",
                    ),
                ),
            ),
            "eu_ai_act": _framework(
                "eu_ai_act",
                (
                    _row(
                        id="eu-1",
                        framework="eu_ai_act",
                        coverage=COVERAGE_CUSTOMER_RESPONSIBILITY,
                        provenance=PROVENANCE_CUSTOMER_SUPPLIED,
                        artifact_type="customer_records",
                    ),
                ),
            ),
        }
        with pytest.raises(MappingPackError, match="does not map these indexed artefact types"):
            validate_frameworks(frameworks)


# ---------------------------------------------------------------------------
# Load path: the honesty rules must actually be WIRED INTO _build_pack
# ---------------------------------------------------------------------------
#
# The tests above call ``validate_mapping_rows`` / ``validate_frameworks``
# directly, so deleting those calls from ``_build_pack`` would leave the suite
# green while a dishonest pack loaded happily. These tests drive documents
# through the real loader instead.


def _raw_row(
    row_id: str,
    clause: str,
    *,
    artifact_type: str,
    coverage: str,
    provenance: str,
) -> dict[str, Any]:
    return {
        "id": row_id,
        "clause": clause,
        "clause_intent": "The clause intent is stated in a sentence.",
        "modulo_control": "Modulo records the fact and produces the evidence.",
        "artifact_type": artifact_type,
        "customer_location": "GET /api/v1/admin/audit",
        "coverage": coverage,
        "provenance": provenance,
        "responsibility": "The customer is responsible for the remaining control.",
    }


def _pack_documents() -> dict[str, dict[str, Any]]:
    """A complete, minimal, VALID pack expressed as the raw YAML documents.

    Valid by construction (each framework names at least one row Modulo does not
    satisfy, every SOC 2 required clause is present, every indexed artefact type
    is referenced) so a test can mutate exactly one field and attribute the
    resulting rejection to that mutation rather than to a broken fixture.
    """
    indexed = list(INDEXED_ARTIFACT_TYPES)
    soc2_rows = [
        _raw_row(
            "soc2-customer",
            min(SOC2_REQUIRED_CLAUSES),
            artifact_type="customer_records",
            coverage=COVERAGE_CUSTOMER_RESPONSIBILITY,
            provenance=PROVENANCE_CUSTOMER_SUPPLIED,
        )
    ]
    soc2_rows.extend(
        _raw_row(
            f"soc2-{index}",
            clause,
            artifact_type=indexed[index % len(indexed)],
            coverage=COVERAGE_SUPPORTED,
            provenance=PROVENANCE_OBSERVED,
        )
        for index, clause in enumerate(sorted(SOC2_REQUIRED_CLAUSES)[1:])
    )
    iso_rows = [
        _raw_row(
            "iso-customer",
            "8.2",
            artifact_type="customer_records",
            coverage=COVERAGE_CUSTOMER_RESPONSIBILITY,
            provenance=PROVENANCE_CUSTOMER_SUPPLIED,
        ),
        _raw_row(
            "iso-1",
            "8.1",
            artifact_type="run_snapshot",
            coverage=COVERAGE_SUPPORTED,
            provenance=PROVENANCE_OBSERVED,
        ),
    ]
    eu_rows = [
        _raw_row(
            "eu-customer",
            "Article 9",
            artifact_type="customer_records",
            coverage=COVERAGE_CUSTOMER_RESPONSIBILITY,
            provenance=PROVENANCE_CUSTOMER_SUPPLIED,
        ),
        _raw_row(
            "eu-1",
            "Article 12",
            artifact_type="audit_chain",
            coverage=COVERAGE_SUPPORTED,
            provenance=PROVENANCE_OBSERVED,
        ),
    ]
    return {
        "pack.yaml": {
            "pack_version": "1.0.0",
            "published_at": "2026-10-21",
            "title": "Test pack",
            "honesty_note": "This pack claims nothing it cannot evidence.",
            "frameworks": [
                {"id": "soc2", "file": "soc2.yaml"},
                {"id": "iso_42001", "file": "iso-42001.yaml"},
                {"id": "eu_ai_act", "file": "eu-ai-act.yaml"},
            ],
        },
        "soc2.yaml": {
            "framework": "soc2",
            "name": "SOC 2",
            "source": "AICPA Trust Services Criteria.",
            "scope_note": "SOC 2 audits the customer's system, not Modulo.",
            "rows": soc2_rows,
        },
        "iso-42001.yaml": {
            "framework": "iso_42001",
            "name": "ISO/IEC 42001",
            "source": "ISO/IEC 42001:2023.",
            "scope_note": "Modulo is a control inside the customer's AIMS.",
            "rows": iso_rows,
        },
        "eu-ai-act.yaml": {
            "framework": "eu_ai_act",
            "name": "EU AI Act",
            "source": "Regulation (EU) 2024/1689.",
            "scope_note": "Obligations fall on the provider and deployer.",
            "rows": eu_rows,
        },
    }


@pytest.fixture
def install_documents(monkeypatch: pytest.MonkeyPatch):
    """Route ``pack._read_yaml`` at an in-memory document set."""

    def _install(documents: dict[str, dict[str, Any]]) -> None:
        monkeypatch.setattr(pack_module, "_read_yaml", lambda filename: documents[filename])

    return _install


class TestBuildPackEnforcesHonesty:
    def test_accepts_the_control_document_set(self, install_documents) -> None:
        """Control: the fixture itself is valid, so the rejects below are causal."""
        install_documents(_pack_documents())

        pack = pack_module._build_pack()

        assert pack.framework_ids() == FRAMEWORK_IDS

    def test_build_pack_rejects_a_satisfied_row_resting_on_customer_material(self, install_documents) -> None:
        documents = _pack_documents()
        dishonest = documents["soc2.yaml"]["rows"][1]
        dishonest["coverage"] = COVERAGE_SATISFIED
        dishonest["provenance"] = PROVENANCE_CUSTOMER_SUPPLIED
        install_documents(documents)

        with pytest.raises(MappingPackError, match="cannot be evidenced by customer-supplied"):
            pack_module._build_pack()

    def test_build_pack_rejects_a_framework_that_claims_everything(self, install_documents) -> None:
        documents = _pack_documents()
        documents["eu-ai-act.yaml"]["rows"][0]["coverage"] = COVERAGE_SUPPORTED
        documents["eu-ai-act.yaml"]["rows"][0]["artifact_type"] = "audit_chain"
        documents["eu-ai-act.yaml"]["rows"][0]["provenance"] = PROVENANCE_OBSERVED
        install_documents(documents)

        with pytest.raises(MappingPackError, match="claims every clause as met"):
            pack_module._build_pack()

    def test_build_pack_rejects_a_dropped_soc2_clause(self, install_documents) -> None:
        documents = _pack_documents()
        documents["soc2.yaml"]["rows"] = documents["soc2.yaml"]["rows"][:1]
        install_documents(documents)

        with pytest.raises(MappingPackError, match="missing required clauses"):
            pack_module._build_pack()


class TestBuildPackRejectsAMalformedDocumentSet:
    """The loader's structural rejections, each pinned through the real loader.

    A malformed shipped file must break the surface that serves it (the pack is
    an audit artefact, so a silently empty or half-loaded pack is the worst
    outcome). Each test mutates exactly one field of a valid document set, so
    the rejection is attributable to that mutation.
    """

    def test_rejects_a_pack_metadata_entry_that_is_not_a_mapping(self, install_documents) -> None:
        documents = _pack_documents()
        documents["pack.yaml"]["frameworks"] = ["soc2.yaml"]
        install_documents(documents)

        with pytest.raises(MappingPackError, match="expected a mapping"):
            pack_module._build_pack()

    def test_rejects_an_invalid_pack_version(self, install_documents) -> None:
        documents = _pack_documents()
        documents["pack.yaml"]["pack_version"] = "1.0"
        install_documents(documents)

        with pytest.raises(MappingPackError, match=r"MAJOR\.MINOR\.PATCH"):
            pack_module._build_pack()

    def test_rejects_pack_metadata_that_lists_no_frameworks(self, install_documents) -> None:
        documents = _pack_documents()
        documents["pack.yaml"]["frameworks"] = []
        install_documents(documents)

        with pytest.raises(MappingPackError, match="at least one framework"):
            pack_module._build_pack()

    def test_rejects_a_framework_entry_without_a_string_id_and_file(self, install_documents) -> None:
        documents = _pack_documents()
        documents["pack.yaml"]["frameworks"][0]["id"] = 7
        install_documents(documents)

        with pytest.raises(MappingPackError, match="needs string 'id' and 'file'"):
            pack_module._build_pack()

    def test_rejects_a_duplicate_framework_entry(self, install_documents) -> None:
        documents = _pack_documents()
        documents["pack.yaml"]["frameworks"].append({"id": "soc2", "file": "soc2.yaml"})
        install_documents(documents)

        with pytest.raises(MappingPackError, match="duplicate framework 'soc2'"):
            pack_module._build_pack()

    def test_rejects_a_framework_document_declaring_the_wrong_framework(self, install_documents) -> None:
        documents = _pack_documents()
        documents["soc2.yaml"]["framework"] = "iso_42001"
        install_documents(documents)

        with pytest.raises(MappingPackError, match="'framework' must equal 'soc2'"):
            pack_module._build_pack()

    def test_rejects_a_framework_document_with_no_rows(self, install_documents) -> None:
        documents = _pack_documents()
        documents["soc2.yaml"]["rows"] = []
        install_documents(documents)

        with pytest.raises(MappingPackError, match="'rows' must be a non-empty list"):
            pack_module._build_pack()


class TestLimitationFieldsMustBeStated:
    """A YAML null used to become the literal 'None'; omission became ''."""

    @pytest.mark.parametrize("field", ["source", "scope_note"])
    def test_rejects_a_null_framework_limitation_field(self, install_documents, field: str) -> None:
        documents = _pack_documents()
        documents["soc2.yaml"][field] = None
        install_documents(documents)

        with pytest.raises(MappingPackError, match=field):
            pack_module._build_pack()

    @pytest.mark.parametrize("field", ["source", "scope_note"])
    def test_rejects_an_omitted_framework_limitation_field(self, install_documents, field: str) -> None:
        documents = _pack_documents()
        del documents["soc2.yaml"][field]
        install_documents(documents)

        with pytest.raises(MappingPackError, match=field):
            pack_module._build_pack()

    @pytest.mark.parametrize("field", ["title", "published_at", "honesty_note"])
    def test_rejects_a_null_pack_limitation_field(self, install_documents, field: str) -> None:
        documents = _pack_documents()
        documents["pack.yaml"][field] = None
        install_documents(documents)

        with pytest.raises(MappingPackError, match=field):
            pack_module._build_pack()

    def test_rejects_a_pack_with_no_honesty_note_at_all(self, install_documents) -> None:
        documents = _pack_documents()
        del documents["pack.yaml"]["honesty_note"]
        install_documents(documents)

        with pytest.raises(MappingPackError, match="honesty_note"):
            pack_module._build_pack()
