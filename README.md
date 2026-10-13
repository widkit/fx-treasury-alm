# fx-treasury-alm

Corporate Treasury Asset and Liability Management (ALM) pipeline for FX exposure.

1. **Extraction** (`main.py`, uses `blpapi`): pulls live FX spot rates, forward points and history from Bloomberg, calculates 95%/99% historical Value-at-Risk (VaR) and forward carry, and logs each run to a local SQLite database, `treasury_alm.db`.
2. **Dashboard** (`src/blproj/app.py`): a read-only Flask web dashboard that visualizes what the extraction script has stored.

## Dashboard

- **KPI banner:** latest Total Carry, Diversified 1D VaR 95% and Diversification Benefit, with change versus the prior run.
- **Exposure monitor:** latest snapshot of `fx_exposure_monitor` with a portfolio totals row.
- **Chart:** history of Diversified VaR 95% against Total Carry from `portfolio_risk_summary`.
- Auto-refreshes every 30 seconds. Dark, Bloomberg Terminal-style UI built with Bootstrap 5 and Chart.js (loaded from CDNs, so the browser needs internet access).

## Layout

```
fx-treasury-alm/
├── templates/
│   └── index.html        # dashboard page
├── src/blproj/
│   └── app.py            # Flask backend
├── treasury_alm.db       # written by the extraction script
├── pyproject.toml
└── tests/
```

## Run the dashboard

The extraction script must have run at least once so `treasury_alm.db` has data.

```bash
poetry install
poetry run python src/blproj/app.py
```

Open http://127.0.0.1:5000. The dashboard opens the database read-only, so it is safe to run alongside the extraction script.

## Configuration

All optional, set as environment variables.

| Variable | Default | Purpose |
| --- | --- | --- |
| `TREASURY_DB_PATH` | `treasury_alm.db` found in `app.py`'s folder or a parent | Path to the SQLite file |
| `HOST` / `PORT` | `127.0.0.1` / `5000` | Bind address |
| `REFRESH_SECONDS` | `30` | Browser auto-refresh interval |
| `HISTORY_DEFAULT` / `HISTORY_MAX` | `250` / `5000` | Rows of history charted by default / hard cap |
| `FLASK_DEBUG` | off | Set to `1` for the Flask debugger (development only) |

## API

| Endpoint | Returns |
| --- | --- |
| `GET /` | Dashboard page |
| `GET /api/dashboard?limit=N` | Latest exposure, history, latest and previous summary rows |
| `GET /api/exposure` | Latest snapshot of `fx_exposure_monitor` |
| `GET /api/risk-history?limit=N` | `portfolio_risk_summary` time series, oldest first |
| `GET /healthz` | Database connectivity check |

## Database

| Table | Contents |
| --- | --- |
| `fx_exposure_monitor` | One row per currency per run: notional, spot, 1D/5D change, forward points, 3M outright, unhedged/hedged USD, carry, annualized carry (bps), 1D VaR 95/99 |
| `portfolio_risk_summary` | One row per run: portfolio totals, undiversified and diversified VaR, diversification benefit, SOFR rate |

`Run_Timestamp` is stored as text and must sort chronologically (ISO-8601 style, for example `2026-10-13 09:00:00`), because the dashboard picks the latest run with `MAX(Run_Timestamp)`.

## Production

Don't use the built-in Flask server. Use a WSGI server instead, for example:

```bash
poetry add waitress
cd src/blproj
poetry run waitress-serve --listen=127.0.0.1:5000 app:app
```

Before exposing it beyond localhost, put it behind authentication (for example a reverse proxy), since it shows treasury risk data. For locked-down environments, vendor the Bootstrap and Chart.js files instead of loading them from CDNs.
