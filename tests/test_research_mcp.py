"""The research gateway's contract: six tools, one routine, no escape hatch."""

import inspect
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mcp_servers.research import server as research

ROUTINES = [
    {
        "name": "backtest_chart",
        "source": "global",
        "pending": False,
        "is_continuous": False,
    }
]


def _api(monkeypatch, handler):
    """Stand in for the loopback call, recording every request."""
    calls = []

    async def fake(method, path, body=None, timeout=None):
        calls.append({"method": method, "path": path, "body": body})
        return handler(method, path, body)

    monkeypatch.setattr(research, "call_main_api", fake)
    return calls


def _submit_handler(method, path, body):
    if path == "/routines":
        return ROUTINES
    if path == "/routines/backtest_chart/source":
        return {"filename": "backtest_chart.py", "source": research._stock_source()}
    if path == "/routines/run":
        return {"instance_id": "i-abc123"}
    raise AssertionError(f"unexpected call {method} {path}")


@pytest.mark.asyncio
async def test_submit_posts_only_the_backtest_routine(monkeypatch):
    calls = _api(monkeypatch, _submit_handler)

    result = await research.submit_backtest(
        "bollinger_v1_1", "2025-04-22", "2025-04-23"
    )

    assert result["instance_id"] == "i-abc123"
    assert result["status"] == "running"
    assert "?run=i-abc123" in result["dashboard_url"]

    post = [c for c in calls if c["method"] == "POST"]
    assert len(post) == 1
    body = post[0]["body"]
    assert body["routine_name"] == "backtest_chart"
    assert body["server_name"] == "local"
    assert body["attribute_to"] == "condor"
    # No conversation behind this call: the run notifies nobody.
    assert body["on_complete"] == "notify"
    assert "session_key" not in body
    assert body["config"] == {
        "config_name": "bollinger_v1_1",
        "start_date": "2025-04-22",
        "end_date": "2025-04-23",
        "resolution": "1m",
        "trade_cost": 0.0002,
        "chart": False,
    }


@pytest.mark.asyncio
async def test_submit_refuses_a_locally_shadowed_routine(monkeypatch):
    """A local file shadowing the name keeps source='global', so the text must match."""
    calls = _api(
        monkeypatch,
        lambda method, path, body: (
            ROUTINES
            if path == "/routines"
            else (
                {"source": "# a locally shadowed backtest_chart\n"}
                if path == "/routines/backtest_chart/source"
                else {"instance_id": "x"}
            )
        ),
    )

    with pytest.raises(ToolError, match="shipped"):
        await research.submit_backtest("conf", "2025-04-22", "2025-04-23")

    assert not [c for c in calls if c["method"] == "POST"]


@pytest.mark.asyncio
async def test_submit_refuses_a_routine_awaiting_consent(monkeypatch):
    pending = [dict(ROUTINES[0], pending=True)]
    _api(monkeypatch, lambda method, path, body: pending if path == "/routines" else {})

    with pytest.raises(ToolError, match="consent"):
        await research.submit_backtest("conf", "2025-04-22", "2025-04-23")


@pytest.mark.parametrize(
    "overrides",
    [
        {"config_name": "../../etc/passwd"},
        {"config_name": "a/b"},
        {"config_name": ""},
        {"start_date": "2025-04-23", "end_date": "2025-04-22"},
        {"start_date": "2025-01-01", "end_date": "2025-04-01"},
        {"end_date": "2999-01-01"},
        {"start_date": "04/22/2025"},
        {"resolution": "2m"},
        {"trade_cost": float("nan")},
        {"trade_cost": float("inf")},
        {"trade_cost": -0.1},
        {"trade_cost": 0.5},
    ],
)
@pytest.mark.asyncio
async def test_submit_rejects_bad_input_before_it_reaches_condor(
    monkeypatch, overrides
):
    calls = _api(monkeypatch, _submit_handler)
    kwargs = {
        "config_name": "conf",
        "start_date": "2025-04-22",
        "end_date": "2025-04-23",
        **overrides,
    }

    with pytest.raises(ToolError):
        await research.submit_backtest(**kwargs)

    assert calls == []


@pytest.mark.asyncio
async def test_status_reads_a_run_back(monkeypatch):
    def handler(method, path, body):
        assert path == "/routines/instances/i-1"
        return {
            "instance_id": "i-1",
            "routine_name": "backtest_chart",
            "status": "completed",
            "error": None,
            "has_result": True,
            "has_chart": True,
            "result_text": "net pnl 1.23",
            "table_data": [{"task_id": "t-1", "sharpe_ratio": 2.5}],
            "report_id": "r-1",
        }

    _api(monkeypatch, handler)
    result = await research.backtest_status("i-1")

    assert result["status"] == "completed"
    assert result["report_id"] == "r-1"
    assert result["has_report"] is True
    assert "sharpe_ratio" in result["table_data"]
    assert "?run=i-1" in result["dashboard_url"]


@pytest.mark.asyncio
async def test_status_refuses_a_non_backtest_instance(monkeypatch):
    _api(
        monkeypatch,
        lambda method, path, body: {
            "routine_name": "some_trading_routine",
            "status": "running",
        },
    )

    with pytest.raises(ToolError, match="backtest_chart"):
        await research.backtest_status("i-1")


@pytest.mark.asyncio
async def test_list_backtests_filters_by_server_and_caps(monkeypatch):
    summaries = [
        {"task_id": "t1", "server": "local", "created_at": 1, "metrics": {"a": 1}},
        {"task_id": "t2", "server": "local", "created_at": 3, "metrics": {}},
        {"task_id": "t3", "server": "local", "created_at": 2, "metrics": {}},
        {"task_id": "t4", "server": "other", "created_at": 4, "metrics": {}},
    ]
    _api(monkeypatch, lambda method, path, body: {"summaries": summaries})

    result = await research.list_backtests("local", limit=2)

    assert result["total"] == 3
    assert [row["task_id"] for row in result["backtests"]] == ["t2", "t3"]


@pytest.mark.asyncio
async def test_get_backtest_never_pulls_the_chart_payload(monkeypatch):
    calls = _api(
        monkeypatch,
        lambda method, path, body: (
            {
                "summaries": [
                    {"task_id": "t9", "server": "local", "metrics": {"net_pnl": 1}}
                ]
            }
            if path == "/backtesting/archive"
            else {"reports": [], "total": 0}
        ),
    )

    result = await research.get_backtest("t9")

    assert result["task_id"] == "t9"
    assert [c["path"] for c in calls] == [
        "/backtesting/archive",
        "/backtesting/archive/t9/reports",
    ]
    assert result["report_id"] is None  # Legacy entries are not guessed.


@pytest.mark.asyncio
async def test_archive_summary_reads_the_dashboards_nested_shape(monkeypatch):
    """The archive route nests parameters under config.config, not flat keys."""
    entry = {
        "task_id": "t9",
        "server": "local",
        "config": {
            "config": {
                "id": "research_bollinger_v1",
                "controller_name": "bollinger_v1",
                "trading_pair": "BTC-USDT",
                "connector_name": "binance_perpetual",
            },
            "start_time": 100,
            "end_time": 200,
            "backtesting_resolution": "1m",
            "trade_cost": 0.0002,
        },
        "metrics": {"net_pnl": 1},
    }
    _api(
        monkeypatch,
        lambda method, path, body: (
            {"summaries": [entry]}
            if path == "/backtesting/archive"
            else {"reports": [], "total": 0}
        ),
    )

    result = await research.get_backtest("t9")

    assert result["config_name"] == "research_bollinger_v1"
    assert result["controller"] == "bollinger_v1"
    assert result["trading_pair"] == "BTC-USDT"
    assert result["connector"] == "binance_perpetual"
    assert result["start_time"] == 100
    assert result["resolution"] == "1m"


@pytest.mark.asyncio
async def test_get_backtest_recovers_report_without_a_live_instance(monkeypatch):
    def handler(method, path, body):
        if path == "/backtesting/archive":
            return {"summaries": [{"task_id": "t9", "server": "local"}]}
        if path == "/backtesting/archive/t9/reports":
            return {
                "reports": [{"id": "r-9", "source_name": "backtest_chart"}],
                "total": 1,
            }
        raise AssertionError(path)

    calls = _api(monkeypatch, handler)
    result = await research.get_backtest("t9")
    assert result["report_id"] == "r-9"
    assert "routine=backtest_chart&report=r-9" in result["dashboard_url"]
    assert all(c["method"] == "GET" for c in calls)
    assert not any("/instances/" in c["path"] for c in calls)


@pytest.mark.asyncio
async def test_get_report_refuses_a_report_from_another_source(monkeypatch):
    _api(
        monkeypatch,
        lambda method, path, body: {
            "id": "r-1",
            "source_name": "portfolio_snapshot",
            "source_type": "routine",
        },
    )

    with pytest.raises(ToolError, match="backtest_chart"):
        await research.get_report("r-1")


@pytest.mark.asyncio
async def test_get_report_returns_a_dashboard_link(monkeypatch):
    _api(
        monkeypatch,
        lambda method, path, body: {
            "id": "r-1",
            "title": "Backtest: conf",
            "created_at": "2025-04-23T00:00:00Z",
            "source_type": "routine",
            "source_name": "backtest_chart",
            "tags": ["backtest"],
            "subject": "",
            "agent": "condor",
        },
    )

    result = await research.get_report("r-1")

    assert result["title"] == "Backtest: conf"
    assert "routine=backtest_chart" in result["dashboard_url"]
    assert "report=r-1" in result["dashboard_url"]


@pytest.mark.asyncio
async def test_catalog_hides_connection_details(monkeypatch):
    def handler(method, path, body):
        if path == "/routines":
            return ROUTINES
        if path == "/routines/backtest_chart/source":
            return {"source": research._stock_source()}
        if path == "/servers":
            return [
                {
                    "name": "local",
                    "online": True,
                    "host": "10.0.0.5",
                    "port": 8000,
                    "username": "admin",
                    "password": "hunter2",
                }
            ]
        if path.startswith("/routines/options/controller_configs"):
            return {"options": ["conf_a", "conf_b"]}
        raise AssertionError(path)

    _api(monkeypatch, handler)
    result = await research.research_catalog("local")

    assert result["servers"] == [{"name": "local", "online": True}]
    assert result["controller_configs"] == ["conf_a", "conf_b"]
    assert result["backtest_available"] is True
    blob = json.dumps(result)
    assert "hunter2" not in blob and "10.0.0.5" not in blob


@pytest.mark.asyncio
async def test_catalog_still_lists_configs_when_submission_is_refused(monkeypatch):
    def handler(method, path, body):
        if path == "/routines":
            return ROUTINES
        if path == "/routines/backtest_chart/source":
            return {"source": "# shadowed\n"}
        if path == "/servers":
            return [{"name": "local", "online": True}]
        if path.startswith("/routines/options/controller_configs"):
            return {"options": ["conf_a"]}
        raise AssertionError(path)

    _api(monkeypatch, handler)
    result = await research.research_catalog("local")

    assert result["backtest_available"] is False
    assert "shipped" in result["reason"]
    assert result["controller_configs"] == ["conf_a"]


def test_the_surface_is_exactly_six_tools():
    assert set(research.TOOL_NAMES) == {
        "research_catalog",
        "submit_backtest",
        "backtest_status",
        "list_backtests",
        "get_backtest",
        "get_report",
    }
    for name in research.TOOL_NAMES:
        assert inspect.iscoroutinefunction(getattr(research, name))
    for forbidden in ("run_code", "manage_routines", "place_order", "delegate"):
        assert forbidden not in research.TOOL_NAMES
