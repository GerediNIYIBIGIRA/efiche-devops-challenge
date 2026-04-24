"""
/replication-health endpoint.

Trend classification uses linear regression (OLS) over the last 5 lag readings:
  slope > +0.5 sec/reading  -> "growing"
  slope < -0.5 sec/reading  -> "recovering"
  otherwise                 -> "stable"

Degraded = lag grew by more than 10 seconds across the last 3 readings.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from .database import get_db
from .dependencies import get_redis

router = APIRouter()
logger = logging.getLogger("efiche.replication")

REDIS_KEY = "replication_lag_history"
HISTORY_SIZE = 5
DEGRADED_WINDOW = 3
DEGRADED_THRESHOLD = 10.0
TREND_SLOPE_THRESHOLD = 0.5


# --------------------------------------------------------------------------
# Pydantic models
# --------------------------------------------------------------------------

class LagReading(BaseModel):
    lag_seconds: float
    recorded_at: str


class ReplicationHealthResponse(BaseModel):
    lag_seconds: Optional[float]
    trend: str
    degraded: bool
    last_checked: str
    history: list[LagReading]


# --------------------------------------------------------------------------
# Pure logic helpers (also imported by tests)
# --------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _compute_slope(values: list[float]) -> float:
    """OLS slope over integer x-coordinates [0, 1, ..., n-1]."""
    n = len(values)
    if n < 2:
        return 0.0
    x_mean = (n - 1) / 2.0
    y_mean = sum(values) / n
    numerator = sum((i - x_mean) * (values[i] - y_mean) for i in range(n))
    denominator = sum((i - x_mean) ** 2 for i in range(n))
    return numerator / denominator if denominator != 0 else 0.0


def _classify_trend(readings: list[LagReading]) -> str:
    if len(readings) < 2:
        return "stable"
    slope = _compute_slope([r.lag_seconds for r in readings])
    if slope > TREND_SLOPE_THRESHOLD:
        return "growing"
    if slope < -TREND_SLOPE_THRESHOLD:
        return "recovering"
    return "stable"


def _is_degraded(readings: list[LagReading]) -> bool:
    if len(readings) < DEGRADED_WINDOW:
        return False
    window = readings[-DEGRADED_WINDOW:]
    return (window[-1].lag_seconds - window[0].lag_seconds) > DEGRADED_THRESHOLD


# --------------------------------------------------------------------------
# Redis helpers
# --------------------------------------------------------------------------

async def _store_reading(redis: aioredis.Redis, lag_seconds: float, recorded_at: str) -> None:
    score = time.time()
    value = json.dumps({"lag_seconds": lag_seconds, "recorded_at": recorded_at})
    async with redis.pipeline(transaction=True) as pipe:
        pipe.zadd(REDIS_KEY, {value: score})
        pipe.zremrangebyrank(REDIS_KEY, 0, -(HISTORY_SIZE + 1))
        await pipe.execute()


async def _load_history(redis: aioredis.Redis) -> list[LagReading]:
    readings = []
    for raw in await redis.zrange(REDIS_KEY, 0, -1):
        try:
            readings.append(LagReading(**json.loads(raw)))
        except Exception:
            continue
    return readings


# --------------------------------------------------------------------------
# Database helper
# In production: queries pg_last_xact_replay_timestamp() on the real replica.
# In dev: queries get_replication_lag_seconds() which reads from _stub_config.
# --------------------------------------------------------------------------

def _lag_query() -> str:
    """
    Return the SQL query to use based on environment.
    DEV_MODE=true  -> uses get_replication_lag_seconds() from stub
    otherwise      -> uses real pg_last_xact_replay_timestamp()
    """
    if os.getenv("DEV_MODE", "false").lower() == "true":
        return "SELECT get_replication_lag_seconds() AS lag"
    return "SELECT EXTRACT(EPOCH FROM (now() - pg_last_xact_replay_timestamp())) AS lag"


async def _fetch_current_lag(db: AsyncSession) -> Optional[float]:
    result = await db.execute(text(_lag_query()))
    row = result.fetchone()
    if row is None or row[0] is None:
        return None
    return float(row[0])


# --------------------------------------------------------------------------
# Endpoint
# --------------------------------------------------------------------------

@router.get("/replication-health", response_model=ReplicationHealthResponse)
async def get_replication_health(
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
) -> ReplicationHealthResponse:
    last_checked = _now_iso()

    try:
        lag_seconds = await _fetch_current_lag(db)
    except Exception as e:
        logger.error("Database error fetching lag: %s", e)
        raise HTTPException(status_code=503, detail=f"Database unavailable: {str(e)}")

    if lag_seconds is None:
        return ReplicationHealthResponse(
            lag_seconds=None,
            trend="stable",
            degraded=False,
            last_checked=last_checked,
            history=[],
        )

    try:
        await _store_reading(redis, lag_seconds, last_checked)
        history = await _load_history(redis)
    except Exception as e:
        logger.error("Redis error: %s", e)
        return ReplicationHealthResponse(
            lag_seconds=lag_seconds,
            trend="stable",
            degraded=False,
            last_checked=last_checked,
            history=[],
        )

    return ReplicationHealthResponse(
        lag_seconds=lag_seconds,
        trend=_classify_trend(history),
        degraded=_is_degraded(history),
        last_checked=last_checked,
        history=history,
    )