from datetime import datetime
from decimal import Decimal
from typing import Any

import asyncpg

from cch_manage.rankings import RankSnapshotMeta, StoredRankRow


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cch_rank_rows (
    scope text NOT NULL,
    entity_key text NOT NULL,
    name text NOT NULL,
    request_count bigint NOT NULL,
    token_count bigint NOT NULL,
    cost_usd numeric NOT NULL,
    PRIMARY KEY (scope, entity_key)
)
"""

_META_SQL = """
CREATE TABLE IF NOT EXISTS cch_rank_meta (
    id integer PRIMARY KEY,
    week_start timestamptz NOT NULL,
    week_end timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT cch_rank_meta_singleton CHECK (id = 1)
)
"""

_INSERT_SQL = """
INSERT INTO cch_rank_rows (
    scope,
    entity_key,
    name,
    request_count,
    token_count,
    cost_usd
) VALUES ($1, $2, $3, $4, $5, $6)
"""

_META_UPSERT_SQL = """
INSERT INTO cch_rank_meta (id, week_start, week_end, updated_at)
VALUES (1, $1, $2, $3)
ON CONFLICT (id) DO UPDATE SET
    week_start = EXCLUDED.week_start,
    week_end = EXCLUDED.week_end,
    updated_at = EXCLUDED.updated_at
"""


class PostgresRankStore:
    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._pool: asyncpg.Pool | None = None

    async def open(self) -> None:
        self._pool = await asyncpg.create_pool(self._database_url, min_size=1, max_size=3)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def ensure_schema(self) -> None:
        pool = self._require_pool()
        await pool.execute(_SCHEMA_SQL)
        await pool.execute(_META_SQL)

    async def replace_all(
        self,
        rows: list[StoredRankRow],
        *,
        week_start: datetime,
        week_end: datetime,
        updated_at: datetime,
    ) -> None:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute("DELETE FROM cch_rank_rows")
                if rows:
                    await connection.executemany(
                        _INSERT_SQL,
                        [self._row_args(row) for row in rows],
                    )
                await connection.execute(_META_UPSERT_SQL, week_start, week_end, updated_at)

    async def list_snapshot(self) -> tuple[list[StoredRankRow], RankSnapshotMeta | None]:
        pool = self._require_pool()
        meta_record = await pool.fetchrow(
            """
            SELECT week_start, week_end, updated_at
            FROM cch_rank_meta
            WHERE id = 1
            """
        )
        records = await pool.fetch(
            """
            SELECT scope, entity_key, name, request_count, token_count, cost_usd
            FROM cch_rank_rows
            """
        )
        rows = [self._row_from_record(record) for record in records]
        if meta_record is None:
            return rows, None
        return rows, RankSnapshotMeta(
            week_start=_datetime(meta_record["week_start"]),
            week_end=_datetime(meta_record["week_end"]),
            updated_at=_datetime(meta_record["updated_at"]),
        )

    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("CCH rank store is not open")
        return self._pool

    @staticmethod
    def _row_args(row: StoredRankRow) -> tuple[Any, ...]:
        return (
            row.scope,
            row.entity_key,
            row.name,
            row.request_count,
            row.token_count,
            row.cost_usd,
        )

    @staticmethod
    def _row_from_record(record: asyncpg.Record) -> StoredRankRow:
        return StoredRankRow(
            scope=str(record["scope"]),
            entity_key=str(record["entity_key"]),
            name=str(record["name"]),
            request_count=int(record["request_count"]),
            token_count=int(record["token_count"]),
            cost_usd=_decimal(record["cost_usd"]),
        )


def _decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    raise RuntimeError("CCH rank snapshot has no timestamp")
