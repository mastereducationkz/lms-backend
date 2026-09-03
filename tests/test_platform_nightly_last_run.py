"""The nightly job must run once per Almaty day even across container restarts: the last-run
date is persisted in Redis (cache_service) with an in-memory fallback, so a redeploy at
03:40 Almaty does not re-run the 7-day IELTS reconciliation."""

from datetime import datetime
from types import SimpleNamespace

import pytest

import src.schemas.models  # noqa: F401 - register models before any domain import
from src.integrations import reconcile

AT_0340_ALMATY = datetime(2026, 9, 2, 22, 40)  # UTC; == 03:40 Asia/Almaty on 2026-09-03


@pytest.fixture()
def store(monkeypatch):
    data = {}
    monkeypatch.setattr(reconcile.cache_service, "get_json", lambda key: data.get(key))
    monkeypatch.setattr(
        reconcile.cache_service, "set_json",
        lambda key, value, ttl_seconds=None: data.__setitem__(key, value) or True,
    )
    return data


@pytest.fixture()
def nightly(monkeypatch):
    monkeypatch.setenv("PLATFORM_EVENTS_INGEST_ENABLED", "true")
    monkeypatch.setattr(reconcile, "_utcnow", lambda: AT_0340_ALMATY)
    import src.config as config
    monkeypatch.setattr(config, "SessionLocal", lambda: SimpleNamespace(close=lambda: None))
    runs = []
    monkeypatch.setattr(reconcile, "run_nightly", lambda db: runs.append(1) or {"ok": True})
    return runs


def test_fresh_process_skips_when_the_store_says_it_already_ran_today(store, nightly):
    store[reconcile.LAST_RUN_KEY] = "2026-09-03"
    reconcile.PlatformNightlyScheduler().tick()   # in-memory last_run_date is None: a restart
    assert nightly == []


def test_run_persists_the_almaty_date_and_does_not_repeat(store, nightly):
    sched = reconcile.PlatformNightlyScheduler()
    sched.tick()
    sched.tick()
    assert nightly == [1]
    assert store[reconcile.LAST_RUN_KEY] == "2026-09-03"


def test_stale_store_value_from_yesterday_does_not_block(store, nightly):
    store[reconcile.LAST_RUN_KEY] = "2026-09-02"
    reconcile.PlatformNightlyScheduler().tick()
    assert nightly == [1]


def test_memory_fallback_without_a_store(monkeypatch, nightly):
    monkeypatch.setattr(reconcile.cache_service, "get_json", lambda key: None)
    monkeypatch.setattr(reconcile.cache_service, "set_json", lambda *a, **k: False)
    sched = reconcile.PlatformNightlyScheduler()
    sched.tick()
    sched.tick()
    assert nightly == [1]


def test_disabled_flag_never_runs(monkeypatch, store, nightly):
    monkeypatch.setenv("PLATFORM_EVENTS_INGEST_ENABLED", "false")
    reconcile.PlatformNightlyScheduler().tick()
    assert nightly == [] and reconcile.LAST_RUN_KEY not in store
