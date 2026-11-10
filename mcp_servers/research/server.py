"""Research-only MCP gateway: Hermes → Condor → Hummingbot → Dashboard.

This is deliberately *not* a second Condor MCP server. ``mcp_servers/condor``
mounts the agent's whole tool surface — code execution, routines authoring,
delegation, trading — because it is spawned for an agent seat. This one is
spawned for an external research caller and exposes six tools: discover what can
be backtested, submit the stock ``backtest_chart`` routine, read the run back,
list/read archived backtests, and read the backtest's report metadata.

Every call goes through ``call_main_api`` — the existing loopback HTTP path that
mints a JWT from this process's identity — so a run belongs to the main process
and therefore shows up in the dashboard exactly like one started from chat. The
tool surface is the permission boundary: there is no generic ``run_routine``,
no ``run_code``, no schedules and no order placement, so a prompt cannot talk
this process into a wider action than the six below.

Only ``backtest_chart`` is accepted, and only while it is still the shipped
(``source == "global"``) definition: a local or agent routine that shadows the
name is different code, and this gateway refuses to run code it cannot name.
"""

from __future__ import annotations

import functools
import json
import math
import re
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import quote

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from mcp_servers.condor.condor_client import call_main_api
from mcp_servers.condor.exceptions import APIError

#: The one routine this gateway is allowed to start. The dashboard's backtest
#: archive, the run's report and `backtest_compare` all key off this name, which
#: is why the external caller must go through it rather than re-implement it.
ROUTINE_NAME = "backtest_chart"

#: First-version resource bound, not a performance judgement: a wider window is
#: a longer candle fetch inside one tool call, and this gateway is not the place
#: to discover that limit with the user waiting.
MAX_WINDOW_DAYS = 31

RESOLUTIONS = ("1m", "5m", "15m", "1h")

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_INSTRUCTIONS = """\
Research gateway to a Condor install. Use `research_catalog` first to list the
servers and controller configs this install can backtest, then `submit_backtest`
with a config name and a date window. Submission is asynchronous: it returns an
instance id; read it with `backtest_status`, and give the human the returned
dashboard URL. Finished runs are also in `list_backtests` / `get_backtest`, and
the interactive chart is a report readable with `get_report`.

This surface is read-only apart from starting a backtest. It cannot place
orders, execute arbitrary code, create routines, schedule anything, or change
server settings. Do not claim a run succeeded until `backtest_status` reports a
terminal status with no error.
"""

mcp = MCPServer("condor_research", instructions=_INSTRUCTIONS)


def _short(text: str, limit: int = 300) -> str:
    """One line, bounded — tool errors are read by a model, not a log viewer."""
    flat = " ".join(str(text).split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


def _tool(fn):
    """Register a coroutine as an MCP tool, surfacing real error text.

    MCP SDK >= 2.1 turns an ordinary exception into ``Error executing tool
    <name>``, which tells the caller nothing it can act on. Re-raising as the
    SDK's ``ToolError`` keeps the message; everything else is flattened to one
    bounded line so a traceback or a response body never becomes the message.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except ToolError:
            raise
        except APIError as exc:
            raise ToolError(_short(f"{fn.__name__}: {exc}")) from exc
        except Exception as exc:  # noqa: BLE001 - the boundary of the server
            raise ToolError(_short(f"{fn.__name__} failed: {exc}")) from exc

    mcp.tool()(wrapper)
    return wrapper


def _clean_name(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ToolError(f"{field} must be a string")
    text = value.strip()
    if not text:
        raise ToolError(f"{field} is required")
    if ".." in text or not _NAME_RE.fullmatch(text):
        raise ToolError(f"{field} contains characters this gateway does not accept")
    return text


def _clean_id(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ToolError(f"{field} must be a string")
    text = value.strip()
    if not text:
        raise ToolError(f"{field} is required")
    if ".." in text or not _ID_RE.fullmatch(text):
        raise ToolError(f"{field} is not a valid identifier")
    return text


def _clean_date(value: Any, field: str) -> date:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value.strip()):
        raise ToolError(f"{field} must be YYYY-MM-DD")
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ToolError(f"{field} is not a real date: {value!r}") from exc


def _validate_window(start_date: Any, end_date: Any) -> tuple[str, str]:
    start = _clean_date(start_date, "start_date")
    end = _clean_date(end_date, "end_date")
    if start >= end:
        raise ToolError("start_date must be before end_date")
    if end > datetime.now(timezone.utc).date():
        raise ToolError("end_date cannot be in the future")
    if (end - start).days > MAX_WINDOW_DAYS:
        raise ToolError(
            f"the window may not exceed {MAX_WINDOW_DAYS} days in this version"
        )
    return start.isoformat(), end.isoformat()


def _validate_resolution(value: Any) -> str:
    if value not in RESOLUTIONS:
        raise ToolError(f"resolution must be one of {list(RESOLUTIONS)}")
    return str(value)


def _validate_trade_cost(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError("trade_cost must be a number")
    number = float(value)
    if not math.isfinite(number) or number < 0 or number > 0.1:
        raise ToolError("trade_cost must be a finite decimal between 0 and 0.1")
    return number


def _validate_limit(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise ToolError("limit must be an integer between 1 and 100")
    return value


def _dashboard_url() -> str:
    from utils.config import WEB_PORT

    return f"http://127.0.0.1:{WEB_PORT}"


async def _routine_row() -> dict:
    """The discovered ``backtest_chart`` row — or a refusal to run anything."""
    routines = await call_main_api("GET", "/routines")
    row = None
    if isinstance(routines, list):
        row = next(
            (
                entry
                for entry in routines
                if isinstance(entry, dict) and entry.get("name") == ROUTINE_NAME
            ),
            None,
        )
    if row is None:
        raise ToolError(f"{ROUTINE_NAME} is not available on this install")
    if row.get("pending"):
        raise ToolError(
            f"{ROUTINE_NAME} is waiting for operator consent on this install"
        )
    if row.get("source") != "global":
        raise ToolError(
            f"{ROUTINE_NAME} is shadowed by a local definition "
            f"(source={row.get('source')!r}); refusing to run code this gateway "
            "cannot identify. Run it from the dashboard instead."
        )
    return row


def _summary_row(summary: dict) -> dict:
    """An archived run reduced to what a research caller reads.

    ``GET /backtesting/archive`` speaks the dashboard's own shape: the run's
    parameters live under ``config.config`` and ``config.start_time``, not as
    top-level ``config_id`` fields. Reading the flat names here is what made an
    earlier version report every archived run with an empty config name.
    """
    params = summary.get("config")
    params = params if isinstance(params, dict) else {}
    controller = params.get("config")
    controller = controller if isinstance(controller, dict) else {}

    return {
        "task_id": summary.get("task_id", ""),
        "status": summary.get("status") or "completed",
        "server": summary.get("server", ""),
        "config_name": controller.get("id") or summary.get("config_id", ""),
        "controller": controller.get("controller_name")
        or summary.get("controller", ""),
        "trading_pair": controller.get("trading_pair")
        or summary.get("trading_pair", ""),
        "connector": controller.get("connector_name") or summary.get("connector", ""),
        "start_time": params.get("start_time") or summary.get("start_time"),
        "end_time": params.get("end_time") or summary.get("end_time"),
        "resolution": params.get("backtesting_resolution")
        or summary.get("resolution", ""),
        "trade_cost": (
            params.get("trade_cost")
            if params.get("trade_cost") is not None
            else summary.get("trade_cost")
        ),
        "metrics": summary.get("metrics") or {},
        "has_payload": summary.get("has_payload", True),
        "created_at": summary.get("created_at"),
        "completed_at": summary.get("completed_at"),
        "error": summary.get("error"),
    }


@_tool
async def research_catalog(server_name: str = "local") -> dict:
    """List what this install can backtest: servers, controller configs, readiness.

    Call this before the first `submit_backtest` — the config name it returns is
    the one the Hummingbot server actually knows. Only server names and
    reachability are exposed; connection details never leave the main process.
    """
    server = _clean_name(server_name, "server_name")
    try:
        await _routine_row()
        ready, reason = True, ""
    except ToolError as exc:
        # Discovery should still show what exists when submission is refused;
        # the caller needs the reason, not an empty failure.
        ready, reason = False, str(exc)

    servers = await call_main_api("GET", "/servers")
    names = [
        {"name": entry.get("name", ""), "online": bool(entry.get("online"))}
        for entry in (servers if isinstance(servers, list) else [])
        if isinstance(entry, dict) and entry.get("name")
    ]

    options = await call_main_api(
        "GET", f"/routines/options/controller_configs?server={quote(server, safe='')}"
    )
    configs = options.get("options", []) if isinstance(options, dict) else []

    return {
        "server": server,
        "servers": names,
        "controller_configs": [c for c in configs if isinstance(c, str)][:200],
        "backtest_routine": ROUTINE_NAME,
        "backtest_available": ready,
        "reason": reason,
        "dashboard_url": _dashboard_url(),
    }


@_tool
async def submit_backtest(
    config_name: str,
    start_date: str,
    end_date: str,
    server_name: str = "local",
    resolution: str = "1m",
    trade_cost: float = 0.0002,
) -> dict:
    """Start a backtest of one saved controller config and return its run id.

    The run is created in the Condor main process, not here, so it survives this
    call and appears in the dashboard. Read it with `backtest_status`; a chart
    report is produced on completion and can be opened with `get_report`.
    """
    config = _clean_name(config_name, "config_name")
    server = _clean_name(server_name, "server_name")
    start, end = _validate_window(start_date, end_date)
    granularity = _validate_resolution(resolution)
    cost = _validate_trade_cost(trade_cost)
    await _routine_row()

    started = await call_main_api(
        "POST",
        "/routines/run",
        {
            "routine_name": ROUTINE_NAME,
            "server_name": server,
            "config": {
                "config_name": config,
                "start_date": start,
                "end_date": end,
                "resolution": granularity,
                "trade_cost": cost,
                # No PNG pushed to a chat, but the interactive chart still goes
                # into the web report (see the routine's `_render`).
                "chart": False,
            },
            "attribute_to": "condor",
            # "notify" with no session_key: there is no conversation to notify,
            # so the run leaves no chat message behind.
            "on_complete": "notify",
        },
    )
    instance_id = started.get("instance_id") if isinstance(started, dict) else None
    if not instance_id:
        raise ToolError(_short(f"backtest did not start: {started}"))

    return {
        "instance_id": instance_id,
        "status": "running",
        "server": server,
        "config_name": config,
        "start_date": start,
        "end_date": end,
        "resolution": granularity,
        "trade_cost": cost,
        "dashboard_url": f"{_dashboard_url()}/routines?run={quote(str(instance_id), safe='')}",
        "note": "Started. Read it with backtest_status; do not claim a result yet.",
    }


@_tool
async def backtest_status(instance_id: str) -> dict:
    """Read a backtest run: status, error, metrics table and its report id."""
    iid = _clean_id(instance_id, "instance_id")
    instance = await call_main_api("GET", f"/routines/instances/{quote(iid, safe='')}")
    if not isinstance(instance, dict):
        raise ToolError(_short(f"instance {iid!r} returned no record"))
    if instance.get("routine_name") != ROUTINE_NAME:
        raise ToolError("this gateway only reads backtest_chart runs")

    table = instance.get("table_data")
    if table:
        # The metrics table is one row; bound it anyway so a future routine
        # cannot turn a status read into an unbounded response.
        table = _short(json.dumps(table, ensure_ascii=False), 2000)

    return {
        "instance_id": iid,
        "routine": ROUTINE_NAME,
        "status": instance.get("status", "unknown"),
        "error": instance.get("error"),
        "has_result": bool(instance.get("has_result")),
        # ``has_chart`` on an instance means the PNG pushed to a chat; this
        # gateway asks for chart=False, so the interactive chart lives in the
        # web report instead. Say which one exists rather than reporting a
        # successful run as chartless.
        "has_chart_image": bool(instance.get("has_chart")),
        "has_report": bool(instance.get("report_id")),
        "result_text": _short(instance.get("result_text") or "", 2000),
        "table_data": table,
        "report_id": instance.get("report_id"),
        "dashboard_url": f"{_dashboard_url()}/routines?run={quote(iid, safe='')}",
    }


@_tool
async def list_backtests(server_name: str = "local", limit: int = 20) -> dict:
    """List archived backtests for one server, newest first, without payloads."""
    server = _clean_name(server_name, "server_name")
    count = _validate_limit(limit)
    archive = await call_main_api("GET", "/backtesting/archive")
    summaries = archive.get("summaries", []) if isinstance(archive, dict) else []
    rows = [
        _summary_row(entry)
        for entry in summaries
        if isinstance(entry, dict) and entry.get("server") == server
    ]
    rows.sort(key=lambda row: row.get("created_at") or 0, reverse=True)
    return {
        "server": server,
        "total": len(rows),
        "returned": min(count, len(rows)),
        "backtests": rows[:count],
    }


@_tool
async def get_backtest(task_id: str) -> dict:
    """Read one archived backtest's parameters and metrics.

    The run's chart payload can be very large, so this returns the index summary
    only; open the dashboard to read the chart, or use `get_report` when the run
    produced a report id.
    """
    tid = _clean_id(task_id, "task_id")
    archive = await call_main_api("GET", "/backtesting/archive")
    summaries = archive.get("summaries", []) if isinstance(archive, dict) else []
    for entry in summaries:
        if isinstance(entry, dict) and entry.get("task_id") == tid:
            return _summary_row(entry)
    raise ToolError(f"backtest {tid!r} is not in the archive")


@_tool
async def get_report(report_id: str) -> dict:
    """Read the metadata of a backtest's interactive report and its dashboard link."""
    rid = _clean_id(report_id, "report_id")
    report = await call_main_api("GET", f"/reports/{quote(rid, safe='')}")
    if not isinstance(report, dict):
        raise ToolError(_short(f"report {rid!r} returned no record"))
    if report.get("source_name") != ROUTINE_NAME:
        raise ToolError("this gateway only reads backtest_chart reports")
    return {
        "report_id": report.get("id", rid),
        "title": report.get("title", ""),
        "created_at": report.get("created_at", ""),
        "source_type": report.get("source_type", ""),
        "source_name": report.get("source_name", ""),
        "tags": report.get("tags") or [],
        "subject": report.get("subject", ""),
        "agent": report.get("agent", ""),
        "dashboard_url": f"{_dashboard_url()}/routines?report={quote(rid, safe='')}",
    }


#: The whole surface, so a test can pin it and a reviewer can read it in one
#: glance. Nothing else is registered on this server.
TOOL_NAMES = (
    "research_catalog",
    "submit_backtest",
    "backtest_status",
    "list_backtests",
    "get_backtest",
    "get_report",
)


if __name__ == "__main__":
    mcp.run()
