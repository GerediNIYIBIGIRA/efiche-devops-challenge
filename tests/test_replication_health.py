"""
Unit tests for replication health logic.

PostgreSQL is mocked (unittest.mock) - never calls a real database.
Redis is replaced with fakeredis - no Redis server needed.
"""

import json
import time
import pytest
import fakeredis.aioredis as fakeredis

from ops_agent.replication_health import (
    LagReading,
    _classify_trend,
    _compute_slope,
    _is_degraded,
    _load_history,
    _store_reading,
    HISTORY_SIZE,
)


def readings(lags: list) -> list:
    return [LagReading(lag_seconds=l, recorded_at="2026-04-20T08:00:00Z") for l in lags]


def make_mock_db(lag_value: float):
    from unittest.mock import AsyncMock, MagicMock
    mock_row = MagicMock()
    mock_row.__getitem__ = lambda self, idx: lag_value
    mock_result = MagicMock()
    mock_result.fetchone.return_value = mock_row
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_result)
    return mock_db


@pytest.fixture
async def redis_client():
    client = fakeredis.FakeRedis()
    yield client
    await client.flushall()
    await client.aclose()


def test_slope_flat():
    assert _compute_slope([5.0, 5.0, 5.0]) == pytest.approx(0.0)

def test_slope_increasing():
    assert _compute_slope([1.0, 2.0, 3.0, 4.0, 5.0]) == pytest.approx(1.0)

def test_slope_decreasing():
    assert _compute_slope([5.0, 4.0, 3.0, 2.0, 1.0]) == pytest.approx(-1.0)

def test_slope_single_value():
    assert _compute_slope([3.0]) == 0.0

def test_slope_empty():
    assert _compute_slope([]) == 0.0

def test_trend_growing_monotonic():
    assert _classify_trend(readings([1.1, 1.8, 2.9, 3.6, 4.2])) == "growing"

def test_trend_growing_noisy():
    assert _classify_trend(readings([1.0, 2.5, 2.0, 4.0, 5.5])) == "growing"

def test_trend_growing_two_readings():
    assert _classify_trend(readings([1.0, 3.0])) == "growing"

def test_trend_recovering_monotonic():
    assert _classify_trend(readings([8.0, 6.5, 5.0, 3.2, 1.8])) == "recovering"

def test_trend_recovering_noisy():
    assert _classify_trend(readings([9.0, 7.0, 8.0, 5.0, 3.0])) == "recovering"

def test_trend_recovering_two_readings():
    assert _classify_trend(readings([10.0, 2.0])) == "recovering"

def test_trend_stable_flat():
    assert _classify_trend(readings([2.0, 2.0, 2.0, 2.0, 2.0])) == "stable"

def test_trend_stable_tiny_oscillation():
    assert _classify_trend(readings([2.0, 2.1, 1.9, 2.0, 2.1])) == "stable"

def test_trend_stable_single_reading():
    assert _classify_trend(readings([5.0])) == "stable"

def test_trend_stable_empty():
    assert _classify_trend([]) == "stable"

def test_degraded_true_when_growth_exceeds_10():
    assert _is_degraded(readings([0.5, 0.8, 1.0, 6.0, 12.5])) is True

def test_degraded_false_when_growth_under_10():
    assert _is_degraded(readings([0.5, 0.8, 1.0, 5.0, 10.9])) is False

def test_degraded_false_when_exactly_10():
    assert _is_degraded(readings([0.0, 0.0, 0.0, 5.0, 10.0])) is False

def test_degraded_false_too_few_readings():
    assert _is_degraded(readings([1.0, 2.0])) is False

def test_degraded_false_when_recovering():
    assert _is_degraded(readings([15.0, 10.0, 8.0, 5.0, 2.0])) is False


@pytest.mark.asyncio
async def test_redis_stores_and_loads(redis_client):
    await _store_reading(redis_client, 3.5, "2026-04-20T08:00:00Z")
    history = await _load_history(redis_client)
    assert len(history) == 1
    assert history[0].lag_seconds == 3.5


@pytest.mark.asyncio
async def test_redis_trims_to_history_size(redis_client):
    for i in range(HISTORY_SIZE + 3):
        await _store_reading(redis_client, float(i), "2026-04-20T08:00:00Z")
    history = await _load_history(redis_client)
    assert len(history) == HISTORY_SIZE


@pytest.mark.asyncio
async def test_redis_history_oldest_first(redis_client):
    for i in range(3):
        score = time.time() + i * 10
        value = json.dumps({"lag_seconds": float(i), "recorded_at": "2026-04-20T08:00:00Z"})
        await redis_client.zadd("replication_lag_history", {value: score})
    history = await _load_history(redis_client)
    lags = [r.lag_seconds for r in history]
    assert lags == sorted(lags)


@pytest.mark.asyncio
async def test_endpoint_growing_trend(redis_client):
    from fastapi import FastAPI
    from httpx import AsyncClient, ASGITransport
    from ops_agent.replication_health import router
    from ops_agent.database import get_db
    from ops_agent.dependencies import get_redis

    for i, lag in enumerate([1.1, 1.8, 2.9, 3.6]):
        score = time.time() - (5 - i) * 10
        value = json.dumps({"lag_seconds": lag, "recorded_at": "2026-04-20T08:00:00Z"})
        await redis_client.zadd("replication_lag_history", {value: score})

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: make_mock_db(4.2)
    app.dependency_overrides[get_redis] = lambda: redis_client

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/replication-health")

    assert resp.status_code == 200
    data = resp.json()
    assert data["trend"] == "growing"
    assert data["lag_seconds"] == pytest.approx(4.2, abs=0.1)


@pytest.mark.asyncio
async def test_endpoint_recovering_trend(redis_client):
    from fastapi import FastAPI
    from httpx import AsyncClient, ASGITransport
    from ops_agent.replication_health import router
    from ops_agent.database import get_db
    from ops_agent.dependencies import get_redis

    for i, lag in enumerate([9.0, 7.0, 5.0, 3.0]):
        score = time.time() - (5 - i) * 10
        value = json.dumps({"lag_seconds": lag, "recorded_at": "2026-04-20T08:00:00Z"})
        await redis_client.zadd("replication_lag_history", {value: score})

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: make_mock_db(1.5)
    app.dependency_overrides[get_redis] = lambda: redis_client

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/replication-health")

    assert resp.status_code == 200
    assert resp.json()["trend"] == "recovering"


@pytest.mark.asyncio
async def test_endpoint_degraded_flag(redis_client):
    from fastapi import FastAPI
    from httpx import AsyncClient, ASGITransport
    from ops_agent.replication_health import router
    from ops_agent.database import get_db
    from ops_agent.dependencies import get_redis

    for i, lag in enumerate([0.5, 1.0, 2.0, 6.0]):
        score = time.time() - (5 - i) * 10
        value = json.dumps({"lag_seconds": lag, "recorded_at": "2026-04-20T08:00:00Z"})
        await redis_client.zadd("replication_lag_history", {value: score})

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: make_mock_db(12.5)
    app.dependency_overrides[get_redis] = lambda: redis_client

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/replication-health")

    assert resp.status_code == 200
    assert resp.json()["degraded"] is True
