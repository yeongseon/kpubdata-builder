"""Security contract for #504 read-only SQL."""

from __future__ import annotations

import pytest

from kpubdata_builder.query.security import UnsafeQueryError, validate_read_only_sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM dataset",
        "SELECT city, COUNT(*) AS n FROM dataset GROUP BY city ORDER BY n DESC",
        "WITH filtered AS (SELECT * FROM dataset WHERE value > 0) SELECT * FROM filtered",
        "WITH a AS (SELECT * FROM dataset), b AS (SELECT * FROM a) SELECT * FROM b",
        "SELECT * FROM (SELECT * FROM dataset WHERE value > 0) AS nested",
    ],
)
def test_allows_queries_derived_from_dataset(sql: str) -> None:
    assert validate_read_only_sql(sql).canonical_sql


@pytest.mark.parametrize(
    "sql",
    [
        "WITH dataset AS (SELECT 1 AS x) SELECT * FROM dataset",
        'WITH "dataset" AS (SELECT 1 AS x) SELECT * FROM "dataset"',
        "WITH RECURSIVE x AS (SELECT * FROM dataset) SELECT * FROM x",
        "SELECT * FROM other",
        "SELECT * FROM main.dataset",
        'SELECT * FROM "dataset"',
        "SELECT 1",
        "WITH unused AS (SELECT * FROM dataset) SELECT 1",
        "SELECT * FROM (VALUES (1)) AS x",
        "SELECT * FROM UNNEST([1, 2])",
        "SELECT * FROM read_csv('data.csv')",
        "SELECT * FROM ReAd_ParQuEt /* disguised */ ('data.parquet')",
        "SELECT * FROM dataset JOIN read_json('x.json') AS x ON true",
        "SELECT * FROM dataset; SELECT * FROM dataset",
        "INSERT INTO dataset VALUES (1)",
        "UPDATE dataset SET value = 1",
        "DELETE FROM dataset",
        "CREATE TABLE other AS SELECT * FROM dataset",
        "DROP TABLE dataset",
        "ALTER TABLE dataset ADD COLUMN x INT",
        "COPY dataset TO 'x.csv'",
        "ATTACH 'x.db' AS x",
        "INSTALL httpfs",
        "LOAD httpfs",
        "SET threads = 4",
        "PRAGMA version",
    ],
)
def test_rejects_unsafe_or_unbound_relations(sql: str) -> None:
    with pytest.raises(UnsafeQueryError):
        validate_read_only_sql(sql)


def test_canonical_sql_removes_comments() -> None:
    result = validate_read_only_sql("SELECT /* untrusted */ * FROM dataset")
    assert "untrusted" not in result.canonical_sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT city FROM dataset UNION SELECT city FROM dataset",
        "SELECT city FROM dataset UNION ALL SELECT city FROM dataset",
        "SELECT city FROM dataset INTERSECT SELECT city FROM dataset",
        "SELECT city FROM dataset EXCEPT SELECT city FROM dataset",
        "WITH a AS (SELECT city FROM dataset UNION SELECT city FROM dataset) SELECT * FROM a",
        "SELECT * FROM (SELECT city FROM dataset UNION SELECT city FROM dataset) AS u",
    ],
)
def test_allows_set_operations_over_dataset(sql: str) -> None:
    """집합 연산도 dataset에서 파생된 질의다 (#504).

    scope walker가 집합 연산 분기를 못 읽으면 여기서 AttributeError로 터진다 —
    sqlglot 30.19가 ``Scope.union_scopes``를 ``set_operation_scopes``로 바꿨고,
    선언된 버전 범위(>=30.17,<31)는 양쪽을 모두 허용한다. 질의 허용 여부를 정하는
    가드 안에서 나는 예외라 조용히 지나갈 수 없다.
    """
    assert validate_read_only_sql(sql).sql


def test_set_operation_branches_are_counted_not_skipped() -> None:
    # 분기를 못 세면 "dataset을 참조해야 한다" 규칙이 통과할 수 없다 — 이 질의는
    # 오직 분기 안에서만 dataset을 참조한다.
    assert validate_read_only_sql(
        "SELECT * FROM (SELECT city FROM dataset UNION SELECT city FROM dataset) AS u"
    ).sql


def test_set_operation_over_a_non_dataset_table_is_still_rejected() -> None:
    with pytest.raises(UnsafeQueryError):
        validate_read_only_sql("SELECT city FROM other UNION SELECT city FROM other")
