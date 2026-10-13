import datetime
import sqlite3
import blpapi as blp
import numpy as np
import pandas

blpOptions = blp.SessionOptions()
blpOptions.setServerHost('localhost')
blpOptions.setServerPort(8194)

blpSession = blp.Session(blpOptions)

tickers = ["EURUSD Curncy", "EUR3M Curncy", "GBPUSD Curncy", "GBP3M Curncy", "USDJPY Curncy", "JPY3M Curncy", "SOFRRATE Index"]
flds = ["PX_LAST", "BID", "ASK", "CHG_PCT_1D", "CHG_PCT_5D", "FWD_SCALE"]
fwdPts = []

try:
    if not blpSession.start():
        raise ConnectionError("Failed to start session")
    if not blpSession.openService("//blp/refdata"):
        raise ConnectionError("Failed to open //blp/refdata")
    refDataService = blpSession.getService("//blp/refdata")

    # =========================================================================
    # 1. REFERENCE DATA REQUEST (Current Spot, 3M Fwd Points, SOFR)
    # =========================================================================
    request = refDataService.createRequest("ReferenceDataRequest")
    for i in tickers:
        request.append("securities", i)
    for i in flds:
        request.append("fields", i)

    blpSession.sendRequest(request)

    while True:
        event = blpSession.nextEvent(500)
        for msg in event:
            if msg.messageType() == blp.Name("ReferenceDataResponse"):
                for i in msg.getElement("securityData"):
                    ticker = i.getElementAsString("security")
                    if i.hasElement("securityError"):
                        raise ValueError(f"Bad Ticker: {ticker}")
                    
                    # Log field exceptions (e.g. FWD_SCALE on SOFRRATE Index)
                    fe = i.getElement("fieldExceptions")
                    if fe.numValues() > 0:
                        bad_fld = fe.getValueAsElement(0).getElementAsString("fieldId")
                        print(f"[VALIDATION WARN] {ticker} skipped unsupported field: {bad_fld}")

                    fld_data = i.getElement("fieldData")
                    row = {"security": ticker}
                    for f in flds:
                        if fld_data.hasElement(f):
                            row[f] = fld_data.getElementAsFloat(f)
                        else:
                            row[f] = None
                    fwdPts.append(row)

        if event.eventType() == blp.Event.RESPONSE:
            break

    # =========================================================================
    # 2. HISTORICAL DATA REQUEST (1-Year Daily Spot Rates for VaR)
    # =========================================================================
    hist_req = refDataService.createRequest("HistoricalDataRequest")
    spot_tickers = ["EURUSD Curncy", "GBPUSD Curncy", "USDJPY Curncy"]
    for s in spot_tickers:
        hist_req.append("securities", s)
    hist_req.append("fields", "PX_LAST")

    end_dt = datetime.date.today()
    start_dt = end_dt - datetime.timedelta(days=365)
    hist_req.set("startDate", start_dt.strftime("%Y%m%d"))
    hist_req.set("endDate", end_dt.strftime("%Y%m%d"))
    hist_req.set("periodicitySelection", "DAILY")

    blpSession.sendRequest(hist_req)
    hist_series = {}

    while True:
        event = blpSession.nextEvent(500)
        for msg in event:
            if msg.messageType() == blp.Name("HistoricalDataResponse"):
                sec_data = msg.getElement("securityData")
                tkr = sec_data.getElementAsString("security")
                dates, prices = [], []
                for bar in sec_data.getElement("fieldData"):
                    dates.append(bar.getElementAsString("date"))
                    prices.append(bar.getElementAsFloat("PX_LAST"))
                hist_series[tkr] = pandas.Series(prices, index=pandas.to_datetime(dates))

        if event.eventType() == blp.Event.RESPONSE:
            break

finally:
    blpSession.stop()

refDf = pandas.DataFrame(fwdPts).set_index("security")
histDf = pandas.DataFrame(hist_series).sort_index().ffill().dropna()

# =========================================================================
# 3. TREASURY ALM, CARRY & VALUE-AT-RISK (VaR) ENGINE
# =========================================================================
portfolio = {
    "EUR": {"spot_tkr": "EURUSD Curncy", "fwd_tkr": "EUR3M Curncy", "notional": 10_000_000, "usd_base": False},
    "GBP": {"spot_tkr": "GBPUSD Curncy", "fwd_tkr": "GBP3M Curncy", "notional": 8_000_000, "usd_base": False},
    "JPY": {"spot_tkr": "USDJPY Curncy", "fwd_tkr": "JPY3M Curncy", "notional": 1_500_000_000, "usd_base": True},
}

treasury_rows = []
daily_pnl_df = pandas.DataFrame(index=histDf.index[1:])

for ccy, cfg in portfolio.items():
    spot = refDf.loc[cfg["spot_tkr"], "PX_LAST"]
    chg_1d = refDf.loc[cfg["spot_tkr"], "CHG_PCT_1D"]
    chg_5d = refDf.loc[cfg["spot_tkr"], "CHG_PCT_5D"]
    fwd_pts = refDf.loc[cfg["fwd_tkr"], "PX_LAST"]
    scale = refDf.loc[cfg["fwd_tkr"], "FWD_SCALE"]
    notional = cfg["notional"]

    outright_3m = spot + (fwd_pts * 10 ** -scale)

    if not cfg["usd_base"]:
        unhedged_usd = notional * spot
        hedged_usd = notional * outright_3m
        hist_usd_val = histDf[cfg["spot_tkr"]] * notional
    else:
        unhedged_usd = notional / spot
        hedged_usd = notional / outright_3m
        hist_usd_val = notional / histDf[cfg["spot_tkr"]]

    hedge_carry_usd = hedged_usd - unhedged_usd
    ann_carry_bps = ((hedged_usd / unhedged_usd) - 1) * 4 * 10000

    # Historical Daily PnL vector (in USD) & 95% / 99% VaR
    daily_pnl = hist_usd_val.diff().dropna()
    daily_pnl_df[ccy] = daily_pnl
    var_95_usd = float(-np.percentile(daily_pnl, 5))
    var_99_usd = float(-np.percentile(daily_pnl, 1))

    treasury_rows.append({
        "CCY": ccy,
        "Notional": notional,
        "Spot": round(spot, 4),
        "1D_Chg%": round(chg_1d, 2),
        "5D_Chg%": round(chg_5d, 2),
        "Fwd_Pts": round(fwd_pts, 2),
        "Outright_3M": round(outright_3m, 4),
        "Unhedged_USD": round(unhedged_usd, 2),
        "Hedged_USD": round(hedged_usd, 2),
        "Carry_USD": round(hedge_carry_usd, 2),
        "Ann_Carry_Bps": round(ann_carry_bps, 2),
        "1D_VaR_95_USD": round(var_95_usd, 2),
        "1D_VaR_99_USD": round(var_99_usd, 2),
    })

treasuryDf = pandas.DataFrame(treasury_rows)

# Portfolio-level correlated (diversified) VaR vs standalone sum
portfolio_daily_pnl = daily_pnl_df.sum(axis=1)
undiversified_var_95 = float(treasuryDf["1D_VaR_95_USD"].sum())
diversified_var_95 = float(-np.percentile(portfolio_daily_pnl, 5))
diversified_var_99 = float(-np.percentile(portfolio_daily_pnl, 1))
diversification_benefit = undiversified_var_95 - diversified_var_95
sofr_rate = float(refDf.loc["SOFRRATE Index", "PX_LAST"])

# =========================================================================
# 4. SQLITE PERSISTENCE
# =========================================================================
run_ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
treasuryDf["Run_Timestamp"] = run_ts

summary_df = pandas.DataFrame([{
    "Run_Timestamp": run_ts,
    "Total_Unhedged_USD": round(float(treasuryDf["Unhedged_USD"].sum()), 2),
    "Total_Hedged_USD": round(float(treasuryDf["Hedged_USD"].sum()), 2),
    "Total_Carry_USD": round(float(treasuryDf["Carry_USD"].sum()), 2),
    "Undiversified_VaR_95_USD": round(undiversified_var_95, 2),
    "Diversified_VaR_95_USD": round(diversified_var_95, 2),
    "Diversified_VaR_99_USD": round(diversified_var_99, 2),
    "Diversification_Benefit_USD": round(diversification_benefit, 2),
    "SOFR_Rate_Pct": sofr_rate,
}])

with sqlite3.connect("treasury_alm.db") as conn:
    treasuryDf.to_sql("fx_exposure_monitor", conn, if_exists="append", index=False)
    summary_df.to_sql("portfolio_risk_summary", conn, if_exists="append", index=False)

print("\n=== CORPORATE TREASURY FX EXPOSURE & HEDGING CARRY TABLE ===")
print(treasuryDf.drop(columns=["Run_Timestamp"]).to_string(index=False))

print("\n=== PORTFOLIO VaR & ALM RISK SUMMARY (LOGGED TO SQLITE) ===")
print(summary_df.T.to_string(header=False))