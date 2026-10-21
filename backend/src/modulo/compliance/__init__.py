"""Compliance control-mapping pack and per-instance evidence index (FAR-2036).

Modulo holds no SOC 2, ISO/IEC 42001 or EU AI Act attestation. In a
self-hosted deployment the customer's auditor audits the customer's own
instance, so this package ships the two things that make that possible:

* a **versioned control-mapping pack** (:mod:`modulo.compliance.pack`) mapping
  framework clauses to Modulo controls, the artefact that evidences each one,
  where the customer finds it, an honest coverage attribution and the
  evidence's provenance;
* a **per-instance evidence index** (:mod:`modulo.compliance.evidence_index`)
  that lists the artefacts that actually exist for one organisation, keyed to
  the mapping rows, and surfaces a missing mapped artefact instead of omitting
  it.

Coverage is per row and per framework. There is no single compliant verdict.
"""

from __future__ import annotations

from modulo.compliance.artifacts import (
    ARTIFACT_TYPES,
    COVERAGE_VALUES,
    INDEXED_ARTIFACT_TYPES,
    PROVENANCE_VALUES,
)
from modulo.compliance.evidence_index import (
    EvidenceIndex,
    EvidenceIndexIncompleteError,
    build_evidence_index,
)
from modulo.compliance.pack import MappingPack, MappingPackError, MappingRow, load_mapping_pack

__all__ = [
    "ARTIFACT_TYPES",
    "COVERAGE_VALUES",
    "INDEXED_ARTIFACT_TYPES",
    "PROVENANCE_VALUES",
    "EvidenceIndex",
    "EvidenceIndexIncompleteError",
    "MappingPack",
    "MappingPackError",
    "MappingRow",
    "build_evidence_index",
    "load_mapping_pack",
]
