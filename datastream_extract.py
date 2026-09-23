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

Config is in config.ini next to this file. Edit that, not the code.
Force a fresh full seed:   python datastream_extract.py --full
"""

import configparser
import os
import sys
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
    except ImportError:
        log("openpyxl not installed, writing CSV view instead (pip install openpyxl for Excel).", logfile)
        master.to_csv(out_dir / "datastream_RI_pivot_latest.csv")
        long_df.to_csv(out_dir / "datastream_RI_long_latest.csv", index=False)
        wrote += ["datastream_RI_pivot_latest.csv", "datastream_RI_long_latest.csv"]

    last_date = master.index.max()
    log(f"DONE: {ok} ok, {failed} failed. master={master.shape[0]} rows x "
        f"{master.shape[1]} tickers, last date {last_date:%Y-%m-%d}. Wrote {wrote}", logfile)


if __name__ == "__main__":
    main()
