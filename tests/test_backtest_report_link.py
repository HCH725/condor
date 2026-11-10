"""A stored task finds its reports after restart, without widening ownership."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from condor.backtest_store import BacktestStore
from condor.reports import ReportBuilder
from condor.reports import store as report_store
from condor.web import auth
from condor.web.models import WebUser
from condor.web.routes import backtesting

ALICE = WebUser(id=11, username="alice", first_name="Alice", role="user")
BOB = WebUser(id=22, username="bob", first_name="Bob", role="user")
ADMIN = WebUser(id=99, username="admin", first_name="Admin", role="admin")


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDOR_REPORTS_DIR", str(tmp_path / "reports"))
    archive_dir = tmp_path / "backtests"
    archive = BacktestStore(archive_dir)
    archive.save_result("local", "task-1", {"status": "completed"})
    archive.save_result("private", "task-private", {"status": "completed"})
    # Deliberately discard the first store; discovery must be durable, not rely
    # on an in-memory instance or last_report_id contextvar.
    monkeypatch.setattr(
        backtesting, "get_backtest_store", lambda: BacktestStore(archive_dir)
    )
    cm = SimpleNamespace(
        list_servers=lambda: {"local": {}, "private": {}},
        has_server_access=lambda uid, name, *args: name == "local" or uid == 99,
        is_admin=lambda uid: uid == 99,
    )
    monkeypatch.setattr(backtesting, "get_config_manager", lambda: cm)
    monkeypatch.setattr(auth, "get_config_manager", lambda: cm)
    app = FastAPI()
    app.include_router(backtesting.router, prefix="/api/v1")
    current = [ALICE]
    app.dependency_overrides[auth.get_current_user] = lambda: current[0]
    return TestClient(app), current


def _report(task="task-1", owner=11, source="backtest_chart", subject=None):
    builder = ReportBuilder("Persisted backtest").source("routine", source)
    # Match the public subject contract independently of the implementation.
    builder.subject(subject if subject is not None else "backtest:" + task)
    builder.markdown("An isolated report fixture, not market data.")
    with report_store.attribute_owner(owner):
        rid = asyncio.run(builder.save())
    report_store.reset_last_report_id()
    return rid


def test_task_finds_report_from_disk_after_instance_state_is_gone(environment):
    client, _ = environment
    rid = _report()
    response = client.get("/api/v1/backtesting/archive/task-1/reports")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert [row["id"] for row in body["reports"]] == [rid]
    assert body["reports"][0]["subject"] == "backtest:task-1"


def test_same_server_access_does_not_reveal_another_owners_report(environment):
    client, current = environment
    own = _report(owner=11)
    other = _report(owner=22)
    response = client.get("/api/v1/backtesting/archive/task-1/reports")
    assert [row["id"] for row in response.json()["reports"]] == [own]
    current[0] = BOB
    response = client.get("/api/v1/backtesting/archive/task-1/reports")
    assert [row["id"] for row in response.json()["reports"]] == [other]
    current[0] = ADMIN
    assert client.get("/api/v1/backtesting/archive/task-1/reports").json()["total"] == 2


def test_unavailable_server_task_is_404_even_when_report_is_owned(environment):
    client, _ = environment
    _report(task="task-private")
    assert (
        client.get("/api/v1/backtesting/archive/task-private/reports").status_code
        == 404
    )
    assert client.get("/api/v1/backtesting/archive/missing/reports").status_code == 404


def test_a_pruned_report_becomes_an_empty_lookup_not_a_dangling_id(environment):
    client, _ = environment
    rid = _report()
    assert asyncio.run(report_store.delete_report(rid)) is True
    response = client.get("/api/v1/backtesting/archive/task-1/reports")
    assert response.status_code == 200
    assert response.json() == {"reports": [], "total": 0}


def test_legacy_unlinked_and_other_sources_never_get_guessed(environment):
    client, _ = environment
    _report(subject="")
    _report(task="task-other")
    _report(source="portfolio_snapshot")
    response = client.get("/api/v1/backtesting/archive/task-1/reports")
    assert response.status_code == 200
    assert response.json() == {"reports": [], "total": 0}
