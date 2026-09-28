"""Data-based voting 2027 – Eduskunta data pipeline.

Usage:
    python run.py                      # collect everything, build tables + analytics
    python run.py --steps votes rollcalls   # only some collection steps
    python run.py --no-collect         # rebuild tables from cached raw data
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

from edk import analytics, collectors, config, tables
from edk.client import Client


def save(tbls: dict[str, pd.DataFrame], out: Path, parquet: bool) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for name, df in tbls.items():
        if df is None or df.empty:
            continue
        df.to_csv(out / f"{name}.csv", index=False, encoding="utf-8-sig")  # utf-8-sig: opens cleanly in Excel
        if parquet:
            try:
                df.to_parquet(out / f"{name}.parquet", index=False)
            except Exception as e:  # pyarrow missing or mixed types
                logging.debug("parquet skipped for %s: %s", name, e)
        logging.info("%-36s %8d rows", name, len(df))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="data", help="output folder (default: data)")
    ap.add_argument("--steps", nargs="*", choices=list(collectors.STEPS), help="collection steps to run")
    ap.add_argument("--no-collect", action="store_true", help="skip downloading, rebuild from raw/")
    ap.add_argument("--user-agent", default=config.USER_AGENT, help="User-Agent incl. your contact")
    ap.add_argument("--parquet", action="store_true", help="also write Parquet files")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    out = Path(a.out)
    raw = out / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    if not a.no_collect:
        c = Client(user_agent=a.user_agent)
        for step in a.steps or list(collectors.STEPS):
            logging.info("== collecting: %s", step)
            try:
                collectors.STEPS[step](c, raw)
            except Exception:
                logging.exception("step %s failed – continuing with the others", step)

    logging.info("== building tables")
    tbls = tables.build_all(raw)
    save(tbls, out / "tables", a.parquet)
    logging.info("== building analytics")
    save(analytics.build_analytics(tbls), out / "analytics", a.parquet)
    logging.info("done -> %s", out.resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
