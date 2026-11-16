# Local trading command center

The dashboard is a separate, read-only local HTTP service. It does not start a trading
runtime, approve risk, admit orders, settle funding, or mutate a journal. The browser
polls every 15 seconds. Paper and real account figures never share an account model.

## Start

Set a local `DASHBOARD_PASSWORD` environment variable, then run:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
python -B dashboard_server.py --shadow-journal runs/m13-shadow-2h-final-20261116.sqlite
```

Open `http://127.0.0.1:8765`. The server binds to loopback only. A different
`--shadow-journal` can be selected; the viewer requires its M13 source identity to
match the current code. The HTTP server never opens the journal's writer epoch.
Missing, pending, quarantined, malformed, or source-incompatible journals are shown
as unavailable/error states. Historic candle prices remain timestamped and are marked
stale once receipt is more than 10 minutes old. No price or balance is filled in.

The standalone command can show shadow market and M7 facts. It cannot reconstruct an
M8 actor from an arbitrary journal without taking ownership of the journal. To monitor
an **already running, approved paper process**, create the reader in that process and
pass its authoritative runtime object:

```python
reader = DashboardReader(shadow_path=shadow_path, paper_runtime=paper_runtime)
server = create_server(reader, dashboard_password)
```

The paper runtime must already exist and own its M8/M9.1 actors. Dashboard requests
only call the M8 snapshot reader and inspect immutable M8/M9.1 event histories. Reads
of a runtime with an unfinished frame, uncertain journal command, or changed head
fail closed. The dashboard does not make an inactive paper session active. No production
paper configuration is inferred from the shadow journal.

## Data contract

All JSON responses use `dashboard-v1` from `GET /api/state?mode=paper|real`. Focused
read routes are `/api/markets`, `/api/positions`, `/api/signals`, `/api/analytics`,
`/api/risk`, `/api/health`, `/api/trades`, and `/api/trades/{id}`. Trade history supports
`search`, `asset`, `side`, `result`, `start`, `end`, `page`, and `page_size`. A source
amount is serialized as a decimal string; missing values are JSON null. The frontend
uses those strings for display only and does not calculate accounting.

| Data | Authority | Missing-data behavior |
|---|---|---|
| BTC/ETH finalized prices and M7 decisions | APPLIED M13 shadow journal rows | Unavailable, stale, or error |
| Paper equity, cash, margin, positions, fees, funding | Connected M8 portfolio snapshot | Paper runtime not connected |
| Paper orders and fills | Connected M9.1 bridge lifecycle | No simulated orders |
| Paper trades and attribution | M9.1 fills joined to M8 booking snapshots | No completed trades |
| Real USDT balance and positions | Optional signed Binance USD-M account GET | Exchange account not connected |

Equity, realized and unrealized P&L are M8 values. Closed-trade gross/fees/funding
are derived from the matching M8 opening/closing snapshots and M9.1 fill records.
Trade attribution has a simulated exit label if no M12 exit reason is recorded.
Real account mode does not claim to know real trade history, stop/TP orders, realized
P&L, or account-wide holdings beyond the USDT asset and BTC/ETH positions returned by
the endpoint. The 24-hour price change is calculated from available finalized 5m
closes in the journal; it is absent without the matching previous-day close.
The dashboard does not claim that a signal is an executed trade.

## Real account monitoring boundary

Optional `DASHBOARD_BINANCE_READONLY_KEY` and `DASHBOARD_BINANCE_READONLY_SECRET`
configure one signed **GET** to Binance USD-M `/fapi/v2/account`. Use an API key with
read-only permission. The dashboard has no trading or withdrawal endpoint or route;
the Real Account selector only changes which account read model is displayed. No
credentials are returned to the browser. Invalid or incomplete responses result in
unavailable account data, never zero. Exchange retrieval is cached for 15 seconds.
The operator must configure and protect the key separately; this project does not
create or inspect key permissions. Official response contract:
https://developers.binance.com/en/docs/derivatives/usds-margined-futures/account/rest-api/Account-Information-V2

## Security and operational limits

Login requires `DASHBOARD_PASSWORD`. Only login/logout accept POST. Other POST paths
return 405. Session cookies are random, HttpOnly, SameSite Strict and expire after
eight hours. The service checks local Host and POST Origin, sends a restrictive CSP,
and binds `127.0.0.1`; deploy behind a reviewed TLS/auth proxy only after separate
authorization. The browser never receives an API secret. Dashboard failures do not
call or alter the trading actors. No real order execution is possible through this
service. The paper risk display reports the current M8 configuration, including the
user-selected 10% current-equity planned-loss limit, but cannot change it.

No historical paper trade is fabricated for the existing shadow-only journal. A
cross-process read-only paper-state snapshot service, live heartbeat, exchange trade
history, and real account chart history are outside this implementation. There is no
paper-trading readiness or profitability claim here.
