#!/usr/bin/env python3
"""
Datastream RI extraction for Bluepoint Analytics.

Two blocks:

1. Monthly block (runs once, to build history). Pulls RI at monthly frequency
   from start_date and keeps completed months only, so the last row is the
   previous month. Written to output/datastream_RI_master.csv. It does not run
   again unless the master is missing or you pass --full.

2. Daily block (runs every time). Pulls the latest daily point per ticker and
   puts it in as the current month's value. Each run first deletes any row
   dated in the current calendar month, then inserts the new daily point. So
   within a month there is one rolling row: 2 Sep is replaced by 3 Sep, and so
   on. When the month turns over, the last daily row of the old month is left in
   place as that month's value and the new month starts its own rolling row.

3. Portfolio file for the app. Writes output/portfolio_data.xlsx with the
   sheets assets, prices, fx_rates, portfolio_holdings and benchmarks:
   - prices: the master in long form (date, ticker, price = RI).
   - assets: from portfolio_inputs.xlsx, with blanks (name, local_ccy, region)
     filled from Datastream static data.
   - fx_rates: fx_to_nzd for each non-NZD currency on each price date, from
     the free Frankfurter (ECB) FX service.
   - portfolio_holdings, benchmarks: copied from portfolio_inputs.xlsx.
   If portfolio_inputs.xlsx does not exist, a starter one is created.

Config is in config.ini next to this file. Edit that, not the code.
Force a fresh full seed:   python datastream_extract.py --full
"""

import configparser
import json
import os
import sys
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

try:
    import DatastreamPy as dsws
except ImportError:
    print("ERROR: DatastreamPy is not installed. Run:  pip install DatastreamPy pytz")
    sys.exit(1)

HERE = Path(__file__).resolve().parent


def today():
    # DS_TODAY_OVERRIDE is a test hook only; ignored in normal use.
    o = os.environ.get("DS_TODAY_OVERRIDE")
    return datetime.strptime(o, "%Y-%m-%d").date() if o else date.today()


def log(msg, logfile):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    try:
        with open(logfile, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def fetch_series(ds, ticker, field, start, end, freq):
    data = ds.get_data(tickers=ticker, fields=[field], kind=1,
                       start=start, end=end, freq=freq)
    s = data.iloc[:, 0]
    s.name = ticker
    # Datastream can return the index as strings; force real dates so every
    # comparison, sort and format downstream works.
    s.index = pd.to_datetime(s.index)
    return s.dropna()


# ---- Portfolio file for the app ----

ASSET_COLS = ["ticker", "name", "asset_type", "asset_class", "sector",
              "region", "local_ccy", "is_benchmark"]
HOLDING_COLS = ["portfolio_name", "effective_date", "ticker",
                "market_value_local", "local_ccy", "weight"]
BENCH_COLS = ["portfolio_name", "benchmark_ticker"]


def blank(v):
    return v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == ""


def fetch_static(ds, tickers, static_fields):
    """Datastream static values: {ticker: {field: value}}. Errors are skipped."""
    data = ds.get_data(tickers=",".join(tickers), fields=list(static_fields), kind=0)
    out = {}
    for _, r in data.iterrows():
        v = r["Value"]
        if blank(v) or str(v).startswith("$$"):
            continue
        out.setdefault(r["Instrument"], {})[r["Datatype"]] = str(v).strip()
    return out


def build_assets(inputs_assets, tickers, benchmark_tickers, static, defaults=True):
    """One row per ticker. Your values in portfolio_inputs.xlsx win; blanks are
    filled from Datastream static data and, if defaults, asset_type and
    is_benchmark (anything in the benchmarks sheet counts as a benchmark)."""
    given = {}
    if inputs_assets is not None:
        for _, r in inputs_assets.iterrows():
            if not blank(r.get("ticker")):
                given[str(r["ticker"]).strip()] = r.to_dict()
    order = list(given) + [t for t in tickers if t not in given]

    rows = []
    for t in order:
        row = {c: given.get(t, {}).get(c) for c in ASSET_COLS}
        row["ticker"] = t
        st = static.get(t, {})
        for col, key in (("name", "name"), ("local_ccy", "ccy"), ("region", "region")):
            if blank(row[col]):
                row[col] = st.get(key)
        if defaults:
            is_bench = t in benchmark_tickers or \
                str(row["is_benchmark"]).strip().lower() in ("true", "1", "yes")
            row["is_benchmark"] = is_bench
            if blank(row["asset_type"]):
                row["asset_type"] = "Benchmark" if is_bench else "Asset"
        rows.append(row)
    return pd.DataFrame(rows, columns=ASSET_COLS)


def fetch_fx(fx_url, ccy, base_ccy, start, end):
    """Daily rates from Frankfurter (ECB): units of base_ccy per 1 ccy."""
    url = f"{fx_url.rstrip('/')}/{start}..{end}?base={ccy}&symbols={base_ccy}"
    req = urllib.request.Request(url, headers={"User-Agent": "datastream-extract"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.load(resp)
    rates = {pd.Timestamp(d): v[base_ccy] for d, v in payload.get("rates", {}).items()
             if base_ccy in v}
    return pd.Series(rates, dtype=float).sort_index()


def build_fx(price_dates, ccys, base_ccy, fx_url, previous, logfile):
    """fx_to_<base> for each currency on each price date, using the last rate
    on or before that date (month ends can fall on weekends). If the download
    fails, that currency's rows from the previous portfolio file are kept."""
    rows = []
    fx_col = f"fx_to_{base_ccy.lower()}"
    if len(price_dates) == 0:
        return pd.DataFrame(columns=["date", "ccy", fx_col])
    start = (price_dates.min() - timedelta(days=10)).strftime("%Y-%m-%d")
    end = price_dates.max().strftime("%Y-%m-%d")
    for ccy in ccys:
        try:
            s = fetch_fx(fx_url, ccy, base_ccy, start, end)
            aligned = s.reindex(s.index.union(price_dates)).ffill().reindex(price_dates).dropna()
            for d, v in aligned.items():
                rows.append({"date": d, "ccy": ccy, "fx": float(v)})
            log(f"OK   FX {ccy}->{base_ccy}: {len(aligned)} dates", logfile)
        except Exception as e:
            old = previous[previous["ccy"] == ccy] if fx_col in previous else previous.iloc[0:0]
            for _, r in old.iterrows():
                rows.append({"date": pd.Timestamp(r["date"]), "ccy": ccy, "fx": r[fx_col]})
            log(f"FAIL FX {ccy}->{base_ccy}: {e}. Kept {len(old)} rows from last run.", logfile)
    fx = pd.DataFrame(rows, columns=["date", "ccy", "fx"])
    return fx.rename(columns={"fx": fx_col})


def write_portfolio_file(ds, master, tickers, cfg, logfile):
    in_path = HERE / cfg.get("portfolio", "input_file", fallback="portfolio_inputs.xlsx").strip()
    out_path = (HERE / cfg.get("output", "folder", fallback="output").strip() /
                cfg.get("portfolio", "output_file", fallback="portfolio_data.xlsx").strip())
    base_ccy = cfg.get("portfolio", "base_ccy", fallback="NZD").strip().upper()
    fx_url = cfg.get("portfolio", "fx_url", fallback="https://api.frankfurter.dev/v1").strip()
    static_fields = {
        "name": cfg.get("portfolio", "name_field", fallback="NAME").strip(),
        "ccy": cfg.get("portfolio", "ccy_field", fallback="ISOCUR").strip(),
        "region": cfg.get("portfolio", "region_field", fallback="GEOGN").strip(),
    }

    inputs = {}
    if in_path.exists():
        try:
            inputs = pd.read_excel(in_path, sheet_name=None, dtype=object)
        except Exception as e:
            log(f"FAIL portfolio: could not read {in_path.name} ({e}). Portfolio file not written.", logfile)
            return None
    holdings = inputs.get("portfolio_holdings", pd.DataFrame(columns=HOLDING_COLS))
    benchmarks = inputs.get("benchmarks", pd.DataFrame(columns=BENCH_COLS))
    bench_tickers = set(benchmarks["benchmark_ticker"].dropna().astype(str).str.strip()) \
        if "benchmark_ticker" in benchmarks else set()

    # Static data only for what is still blank, so a filled-in inputs file
    # costs no Datastream requests.
    static = {}
    assets = build_assets(inputs.get("assets"), tickers, bench_tickers, static)
    need = assets.loc[assets[["name", "local_ccy", "region"]].apply(lambda c: c.map(blank)).any(axis=1), "ticker"].tolist()
    if need:
        try:
            raw = fetch_static(ds, need, static_fields.values())
            static = {t: {k: raw.get(t, {}).get(f) for k, f in static_fields.items()} for t in need}
            log(f"OK   static data for {len(need)} assets", logfile)
        except Exception as e:
            log(f"WARN static data failed ({e}); fill name/local_ccy/region in {in_path.name}.", logfile)
    assets = build_assets(inputs.get("assets"), tickers, bench_tickers, static)

    if not in_path.exists():
        starter = build_assets(None, tickers, set(), static, defaults=False)
        with pd.ExcelWriter(in_path, engine="openpyxl") as xl:
            starter.to_excel(xl, sheet_name="assets", index=False)
            pd.DataFrame(columns=HOLDING_COLS).to_excel(xl, sheet_name="portfolio_holdings", index=False)
            pd.DataFrame(columns=BENCH_COLS).to_excel(xl, sheet_name="benchmarks", index=False)
        log(f"Created {in_path.name}: fill in asset_class, sector, holdings and benchmarks there.", logfile)

    # prices: the master in long form, dates as YYYY-MM-DD text like the template.
    prices = (master.reset_index()
                    .melt(id_vars="date", var_name="ticker", value_name="price")
                    .dropna(subset=["price"])
                    .sort_values(["ticker", "date"]))

    ccys = sorted({str(c).strip().upper() for c in assets["local_ccy"] if not blank(c)} - {base_ccy})
    previous_fx = pd.DataFrame(columns=["date", "ccy"])
    if out_path.exists():
        try:
            previous_fx = pd.read_excel(out_path, sheet_name="fx_rates")
        except Exception:
            pass
    fx = build_fx(pd.DatetimeIndex(master.index), ccys, base_ccy, fx_url, previous_fx, logfile)

    for df, col in ((prices, "date"), (fx, "date")):
        df[col] = pd.to_datetime(df[col]).dt.strftime("%Y-%m-%d")

    try:
        with pd.ExcelWriter(out_path, engine="openpyxl") as xl:
            assets.to_excel(xl, sheet_name="assets", index=False)
            prices.to_excel(xl, sheet_name="prices", index=False)
            fx.to_excel(xl, sheet_name="fx_rates", index=False)
            holdings.to_excel(xl, sheet_name="portfolio_holdings", index=False)
            benchmarks.to_excel(xl, sheet_name="benchmarks", index=False)
    except PermissionError:
        log(f"FAIL portfolio: {out_path.name} is open in Excel. Close it and run again.", logfile)
        return None
    log(f"PORTFOLIO: {out_path.name} written: {len(assets)} assets, {len(prices)} prices, "
        f"{len(fx)} fx rows ({', '.join(ccys) or 'none'})", logfile)
    return out_path.name


def main():
    force_full = "--full" in sys.argv

    cfg = configparser.ConfigParser()
    cfg_path = HERE / "config.ini"
    if not cfg_path.exists():
        print(f"ERROR: config.ini not found next to the script ({cfg_path}).")
        sys.exit(1)
    cfg.read(cfg_path, encoding="utf-8")

    username = cfg.get("credentials", "username").strip()
    password = cfg.get("credentials", "password").strip()
    tickers = [t.strip() for t in cfg.get("extraction", "tickers").split(",") if t.strip()]
    field = cfg.get("extraction", "fields", fallback="RI").split(",")[0].strip()
    start_date = cfg.get("extraction", "start_date", fallback="2000-01-31").strip()
    daily_lookback = cfg.getint("incremental", "daily_lookback_days", fallback=14)
    master_name = cfg.get("incremental", "master_file", fallback="datastream_RI_master.csv").strip()

    out_dir = HERE / cfg.get("output", "folder", fallback="output").strip()
    out_dir.mkdir(parents=True, exist_ok=True)
    logfile = out_dir / "extract_log.txt"
    master_path = out_dir / master_name

    tdy = today()
    end_date = tdy.strftime("%Y-%m-%d")
    cur_month_start = tdy.replace(day=1)

    try:
        ds = dsws.DataClient(None, username, password)
    except Exception as e:
        log(f"FATAL: could not connect to Datastream: {e}", logfile)
        sys.exit(1)

    master = None
    if master_path.exists() and not force_full:
        try:
            master = pd.read_csv(master_path, index_col=0, parse_dates=True).sort_index()
        except Exception as e:
            log(f"WARN: could not read master ({e}); will re-seed.", logfile)
            master = None

    ok, failed = 0, 0

    # ---- Block 1: monthly seed (only if no master yet) ----
    if master is None:
        log(f"MONTHLY SEED: {len(tickers)} tickers, {start_date} to {end_date}", logfile)
        cols = {}
        for t in tickers:
            try:
                s = fetch_series(ds, t, field, start_date, end_date, "M")
                cols[t] = s
                ok += 1
                log(f"OK   {t:<10} seed: {len(s)} monthly rows", logfile)
            except Exception as e:
                failed += 1
                log(f"FAIL {t:<10} seed: {e}", logfile)
        if not cols:
            log("ERROR: seed returned nothing. Nothing written.", logfile)
            sys.exit(1)
        master = pd.DataFrame(cols).sort_index()
        # Keep completed months only: drop anything in the current month.
        master = master[master.index < pd.Timestamp(cur_month_start)]
    else:
        # Back-seed any brand new tickers' completed history.
        for t in tickers:
            if t not in master.columns:
                try:
                    s = fetch_series(ds, t, field, start_date, end_date, "M")
                    s = s[s.index < pd.Timestamp(cur_month_start)]
                    for dt, v in s.items():
                        master.loc[pd.Timestamp(dt), t] = float(v)
                    log(f"OK   {t:<10} new ticker seeded: {len(s)} months", logfile)
                except Exception as e:
                    failed += 1
                    log(f"FAIL {t:<10} new ticker seed: {e}", logfile)

    # ---- Block 2: daily point for the current month ----
    # Delete any existing row dated in the current calendar month, then insert
    # the latest daily point. This is the single rolling current-month row.
    in_cur_month = (master.index >= pd.Timestamp(cur_month_start)) & \
                   (master.index <= pd.Timestamp(tdy) + pd.offsets.MonthEnd(0))
    if in_cur_month.any():
        master = master[~in_cur_month]

    daily_start = (tdy - timedelta(days=daily_lookback)).strftime("%Y-%m-%d")
    latest = {}   # ticker -> (date, value)
    for t in tickers:
        try:
            s = fetch_series(ds, t, field, daily_start, end_date, "D")
            if len(s):
                latest[t] = (pd.Timestamp(s.index[-1]).normalize(), float(s.iloc[-1]))
                ok += 1
            else:
                log(f"WARN {t:<10} no daily point in last {daily_lookback}d", logfile)
        except Exception as e:
            failed += 1
            log(f"FAIL {t:<10} daily: {e}", logfile)

    if latest:
        # One current-month row, labelled with the latest date seen.
        row_date = max(d for d, _ in latest.values())
        for t, (_, v) in latest.items():
            master.loc[row_date, t] = v
        log(f"DAILY: current-month row {row_date:%Y-%m-%d} set for {len(latest)} tickers", logfile)

    master = master.sort_index()
    master.index.name = "date"
    master.to_csv(master_path)

    # ---- Analyst-facing views ----
    long_df = (master.reset_index()
                     .melt(id_vars="date", var_name="ticker", value_name="value")
                     .dropna(subset=["value"])
                     .sort_values(["ticker", "date"])
                     .reset_index(drop=True))
    long_df["field"] = field
    long_df = long_df[["date", "ticker", "field", "value"]]

    wrote = [master_path.name]
    try:
        import openpyxl  # noqa: F401
        xlsx = out_dir / "datastream_RI_latest.xlsx"
        with pd.ExcelWriter(xlsx, engine="openpyxl") as xl:
            master.to_excel(xl, sheet_name="pivot")
            long_df.to_excel(xl, sheet_name="long", index=False)
        wrote.append(xlsx.name)
        name = write_portfolio_file(ds, master, tickers, cfg, logfile)
        if name:
            wrote.append(name)
    except ImportError:
        log("openpyxl not installed, writing CSV view instead (pip install openpyxl for Excel). "
            "portfolio_data.xlsx also needs openpyxl.", logfile)
        master.to_csv(out_dir / "datastream_RI_pivot_latest.csv")
        long_df.to_csv(out_dir / "datastream_RI_long_latest.csv", index=False)
        wrote += ["datastream_RI_pivot_latest.csv", "datastream_RI_long_latest.csv"]

    last_date = master.index.max()
    log(f"DONE: {ok} ok, {failed} failed. master={master.shape[0]} rows x "
        f"{master.shape[1]} tickers, last date {last_date:%Y-%m-%d}. Wrote {wrote}", logfile)


if __name__ == "__main__":
    main()
