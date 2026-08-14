"""AST-based SQL sandbox for the logical ``dataset`` relation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from sqlglot import exp, parse
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import Scope, build_scope


class UnsafeQueryError(ValueError):
    """The SQL is syntactically invalid or outside the read-only subset."""


@dataclass(frozen=True)
class ValidatedSql:
    canonical_sql: str


def _normalized_identifier(identifier: exp.Identifier) -> str:
    return identifier.name.casefold()


def _reject_unsupported_relations(expression: exp.Expression) -> None:
    """Reject relation-producing nodes other than tables/subqueries/CTEs.

    This is intentionally deny-by-default.  Polars supports file-reading table
    functions, so a name denylist alone would not be a sufficient sandbox.
    """
    for node in expression.walk():
        if isinstance(node, (exp.Values, exp.Unnest, exp.Lateral)):
            raise UnsafeQueryError("relation type is not allowed")

        if isinstance(node, exp.Table) and not isinstance(node.this, exp.Identifier):
            raise UnsafeQueryError("table functions are not allowed")


def _validate_scope(scope: Scope) -> int:
    physical_dataset_refs = 0

    for name in scope.cte_sources:
        if name.casefold() == "dataset":
            raise UnsafeQueryError("CTE alias must not shadow dataset")

    for relation_source in scope.sources.values():
        if isinstance(relation_source, Scope):
            continue
        if not isinstance(relation_source, exp.Table):
            raise UnsafeQueryError("relation type is not allowed")

    for table in scope.tables:
        table_source = scope.sources.get(table.alias_or_name)
        if isinstance(table_source, Scope):
            continue
        if not isinstance(table.this, exp.Identifier):
            raise UnsafeQueryError("table functions are not allowed")
        if table.this.args.get("quoted"):
            raise UnsafeQueryError("quoted table identifiers are not allowed")
        if table.args.get("db") is not None or table.args.get("catalog") is not None:
            raise UnsafeQueryError("qualified tables are not allowed")
        if _normalized_identifier(table.this) != "dataset":
            raise UnsafeQueryError("only the logical dataset table is allowed")
        physical_dataset_refs += 1

    return physical_dataset_refs


def _reachable_dataset_refs(scope: Scope, visited: set[int] | None = None) -> int:
    """Count physical dataset relations reachable from the final result graph."""
    seen = visited if visited is not None else set()
    identity = id(scope)
    if identity in seen:
        return 0
    seen.add(identity)

    count = 0
    for _node, source in scope.selected_sources.values():
        if isinstance(source, Scope):
            count += _reachable_dataset_refs(source, seen)
        elif isinstance(source, exp.Table):
            count += 1
    for child in (*scope.subquery_scopes, *scope.union_scopes):
        count += _reachable_dataset_refs(child, seen)
    return count


def validate_read_only_sql(sql: str) -> ValidatedSql:
    """Parse and validate one SELECT/CTE query, returning canonical SQL.

    The returned SQL, rather than the original text, is the only form handed to
    Polars. This removes comments and reduces parser differential surface.
    """
    if not sql or len(sql.encode("utf-8")) > 64 * 1024:
        raise UnsafeQueryError("SQL must be a non-empty string up to 64 KiB")
    try:
        statements = [statement for statement in parse(sql) if statement is not None]
    except ParseError as exc:
        raise UnsafeQueryError("invalid SQL syntax") from exc
    if len(statements) != 1:
        raise UnsafeQueryError("exactly one SQL statement is required")

    expression = statements[0]
    if not isinstance(expression, exp.Query):
        raise UnsafeQueryError("only SELECT queries are allowed")

    for with_node in expression.find_all(exp.With):
        if with_node.args.get("recursive"):
            raise UnsafeQueryError("recursive CTEs are not allowed")
        for cte in with_node.expressions:
            if cte.alias.casefold() == "dataset":
                raise UnsafeQueryError("CTE alias must not shadow dataset")

    _reject_unsupported_relations(cast(exp.Expression, expression))
    root_scope = build_scope(expression)
    if root_scope is None:
        raise UnsafeQueryError("query scope could not be validated")

    for scope in root_scope.traverse():
        _validate_scope(scope)
    if _reachable_dataset_refs(root_scope) == 0:
        raise UnsafeQueryError("query must reference the logical dataset table")

    canonical = expression.copy()
    for node in canonical.walk():
        node.comments = []
    return ValidatedSql(canonical.sql(pretty=False))


__all__ = ["UnsafeQueryError", "ValidatedSql", "validate_read_only_sql"]
