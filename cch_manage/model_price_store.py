from datetime import datetime
from decimal import Decimal
from typing import Any

import asyncpg

from cch_manage.model_prices import StoredModelPrice


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cch_model_prices (
    model_name text PRIMARY KEY,
    billing text NOT NULL,
    input_per_m numeric,
    output_per_m numeric,
    cache_read_per_m numeric,
    cache_write_per_m numeric,
    per_call numeric,
    updated_at timestamptz NOT NULL,
    long_context_threshold integer,
    long_context_coefficient numeric
)
"""

_LONG_CONTEXT_COLUMN_SQL = (
    "ALTER TABLE cch_model_prices "
    "ADD COLUMN IF NOT EXISTS long_context_threshold integer",
    "ALTER TABLE cch_model_prices "
    "ADD COLUMN IF NOT EXISTS long_context_coefficient numeric",
)

_UPSERT_SQL = """
INSERT INTO cch_model_prices (
    model_name,
    billing,
    input_per_m,
    output_per_m,
    cache_read_per_m,
    cache_write_per_m,
    per_call,
    updated_at,
    long_context_threshold,
    long_context_coefficient
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
ON CONFLICT (model_name) DO UPDATE SET
    billing = EXCLUDED.billing,
    input_per_m = EXCLUDED.input_per_m,
    output_per_m = EXCLUDED.output_per_m,
    cache_read_per_m = EXCLUDED.cache_read_per_m,
    cache_write_per_m = EXCLUDED.cache_write_per_m,
    per_call = EXCLUDED.per_call,
    updated_at = EXCLUDED.updated_at,
    long_context_threshold = EXCLUDED.long_context_threshold,
    long_context_coefficient = EXCLUDED.long_context_coefficient
"""


class PostgresModelPriceStore:
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
        for statement in _LONG_CONTEXT_COLUMN_SQL:
            await pool.execute(statement)

    async def replace_all(self, rows: list[StoredModelPrice]) -> None:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                names = [row.model_name for row in rows]
                if names:
                    await connection.execute(
                        "DELETE FROM cch_model_prices WHERE NOT (model_name = ANY($1::text[]))",
                        names,
                    )
                else:
                    await connection.execute("DELETE FROM cch_model_prices")
                if rows:
                    await connection.executemany(
                        _UPSERT_SQL,
                        [self._row_args(row) for row in rows],
                    )

    async def list_prices(self) -> list[StoredModelPrice]:
        records = await self._require_pool().fetch(
            """
            SELECT
                model_name,
                billing,
                input_per_m,
                output_per_m,
                cache_read_per_m,
                cache_write_per_m,
                per_call,
                updated_at,
                long_context_threshold,
                long_context_coefficient
            FROM cch_model_prices
            """
        )
        return [self._row_from_record(record) for record in records]

    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("CCH model price store is not open")
        return self._pool

    @staticmethod
    def _row_args(row: StoredModelPrice) -> tuple[Any, ...]:
        return (
            row.model_name,
            row.billing,
            row.input_per_m,
            row.output_per_m,
            row.cache_read_per_m,
            row.cache_write_per_m,
            row.per_call,
            row.updated_at,
            row.long_context_threshold,
            row.long_context_coefficient,
        )

    @staticmethod
    def _row_from_record(record: asyncpg.Record) -> StoredModelPrice:
        return StoredModelPrice(
            model_name=str(record["model_name"]),
            billing=str(record["billing"]),
            input_per_m=_decimal_or_none(record["input_per_m"]),
            output_per_m=_decimal_or_none(record["output_per_m"]),
            cache_read_per_m=_decimal_or_none(record["cache_read_per_m"]),
            cache_write_per_m=_decimal_or_none(record["cache_write_per_m"]),
            per_call=_decimal_or_none(record["per_call"]),
            updated_at=_datetime(record["updated_at"]),
            long_context_threshold=_int_or_none(record["long_context_threshold"]),
            long_context_coefficient=_decimal_or_none(record["long_context_coefficient"]),
        )


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return int(value)


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    raise RuntimeError("CCH model price row has no updated_at timestamp")
