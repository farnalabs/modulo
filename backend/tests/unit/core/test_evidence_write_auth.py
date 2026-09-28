"""Producer write-authorisation tests (FAR-966 chunk 7, spec criteria W1-W3).

Covers the key-namespace ownership map (``KEY_NAMESPACE_OWNERSHIP``) via
``assert_write_authorisation``:

- **W1** — a producer writing a key within its namespace is accepted.
- **W2** — a producer writing a key outside its namespace raises
  ``EvidenceWriteAuthorisationError`` (at least 3 cross-namespace cases,
  parametrised).
- **W3** — a producer with an unrecognised type raises
  ``EvidenceWriteAuthorisationError``.

Also asserts the map's coherence invariants: every producer type in the map
must be a member of the authoritative DB CHECK vocabulary
(``PRODUCER_TYPES``).
"""

import pytest

from modulo.core.eval_engine.evidence_layer import (
    KEY_NAMESPACE_OWNERSHIP,
    EvidenceWriteAuthorisationError,
    assert_write_authorisation,
)
from modulo.db.models.evidence import PRODUCER_TYPES

# ---------------------------------------------------------------------------
# W1 — authorised write accepted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("producer_type", "key"),
    [
        ("eval", "eval_passed"),
        ("eval", "eval_score"),
        ("system_state", "connector_health"),
        ("system_state", "sandbox_region"),
        ("system_state", "capability_reasoning"),
        ("run", "node_has_work"),
    ],
)
def test_w1_authorised_write_accepted(producer_type: str, key: str) -> None:
    """A producer writing a key within its own namespace is accepted (no raise)."""
    assert_write_authorisation(producer_type, key)  # does not raise


def test_w1_every_registered_prefix_is_accepted_for_its_owner() -> None:
    """Every prefix registered in the map accepts its own prefix verbatim
    (never an empty, or wrongly-cased, prefix set)."""
    accepted_pairs: list[tuple[str, str]] = []
    for producer_type, prefixes in KEY_NAMESPACE_OWNERSHIP.items():
        for prefix in prefixes:
            assert_write_authorisation(producer_type, prefix)
            accepted_pairs.append((producer_type, prefix))
    assert accepted_pairs
    assert ("eval", "eval_") in accepted_pairs
    assert ("run", "node_") in accepted_pairs


# ---------------------------------------------------------------------------
# W2 — unauthorised write rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("producer_type", "key"),
    [
        ("run", "eval_passed"),
        ("run", "connector_health"),
        ("eval", "node_has_work"),
        ("system_state", "eval_passed"),
        ("system_state", "node_has_work"),
        ("policy_gate", "eval_passed"),
        ("derived", "node_has_work"),
    ],
)
def test_w2_unauthorised_write_rejected(producer_type: str, key: str) -> None:
    """A producer writing outside its namespace raises EvidenceWriteAuthorisationError."""
    with pytest.raises(EvidenceWriteAuthorisationError) as excinfo:
        assert_write_authorisation(producer_type, key)
    message = str(excinfo.value)
    assert "not authorised to write key" in message
    # The offending producer type and key appear so operators can diagnose.
    assert producer_type in message
    assert key in message


def test_w2_boundary_exact_key_without_suffix_is_rejected() -> None:
    """Prefix ownership matches the prefix, not any string starting with the
    producer name: ``eval`` alone does not start with ``eval_`` and must be
    rejected for the ``eval`` producer."""
    with pytest.raises(EvidenceWriteAuthorisationError):
        assert_write_authorisation("eval", "eval")


@pytest.mark.parametrize("producer_type", ["policy_gate", "derived"])
def test_w2_reserved_producers_write_nothing(producer_type: str) -> None:
    """Reserved producers have an empty namespace: any key is unauthorised."""
    assert not KEY_NAMESPACE_OWNERSHIP[producer_type]
    with pytest.raises(EvidenceWriteAuthorisationError):
        assert_write_authorisation(producer_type, "eval_passed")


# ---------------------------------------------------------------------------
# W3 — unknown producer type rejected
# ---------------------------------------------------------------------------


def test_w3_unknown_producer_type_rejected() -> None:
    """A producer type outside the map raises even for an empty key."""
    with pytest.raises(EvidenceWriteAuthorisationError) as excinfo:
        assert_write_authorisation("intruder", "eval_passed")
    assert "Unknown producer type: intruder" in str(excinfo.value)


@pytest.mark.parametrize("producer_type", ["", "EVAL", "eval2", "run_", None])
def test_w3_unknown_producer_type_rejected_parametrised(producer_type: object) -> None:
    """Case variations, suffixes, empty and None are all unknown producer types."""
    with pytest.raises(EvidenceWriteAuthorisationError, match="Unknown producer type"):
        assert_write_authorisation(producer_type, "eval_passed")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Map coherence invariants
# ---------------------------------------------------------------------------


def test_map_producer_types_are_subset_of_check_vocabulary() -> None:
    """Every producer type in KEY_NAMESPACE_OWNERSHIP must be a member of the
    authoritative DB CHECK constraint vocabulary (PRODUCER_TYPES)."""
    map_types = frozenset(KEY_NAMESPACE_OWNERSHIP.keys())
    assert map_types.issubset(PRODUCER_TYPES)
    assert map_types == PRODUCER_TYPES


def test_map_prefixes_follow_producer_type_namespace() -> None:
    """Each registered key prefix begins with its producer's own namespace stem
    (eval -> eval_, run -> node_ etc.), keeping cross-producer confusion out."""
    assert KEY_NAMESPACE_OWNERSHIP["eval"] == {"eval_"}
    assert KEY_NAMESPACE_OWNERSHIP["run"] == {"node_"}
    assert KEY_NAMESPACE_OWNERSHIP["system_state"] == {
        "connector_",
        "sandbox_",
        "capability_",
    }
