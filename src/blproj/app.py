"""
fx-treasury-alm dashboard backend.

Reads treasury_alm.db (written by main.py) in read-only mode and serves:

    GET /                    single-page dashboard
    GET /api/dashboard       latest exposure snapshot + risk history + latest/previous KPIs
    GET /api/exposure        latest snapshot of fx_exposure_monitor
    GET /api/risk-history    portfolio_risk_summary time series
    GET /healthz             liveness / database check

Run:
    pip install flask pandas
    python app.py                       # http://127.0.0.1:5000

Environment variables:
    TREASURY_DB_PATH    path to the SQLite file       (default: treasury_alm.db found in this folder or a parent)
    HISTORY_DEFAULT     default history rows returned (default: 250)
    HISTORY_MAX         hard cap on history rows      (default: 5000)
    REFRESH_SECONDS     browser auto-refresh interval (default: 30)
    HOST / PORT         bind address                  (default: 127.0.0.1 / 5000)
    FLASK_DEBUG         "1" enables the debugger      (default: off)

For production use a WSGI server instead of app.run, e.g.:
    waitress-serve --listen=0.0.0.0:5000 app:app
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Iterator

import pandas as pd
from flask import Flask, jsonify, render_template, request

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

BASE_DIR = Path(__file__).resolve().parent


def _find_db() -> Path:
    """TREASURY_DB_PATH wins; otherwise search this folder and its parents
    (so app.py can live in src/blproj while the DB sits at the repo root)."""
    env = os.environ.get("TREASURY_DB_PATH")
    if env:
        return Path(env)
    for folder in (BASE_DIR, *BASE_DIR.parents):
        candidate = folder / "treasury_alm.db"
        if candidate.exists():
            return candidate
    return BASE_DIR / "treasury_alm.db"  # not found yet; surfaces as a clear error banner


def _find_templates() -> Path:
    """Find templates/index.html in this folder or a parent (repo root)."""
    for folder in (BASE_DIR, *BASE_DIR.parents):
        if (folder / "templates" / "index.html").exists():
            return folder / "templates"
    return BASE_DIR / "templates"


class Config:
    DB_PATH = _find_db()
    HISTORY_DEFAULT = int(os.environ.get("HISTORY_DEFAULT", 250))
    HISTORY_MAX = int(os.environ.get("HISTORY_MAX", 5000))
    REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", 30))


EXPOSURE_TABLE = "fx_exposure_monitor"
SUMMARY_TABLE = "portfolio_risk_summary"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("fx-treasury-alm")


class DatabaseUnavailable(RuntimeError):
    """Raised when the SQLite file is missing or cannot be queried."""


# --------------------------------------------------------------------------- #
# Data access layer
# --------------------------------------------------------------------------- #

@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    """Open a read-only connection so the dashboard can never write to the DB
    or block main.py's writer."""
    db_path = Config.DB_PATH
    if not db_path.exists():
        raise DatabaseUnavailable(f"Database not found at {db_path}. Run main.py first.")
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as conn:
            yield conn
    except sqlite3.Error as exc:
        raise DatabaseUnavailable(f"SQLite error: {exc}") from exc


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    """DataFrame -> JSON-safe list of dicts (NaN/inf become null)."""
    return json.loads(df.to_json(orient="records"))


def fetch_latest_exposure(conn: sqlite3.Connection) -> dict[str, Any]:
    """Rows of fx_exposure_monitor belonging to the most recent Run_Timestamp.

    Run_Timestamp is stored as TEXT; ISO-8601 strings sort chronologically,
    so MAX() returns the latest run.
    """
    run_ts = conn.execute(f"SELECT MAX(Run_Timestamp) FROM {EXPOSURE_TABLE}").fetchone()[0]
    if run_ts is None:
        return {"run_timestamp": None, "rows": []}
    df = pd.read_sql_query(
        f"SELECT * FROM {EXPOSURE_TABLE} WHERE Run_Timestamp = ? ORDER BY rowid",
        conn,
        params=(run_ts,),
    )
    return {"run_timestamp": run_ts, "rows": _records(df)}


def fetch_risk_history(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    """Most recent `limit` rows of portfolio_risk_summary, oldest first (chart order)."""
    df = pd.read_sql_query(
        f"""
        SELECT * FROM (
            SELECT * FROM {SUMMARY_TABLE}
            ORDER BY Run_Timestamp DESC
            LIMIT ?
        ) ORDER BY Run_Timestamp ASC
        """,
        conn,
        params=(limit,),
    )
    return _records(df)


def build_dashboard_payload(history_limit: int) -> dict[str, Any]:
    with get_connection() as conn:
        exposure = fetch_latest_exposure(conn)
        history = fetch_risk_history(conn, history_limit)
    return {
        "exposure": exposure,
        "history": history,
        "latest": history[-1] if history else None,
        "previous": history[-2] if len(history) > 1 else None,
    }


def _history_limit() -> int:
    requested = request.args.get("limit", default=Config.HISTORY_DEFAULT, type=int)
    return max(1, min(requested, Config.HISTORY_MAX))


# --------------------------------------------------------------------------- #
# Flask app
# --------------------------------------------------------------------------- #

def create_app() -> Flask:
    app = Flask(__name__, template_folder=str(_find_templates()))

    @app.after_request
    def no_cache(response):
        # Risk numbers must never be served stale from a browser/proxy cache.
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(DatabaseUnavailable)
    def handle_db_error(exc: DatabaseUnavailable):
        log.error("%s", exc)
        return jsonify(error=str(exc)), 503

    @app.route("/")
    def index():
        try:
            initial = build_dashboard_payload(Config.HISTORY_DEFAULT)
            error = None
        except DatabaseUnavailable as exc:
            log.error("%s", exc)
            initial, error = None, str(exc)
        return render_template(
            "index.html",
            initial=initial,
            error=error,
            refresh_seconds=Config.REFRESH_SECONDS,
            history_default=Config.HISTORY_DEFAULT,
            history_max=Config.HISTORY_MAX,
        )

    @app.route("/api/dashboard")
    def api_dashboard():
        return jsonify(build_dashboard_payload(_history_limit()))

    @app.route("/api/exposure")
    def api_exposure():
        with get_connection() as conn:
            return jsonify(fetch_latest_exposure(conn))

    @app.route("/api/risk-history")
    def api_risk_history():
        with get_connection() as conn:
            return jsonify(fetch_risk_history(conn, _history_limit()))

    @app.route("/healthz")
    def healthz():
        with get_connection() as conn:
            conn.execute(f"SELECT 1 FROM {SUMMARY_TABLE} LIMIT 1")
        return jsonify(status="ok")

    return app


app = create_app()

if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", 5000)),
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
