"""
Standalone Postgres pool for ReadinessOS. Deliberately does not import
anything from /opt/clg -- own database (readinessos), own role
(readinessos_app), zero code-level or network-level connection to the CLG
facility platform. Same isolation principle already applied to clg-bf.
"""
from __future__ import annotations

import os
from typing import Optional

import asyncpg

_pool: Optional[asyncpg.Pool] = None


async def init_pool() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool
    _pool = await asyncpg.create_pool(
        host=os.environ["RXOS_DB_HOST"],
        port=int(os.environ["RXOS_DB_PORT"]),
        user=os.environ["RXOS_DB_USER"],
        password=os.environ["RXOS_DB_PASSWORD"],
        database=os.environ["RXOS_DB_NAME"],
        min_size=1,
        max_size=5,
    )
    return _pool


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialized -- call init_pool() at startup.")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
