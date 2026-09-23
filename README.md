# Datastream extraction (incremental)

Pulls the RI (total return index) at monthly frequency for a fixed ticker list
and keeps a master file up to date. History is pulled once. After that, each run
only refreshes recent data, so you are not re-downloading everything every time.

## How it works

Two blocks.

**Monthly block (runs once).** Pulls RI at monthly frequency from `start_date`
and keeps completed months only, so the last row is the previous month (for a
run in September, the history ends at August). Written to
`datastream_RI_master.csv`. It does not run again unless the master is missing
or you pass `--full`.

**Daily block (runs every time).** Pulls the latest daily point per ticker and
puts it in as the current month's value. Each run first deletes any row dated in
the current calendar month, then inserts the new daily point at its actual date.
So within a month there is one rolling row: 2 Sep is deleted and replaced by
3 Sep, then 4 Sep, and so on.

When the month turns over, the last daily row of the old month is simply not in
the new month, so it stays put as that month's value and the new month starts
its own rolling row. No monthly re-pull, no other bookkeeping.

New tickers added to `config.ini` are back-seeded automatically on the next run.

Force a fresh full seed any time:

    python datastream_extract.py --full

## Files

- `datastream_extract.py` - the extraction.
- `config.ini` - login, tickers, dates, and the incremental settings. Edit this,
  not the script.
- `run_datastream.bat` - double-click to run on demand.
- `output/`
  - `datastream_RI_master.csv` - the authoritative accumulating dataset
    (dates down the rows, one column per ticker). This is the source of truth.
  - `datastream_RI_latest.xlsx` - a view with two sheets, `pivot` and `long`,
    rebuilt from the master each run. CSV versions are written instead if
    openpyxl is not installed.
  - `extract_log.txt` - what happened on each run.

## Config settings that matter

- `tickers` - comma-separated Datastream codes.
- `start_date` - first point for the one-time seed.
- `daily_lookback_days` - how far back to look for the latest daily point
  (covers weekends and holidays). Default 14.

## Running it

Double-click `run_datastream.bat`, or from a command prompt in this folder:

    python datastream_extract.py

## Requirements

    pip install DatastreamPy pytz openpyxl

DatastreamPy, pandas and pytz fetch and shape the data. openpyxl is optional and
only controls Excel vs CSV output.

## Notes

- Keep `config.ini` local. It holds your Datastream password.
- This runs on your PC. The Datastream endpoint is not reachable from Claude's
  cloud environment, so a cloud based Claude task cannot run it. For an
  unattended daily run, point Windows Task Scheduler at `run_datastream.bat`
  (remove the `pause` line from the bat first).
