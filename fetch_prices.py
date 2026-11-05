#!/usr/bin/env python3
"""
Daily closing prices for the AI Trading Agents Competition.

Runs on GitHub Actions (which can reach Yahoo Finance; the competition's cloud
workspace can't). Downloads daily closes for a broad universe of US and Canadian
stocks and ETFs plus the USD/CAD rate, and saves them as JSON in this repo:

  data/closes/YYYY-MM.json   {"YYYY-MM-DD": {"SYMBOL": close, ...}, ...}
  data/fx/YYYY-MM.json       {"YYYY-MM-DD": usdcad, ...}
  data/latest.json           most recent close for every symbol + run info
  data/universe.json         the symbol list used, with where each came from

Closes are the exchange's actual daily close (split-adjusted, NOT dividend-
adjusted). Symbols use Yahoo's format, which matches the competition's:
TSX = "RY.TO", class shares use a hyphen ("BRK-B", "BBD-B.TO").

Usage: python fetch_prices.py [--days 10] [--backfill]   (backfill = 1 year)
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

ROOT = Path(__file__).parent
DATA = ROOT / "data"
UA = {"User-Agent": "trading-prices/1.0 (personal hobby project; GitHub Actions)"}

# Popular ETFs the agents are likely to use (US and Canadian). Extend freely.
ETFS = """
VTI VOO SPY IVV QQQ QQQM DIA IWM VEA VWO VXUS VT BND AGG TLT IEF SHY LQD HYG TIP GLD SLV
XLK XLF XLE XLV XLY XLP XLI XLB XLU XLRE XLC SMH SOXX VGT VHT VNQ VYM SCHD DGRO NOBL
MTUM QUAL USMV VLUE SPLV RSP ARKK IBIT
XIU.TO XIC.TO XEQT.TO VEQT.TO VGRO.TO XGRO.TO VBAL.TO XBAL.TO ZSP.TO VFV.TO XUS.TO XUU.TO
ZCN.TO VCN.TO XEF.TO XEC.TO ZEA.TO XAW.TO VXC.TO XBB.TO ZAG.TO VAB.TO XSB.TO ZFL.TO CASH.TO
ZEB.TO XEG.TO XFN.TO XIT.TO XRE.TO ZRE.TO XUT.TO ZUT.TO XMA.TO XGD.TO ZGD.TO CDZ.TO XEI.TO
VDY.TO ZDV.TO ZWB.TO ZWC.TO HXT.TO HXS.TO ZQQ.TO XQQ.TO TEC.TO
""".split()

EXTRA_FILE = ROOT / "extra_symbols.txt"   # one symbol per line; anything the agents hold that's missing


def wiki_tables(title: str) -> list[pd.DataFrame]:
    r = requests.get(f"https://en.wikipedia.org/wiki/{title}", headers=UA, timeout=30)
    r.raise_for_status()
    return pd.read_html(io.StringIO(r.text))


def sp500() -> list[str]:
    for t in wiki_tables("List_of_S%26P_500_companies"):
        if "Symbol" in t.columns:
            return [str(s).strip().replace(".", "-") for s in t["Symbol"]]
    raise RuntimeError("S&P 500 table not found")


def nasdaq100() -> list[str]:
    for t in wiki_tables("Nasdaq-100"):
        for col in ("Ticker", "Symbol"):
            if col in t.columns and len(t) > 80:
                return [str(s).strip().replace(".", "-") for s in t[col]]
    raise RuntimeError("Nasdaq-100 table not found")


def tsx_composite() -> list[str]:
    for t in wiki_tables("S%26P/TSX_Composite_Index"):
        for col in ("Symbol", "Ticker"):
            if col in t.columns and len(t) > 150:
                out = []
                for s in t[col]:
                    s = str(s).strip().upper()
                    s = s.split(":")[-1].strip()           # "TSX: RY" -> "RY"
                    s = s.replace(".", "-")                 # "BBD.B" -> "BBD-B"
                    if s and s != "NAN":
                        out.append(s if s.endswith("-TO") or s.endswith(".TO") else s + ".TO")
                return out
    raise RuntimeError("TSX Composite table not found")


def build_universe() -> dict[str, list[str]]:
    """Symbol -> list of sources. Falls back to the last saved universe if Wikipedia fails."""
    saved = {}
    if (DATA / "universe.json").exists():
        saved = json.loads((DATA / "universe.json").read_text()).get("symbols", {})
    uni: dict[str, list[str]] = {}

    def add(syms, src):
        for s in syms:
            uni.setdefault(s, [])
            if src not in uni[s]:
                uni[s].append(src)

    for name, fn in (("sp500", sp500), ("nasdaq100", nasdaq100), ("tsx_composite", tsx_composite)):
        try:
            add(fn(), name)
            print(f"universe: {name} ok")
        except Exception as e:  # keep yesterday's list for that index
            print(f"universe: {name} FAILED ({e}); reusing saved list", file=sys.stderr)
            add([s for s, src in saved.items() if name in src], name)
    add(ETFS, "etf_list")
    if EXTRA_FILE.exists():
        add([l.strip().upper() for l in EXTRA_FILE.read_text().splitlines()
             if l.strip() and not l.startswith("#")], "extra")
    return dict(sorted(uni.items()))


def download(symbols: list[str], start: dt.date, end: dt.date) -> pd.DataFrame:
    """Daily closes, columns = symbols, index = dates. Batched to be gentle on Yahoo."""
    frames = []
    for i in range(0, len(symbols), 150):
        chunk = symbols[i:i + 150]
        for attempt in range(3):
            try:
                df = yf.download(chunk, start=start.isoformat(), end=(end + dt.timedelta(days=1)).isoformat(),
                                 auto_adjust=False, actions=False, progress=False, threads=True,
                                 group_by="column")
                if df is not None and not df.empty:
                    close = df["Close"] if isinstance(df.columns, pd.MultiIndex) else df[["Close"]].rename(columns={"Close": chunk[0]})
                    frames.append(close)
                break
            except Exception as e:
                print(f"chunk {i}: attempt {attempt + 1} failed: {e}", file=sys.stderr)
                time.sleep(10 * (attempt + 1))
        time.sleep(2)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, axis=1)


def merge_monthly(folder: Path, rows: dict[str, dict]) -> int:
    """rows: date -> {key: value}. Merge into monthly files. Returns number of values added/changed."""
    folder.mkdir(parents=True, exist_ok=True)
    changed = 0
    by_month: dict[str, dict] = {}
    for d, vals in rows.items():
        by_month.setdefault(d[:7], {})[d] = vals
    for month, days in by_month.items():
        f = folder / f"{month}.json"
        cur = json.loads(f.read_text()) if f.exists() else {}
        for d, vals in days.items():
            slot = cur.setdefault(d, {}) if isinstance(vals, dict) else None
            if slot is None:
                if cur.get(d) != vals:
                    cur[d] = vals
                    changed += 1
                continue
            for k, v in vals.items():
                if slot.get(k) != v:
                    slot[k] = v
                    changed += 1
        f.write_text(json.dumps(dict(sorted(cur.items())), separators=(",", ":"), sort_keys=True))
    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10, help="calendar days to (re)download")
    ap.add_argument("--backfill", action="store_true", help="download one year of history")
    a = ap.parse_args()

    today = dt.datetime.now(dt.timezone.utc).date()
    start = today - dt.timedelta(days=370 if a.backfill else a.days)
    uni = build_universe()
    symbols = list(uni)
    print(f"{len(symbols)} symbols, {start} to {today}")

    closes = download(symbols, start, today)
    # Never save today's prices before the close (4pm Eastern + 30 min): mid-day prices aren't closes.
    et_now = dt.datetime.now(ZoneInfo("America/Toronto"))
    cutoff_day = et_now.date() if et_now.time() >= dt.time(16, 30) else et_now.date() - dt.timedelta(days=1)
    rows: dict[str, dict] = {}
    if not closes.empty:
        for ts, row in closes.iterrows():
            d = pd.Timestamp(ts).date().isoformat()
            if d > cutoff_day.isoformat():
                continue
            vals = {str(s): round(float(v), 4) for s, v in row.items() if pd.notna(v) and float(v) > 0}
            if vals:
                rows[d] = vals
    n_px = merge_monthly(DATA / "closes", rows)

    fx = download(["USDCAD=X"], start, today)
    fx_rows = {}
    if not fx.empty:
        for ts, row in fx.iterrows():
            v = row.iloc[0]
            d = pd.Timestamp(ts).date().isoformat()
            if pd.notna(v) and d <= cutoff_day.isoformat():
                fx_rows[d] = round(float(v), 5)
    n_fx = merge_monthly(DATA / "fx", fx_rows)

    # latest.json: newest close for each symbol across everything saved
    latest: dict[str, dict] = {}
    for f in sorted((DATA / "closes").glob("*.json")):
        for d, vals in sorted(json.loads(f.read_text()).items()):
            for s, v in vals.items():
                latest[s] = {"date": d, "close": v}
    fx_latest = {}
    for f in sorted((DATA / "fx").glob("*.json")):
        for d, v in sorted(json.loads(f.read_text()).items()):
            fx_latest = {"date": d, "usdcad": v}
    missing = sorted(s for s in symbols if s not in latest)
    (DATA / "latest.json").write_text(json.dumps({
        "updated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Yahoo Finance via yfinance (unofficial; personal use)",
        "note": "Daily closes, split-adjusted, not dividend-adjusted. History in data/closes/YYYY-MM.json.",
        "symbols": len(latest), "missing": missing, "usdcad": fx_latest,
        "closes": dict(sorted(latest.items())),
    }, indent=0, sort_keys=False))
    (DATA / "universe.json").write_text(json.dumps({"count": len(uni), "symbols": uni}, indent=0))
    print(f"saved {n_px} price values, {n_fx} fx values; {len(latest)} symbols with a close; {len(missing)} missing")
    if not latest:
        sys.exit("No prices at all: Yahoo may be blocking or yfinance may be broken.")


if __name__ == "__main__":
    main()
