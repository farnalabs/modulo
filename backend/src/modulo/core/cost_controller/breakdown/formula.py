"""Safe 4-operator formula engine for cost components.

A hand-rolled tokenizer + recursive-descent parser (~100 LOC, stdlib-only).
Rejects ``eval()``/``exec()`` and third-party evaluators: the tokenizer cannot
even produce ``.``, ``[``, ``__``, a string literal, or a call, so there is
zero escape surface. ``eval()`` with restricted globals/locals is escapable via
introspection; ``asteval`` pulls in sympy and has escape history — for a
4-operator grammar a hand-rolled parser is smaller than the third-party review
cost (ADR 019).

Grammar (pinned — must NOT grow in v1):

    expr     := term (('+' | '-') term)*
    term     := factor (('*' | '/') factor)*
    factor   := '-' factor | primary
    primary  := NUMBER | IDENT | '(' expr ')'
    NUMBER   := [0-9]+('.'[0-9]+)? | '.'[0-9]+
    IDENT    := [A-Za-z_][A-Za-z0-9_]*

Allowed operators: ``+ - * /``, unary minus, parentheses. NO functions, no
``**``, no comparisons, no assignment, no attribute access, no subscripting.
"""

from __future__ import annotations

import decimal
import re
from decimal import Decimal
from typing import Any

from modulo.core.cost_controller.breakdown.constants import MAX_FORMULA_DEPTH, MAX_FORMULA_LENGTH

__all__ = ["CostFormulaError", "evaluate_formula", "validate_formula"]


class CostFormulaError(ValueError):
    """Formula parse/validation failure (save-time → 422).

    ``code`` is a stable machine code the API layer can surface alongside the
    human message: ``formula_too_long``, ``empty_expression``,
    ``unexpected_character``, ``unbalanced_parentheses``, ``unknown_identifier``,
    ``depth_exceeded``, ``unexpected_token``.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


_TOKEN_RE = re.compile(
    r"""
    [ \t\r\n\f\v]*                           # skip ASCII whitespace ONLY (NBSP is NOT
                                             #   whitespace here — it surfaces as
                                             #   unexpected_character, no silent juxtaposition)
    (?:
        (?P<number>\d+(?:\.\d+)?|\.\d+) |
        (?P<ident>[A-Za-z_][A-Za-z0-9_]*) |
        (?P<op>[+\-*/]) |
        (?P<lparen>\() |
        (?P<rparen>\)) |
        (?P<bad>.)
    )
    """,
    re.VERBOSE,
)


class _Token:
    __slots__ = ("kind", "value")

    def __init__(self, kind: str, value: Any) -> None:
        self.kind = kind
        self.value = value

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"_Token({self.kind}, {self.value!r})"


def _tokenize(formula: str) -> list[_Token]:
    tokens: list[_Token] = []
    pos = 0
    length = len(formula)
    while pos < length:
        match = _TOKEN_RE.match(formula, pos)
        if match is None:
            raise CostFormulaError("unexpected_character", "unexpected character in formula")
        if match.lastgroup == "number":
            tokens.append(_Token("number", match.group("number")))
        elif match.lastgroup == "ident":
            tokens.append(_Token("ident", match.group("ident")))
        elif match.lastgroup == "op":
            tokens.append(_Token("op", match.group("op")))
        elif match.lastgroup == "lparen":
            tokens.append(_Token("lparen", "("))
        elif match.lastgroup == "rparen":
            tokens.append(_Token("rparen", ")"))
        else:  # 'bad' — a character the grammar cannot produce (incl. non-ASCII)
            raise CostFormulaError(
                "unexpected_character",
                f"unexpected character {formula[pos]!r} in formula",
            )
        pos = match.end()
    tokens.append(_Token("eof", ""))
    return tokens


class _Parser:
    """Recursive-descent parser. Raises CostFormulaError on any out-of-grammar form."""

    def __init__(self, tokens: list[_Token], allowed_idents: frozenset[str]) -> None:
        self._tokens = tokens
        self._pos = 0
        self._allowed = allowed_idents

    # -- helpers ---------------------------------------------------------
    def _peek(self) -> _Token:
        return self._tokens[self._pos]

    def _next(self) -> _Token:
        tok = self._tokens[self._pos]
        self._pos += 1
        return tok

    def _expect(self, kind: str) -> _Token:
        tok = self._next()
        if tok.kind != kind:
            raise CostFormulaError("unexpected_token", f"expected {kind} in formula")
        return tok

    # -- grammar ---------------------------------------------------------
    def parse(self, depth: int = 0) -> Any:
        if depth > MAX_FORMULA_DEPTH:
            raise CostFormulaError(
                "depth_exceeded",
                f"formula nesting exceeds max depth {MAX_FORMULA_DEPTH}",
            )
        return self._expr(depth)

    def _check_depth(self, depth: int) -> None:
        if depth > MAX_FORMULA_DEPTH:
            raise CostFormulaError(
                "depth_exceeded",
                f"formula nesting exceeds max depth {MAX_FORMULA_DEPTH}",
            )

    def _expr(self, depth: int) -> Any:
        self._check_depth(depth)
        node = self._term(depth)
        while self._peek().kind == "op" and self._peek().value in ("+", "-"):
            op = self._next().value
            rhs = self._term(depth)
            node = (op, node, rhs)
        return node

    def _term(self, depth: int) -> Any:
        node = self._factor(depth)
        while self._peek().kind == "op" and self._peek().value in ("*", "/"):
            op = self._next().value
            rhs = self._factor(depth)
            node = (op, node, rhs)
        return node

    def _factor(self, depth: int) -> Any:
        self._check_depth(depth)
        if self._peek().kind == "op" and self._peek().value == "-":
            self._next()
            return ("neg", self._factor(depth + 1))
        return self._primary(depth)

    def _primary(self, depth: int) -> Any:
        tok = self._peek()
        if tok.kind == "number":
            self._next()
            return ("num", tok.value)
        if tok.kind == "ident":
            self._next()
            if tok.value not in self._allowed:
                raise CostFormulaError(
                    "unknown_identifier",
                    f"unknown identifier {tok.value!r} (allowed: {sorted(self._allowed)})",
                )
            return ("ident", tok.value)
        if tok.kind == "lparen":
            self._next()
            inner = self._expr(depth + 1)
            self._expect("rparen")
            return ("group", inner)
        if tok.kind == "rparen":
            raise CostFormulaError("unbalanced_parentheses", "unbalanced parentheses in formula")
        if tok.kind == "eof":
            raise CostFormulaError("unexpected_token", "unexpected end of formula")
        raise CostFormulaError("unexpected_token", f"unexpected token {tok.value!r} in formula")


def _compile(formula: str, allowed_idents: frozenset[str]) -> Any:
    if len(formula) > MAX_FORMULA_LENGTH:
        raise CostFormulaError(
            "formula_too_long",
            f"formula exceeds max length {MAX_FORMULA_LENGTH}",
        )
    if not formula.strip():
        raise CostFormulaError("empty_expression", "formula is empty")
    tokens = _tokenize(formula)
    parser = _Parser(tokens, allowed_idents)
    node = parser.parse()
    if parser._peek().kind != "eof":
        raise CostFormulaError("unexpected_token", "trailing tokens in formula")
    return node


def validate_formula(formula: str | None, allowed_idents: frozenset[str]) -> None:
    """Validate a formula string against the grammar + the identifier allowlist.

    Raises :class:`CostFormulaError` on any violation. This is the SINGLE
    validate function — it runs at save time AND at eval time.
    """
    if formula is None:
        return
    _compile(formula, allowed_idents)


def _evaluate(node: Any, params: dict[str, Decimal]) -> Decimal:
    op = node[0]
    if op == "num":
        return Decimal(node[1])
    if op == "ident":
        return params[node[1]]
    if op == "neg":
        return -_evaluate(node[1], params)
    if op == "group":
        return _evaluate(node[1], params)
    left = _evaluate(node[1], params)
    right = _evaluate(node[2], params)
    if op == "+":
        return left + right
    if op == "-":
        return left - right
    if op == "*":
        return left * right
    if op == "/":
        return left / right
    raise CostFormulaError("eval_error", f"unknown operator {op!r}")  # pragma: no cover


def evaluate_formula(
    formula: str,
    params: dict[str, Decimal],
    allowed_idents: frozenset[str],
) -> Decimal:
    """Evaluate a formula against ``params`` (Decimal-typed values).

    Parse + identifier validation run first (the single validate function).
    Eval-time failures — a missing param, division by zero, a non-finite
    result (incl. NaN from ``0/0`` under the reduced-traps localcontext), an
    overflow, or a NEGATIVE final result — raise :class:`CostFormulaError`
    with code ``eval_error``. Intermediate subexpressions may be negative;
    only the final value is checked.
    """
    node = _compile(formula, allowed_idents)
    try:
        result = _evaluate(node, params)
    except decimal.DecimalException:
        raise CostFormulaError("eval_error", "non-finite formula result") from None
    if not result.is_finite():
        raise CostFormulaError("eval_error", "non-finite formula result")
    if result < 0:
        raise CostFormulaError("eval_error", "negative formula result")
    return result
