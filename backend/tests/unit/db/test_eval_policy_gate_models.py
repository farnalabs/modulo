"""ORM metadata unit tests for Eval, PolicyGate, PolicyGateDecision (FAR-1060).

Covers criteria 14, 17, 15a (unit half), plus UniqueConstraint assertions.
No database, no Docker -- inspects ORM __table_args__ only.
"""

import re

from sqlalchemy import CheckConstraint, Integer, UniqueConstraint

from modulo.db.models.eval import _VALID_EVAL_TYPES, Eval
from modulo.db.models.eval_definition import EvalDefinition
from modulo.db.models.policy_gate import PolicyGate
from modulo.db.models.policy_gate_decision import PolicyGateDecision

# -----------------------------------------------------------------------
# C14: version columns exist as Integer, not nullable, with server default "1"
# -----------------------------------------------------------------------


class TestC14VersionColumns:
    """Both Eval.version and PolicyGate.version are Integer, not nullable,
    with a server default of '1'."""

    def test_eval_version_is_integer_not_nullable(self) -> None:
        col = Eval.__table__.c["version"]
        assert isinstance(col.type, Integer)
        assert col.nullable is False

    def test_eval_version_server_default(self) -> None:
        col = Eval.__table__.c["version"]
        assert col.server_default is not None
        assert "1" in str(col.server_default.arg)

    def test_policy_gate_version_is_integer_not_nullable(self) -> None:
        col = PolicyGate.__table__.c["version"]
        assert isinstance(col.type, Integer)
        assert col.nullable is False

    def test_policy_gate_version_server_default(self) -> None:
        col = PolicyGate.__table__.c["version"]
        assert col.server_default is not None
        assert "1" in str(col.server_default.arg)


# -----------------------------------------------------------------------
# C15a (unit half): pre_version_raw exists and is nullable
# -----------------------------------------------------------------------


class TestC15aPreVersionRaw:
    """pre_version_raw is nullable JSONB on both Eval and PolicyGate."""

    def test_eval_pre_version_raw_nullable(self) -> None:
        col = Eval.__table__.c["pre_version_raw"]
        assert col.nullable is True

    def test_policy_gate_pre_version_raw_nullable(self) -> None:
        col = PolicyGate.__table__.c["pre_version_raw"]
        assert col.nullable is True

    def test_eval_pre_version_raw_has_no_default(self) -> None:
        """The column has no server default, so INSERT without it yields NULL."""
        col = Eval.__table__.c["pre_version_raw"]
        assert col.server_default is None

    def test_policy_gate_pre_version_raw_has_no_default(self) -> None:
        col = PolicyGate.__table__.c["pre_version_raw"]
        assert col.server_default is None


# -----------------------------------------------------------------------
# C17: eval_type CHECK constraints carry identical vocabulary and order
# -----------------------------------------------------------------------


def _extract_check_vocabulary(sqltext: str) -> tuple[str, ...]:
    """Extract the IN-list values from a CHECK constraint like
    ``eval_type IN ('llm_judge', 'regex', ...)``."""
    match = re.search(r"IN\s*\(([^)]+)\)", sqltext)
    if match is None:
        msg = f"Could not extract IN-list from: {sqltext}"
        raise ValueError(msg)
    raw = match.group(1)
    values = [v.strip().strip("'") for v in raw.split(",")]
    return tuple(values)


def _find_check_constraint(table_args: tuple, substring: str) -> CheckConstraint:
    """Find a CheckConstraint whose sqltext contains *substring*."""
    for arg in table_args:
        if isinstance(arg, CheckConstraint) and substring in str(arg.sqltext):
            return arg
    msg = f"No CheckConstraint containing '{substring}' in {table_args}"
    raise AssertionError(msg)


class TestC17CheckConstraintVocabulary:
    """The two eval_type CHECK constraints carry identical vocabulary and order."""

    def test_eval_and_eval_definition_same_vocabulary(self) -> None:
        """Extract both constraint sqltexts and compare the vocabulary.
        Do NOT hardcode the value list -- compare the two constraints to each
        other so a future value added to one only fails the test."""
        eval_ck = _find_check_constraint(Eval.__table_args__, "eval_type IN")
        eval_def_ck = _find_check_constraint(EvalDefinition.__table_args__, "eval_type IN")

        eval_vocab = _extract_check_vocabulary(str(eval_ck.sqltext))
        eval_def_vocab = _extract_check_vocabulary(str(eval_def_ck.sqltext))

        assert eval_vocab == eval_def_vocab, (
            f"evals.eval_type CHECK vocabulary {eval_vocab} does not match "
            f"eval_definitions.eval_type CHECK vocabulary {eval_def_vocab}"
        )

    def test_vocabulary_matches_centralised_constant(self) -> None:
        """The centralised _VALID_EVAL_TYPES tuple is the source of truth.
        A value added to one constraint but not the other fails the test above.
        This test asserts the Eval constraint matches the constant."""
        eval_ck = _find_check_constraint(Eval.__table_args__, "eval_type IN")
        vocab = _extract_check_vocabulary(str(eval_ck.sqltext))
        assert vocab == _VALID_EVAL_TYPES


# -----------------------------------------------------------------------
# UniqueConstraint: each table carries UniqueConstraint("id", "organisation_id")
# -----------------------------------------------------------------------


class TestUniqueConstraints:
    """Every new table must carry UniqueConstraint("id", "organisation_id")."""

    @staticmethod
    def _has_uq(table_args: tuple, col1: str, col2: str) -> bool:
        for arg in table_args:
            if isinstance(arg, UniqueConstraint):
                col_names = tuple(c.name for c in arg.columns)
                if col1 in col_names and col2 in col_names:
                    return True
        return False

    def test_eval_has_unique_id_org(self) -> None:
        assert self._has_uq(Eval.__table_args__, "id", "organisation_id")

    def test_policy_gate_has_unique_id_org(self) -> None:
        assert self._has_uq(PolicyGate.__table_args__, "id", "organisation_id")

    def test_policy_gate_decision_has_unique_id_org(self) -> None:
        assert self._has_uq(PolicyGateDecision.__table_args__, "id", "organisation_id")
