import os
import asyncpg
from typing import Optional

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:example@localhost:5432/appointments")

pool: Optional[asyncpg.pool.Pool] = None

async def connect():
    global pool
    if pool is None:
        pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)

async def close():
    global pool
    if pool:
        await pool.close()
        pool = None
