# Research MCP gateway (Hermes → Condor → Hummingbot → Dashboard)

`mcp_servers/research` is a **second, narrow MCP server** in this repository. It
is not a replacement for `mcp_servers/condor`: that one mounts an agent seat's
whole tool surface (code execution, routine authoring, delegation, trading) and
is spawned by Condor itself. This one is spawned by an external research caller
(Hermes) and exposes **six tools only**.

```
Hermes / external MCP host
        │  stdio MCP
        ▼
mcp_servers.research.server
        │  call_main_api()  (loopback HTTP + signed JWT + this process's identity)
        ▼
Condor main process  ── routine store / backtest archive / report store
        │  Hummingbot API (task API)
        ▼
Hummingbot backtest engine
        │
        ▼
Dashboard  /routines?run=<instance>   /routines?report=<report>
```

The run belongs to the Condor **main process**, not to the MCP subprocess, so it
survives the call, appears in the dashboard while it runs, and keeps its report
afterwards. That is the whole reason the gateway submits through Condor instead
of calling the Hummingbot API directly.

## Tools

| Tool | Kind | What it does |
| --- | --- | --- |
| `research_catalog` | read | Servers (name + online only), controller-config ids for a server, backtest readiness, dashboard base URL |
| `submit_backtest` | write (one routine) | Starts the stock `backtest_chart` routine; returns `instance_id` and a `?run=` link |
| `backtest_status` | read | Status, error, metrics table, `report_id`, `?run=` link. Refuses non-`backtest_chart` instances |
| `list_backtests` | read | Archived runs for one server, newest first, summary metrics only |
| `get_backtest` | read | One archived run's parameters + metrics from the archive index (never the chart payload) |
| `get_report` | read | Backtest report metadata + `?report=` link. Refuses reports from other sources |

There is deliberately **no** generic `run_routine`, `run_code`, scheduler,
delegation, or order tool. The mounted surface is the permission model: a prompt
cannot talk this process into an action that is not one of the six.

## Trust boundary

- **Fixed routine, stock code only.** `submit_backtest` always names
  `backtest_chart`. A local file can shadow a shipped routine and still reports
  `source == "global"`, so before every submission the gateway compares the
  source the main process actually resolved (`GET /routines/backtest_chart/source`)
  with `agents/_shared/routines/backtest_chart.py` on disk, byte for byte. A
  mismatch (or a missing stock file) is refused. A pending (consent-awaiting)
  routine is refused for the same reason.
- **No chat wake or message.** The run is submitted with `on_complete="notify"`
  and no `session_key`, so there is no conversation to notify. A per-user routine
  hook the operator configured earlier would still fire on completion — that is
  the operator's own configuration, not something this gateway adds.
- **`chart: false`.** No PNG is pushed anywhere; the interactive Plotly chart is
  still written into the web report (`report_id`).
- **Bounded inputs.** Names/ids reject `..`, `/`, `\`, `?`, `#`, `%` and control
  characters; dates must be real `YYYY-MM-DD`, `start < end`, not in the future,
  and at most **31 days** per run in this version; `resolution` is one of
  `1m/5m/15m/1h`; `trade_cost` must be finite and within `0..0.1`.
- **No payload reads.** `get_backtest` reads the archive index only. The stored
  payload of a run can be >100 MB; returning it over MCP would be both slow and
  useless to a model. `get_report` returns metadata, not the report HTML body.
- **Owner auth is unchanged.** Every call uses `call_main_api`, which mints a
  JWT for this process's identity (`.env` / `CONDOR_USER_ID`), so the main
  process's own owner checks still apply to instances, archive entries and
  reports.

## Launch

```bash
uv run --directory /path/to/condor --no-sync \
  python -m mcp_servers.research.server
```

Register with Hermes:

```bash
hermes mcp add condor_research \
  --command uv \
  --args run --directory /path/to/condor --no-sync \
  python -m mcp_servers.research.server
```

`uv run --directory` is load-bearing: `utils.config` finds `.env` relative to the
process's working directory, so the child must start in the repository. The
gateway needs the same identity the dashboard uses; with no identity, every call
reaches the main process as an unknown user and fails with 403.

## Dashboard links

Both link shapes are the real ones the SPA reads (`frontend/src/pages/Routines.tsx`):

- a run in flight or just finished: `/routines?run=<instance_id>`
- a finished report: `/routines?routine=backtest_chart&report=<report_id>`

(`/reports` itself is only a redirect to `/routines?tab=reports`; there is no
`/reports/<id>` page.)

## Verified end to end

- Hermes session → `research_catalog` → `controller_configs=["research_bollinger_v1"]`
- Hermes session → `list_backtests` → `67c00a73|research_bollinger_v1|6.82`
- `submit_backtest` over stdio MCP → Condor → Hummingbot: instance `2d5e3f99`
  completed, 57 executors, net PnL +6.82 USDC, 1m, BTC-USDT on
  `binance_perpetual` (config `research_bollinger_v1`, a demo config created for
  this verification).
- `backtest_status` returned `report_id=33fa4a`; `GET /reports/33fa4a/html`
  returned 200 with the Plotly chart; `/routines?report=33fa4a` returned the SPA.
- `tests/test_research_mcp.py`: 24 tests, including refusal paths.

## Known limits (first version)

- One controller config per run; no sweeps or parameter grids. Call the tool
  repeatedly if that is what you want.
- 31-day window and four resolutions; a wider request is refused rather than
  silently truncated.
- Backtests run on the Hummingbot task API and consume the server's candle
  fetches; rate limits and exchange availability are the engine's, not this
  gateway's.
- No report HTML over MCP. A model gets the metrics, the title and the link; the
  chart stays in the dashboard.
- **`task_id → report_id` is not persisted.** The report id lives on the routine
  instance in memory; after a Condor restart you can still read the archived
  run's parameters and metrics, but not rediscover its report id. Keep the report
  link (or take it from `backtest_status` while the run is known). Closing this
  needs a change in Condor itself (e.g. stamping the task id into the report
  subject), not a gateway tweak.
- The source check defends against an accidental local shadow, not against a
  same-user actor who can edit both the local routine and the stock file — the
  gateway runs with the operator's own identity by design.
- The demo controller config (`research_bollinger_v1`) is ordinary Hummingbot
  data and can be deleted from the dashboard when it is no longer useful.
