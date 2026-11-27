#!/usr/bin/env python3
"""
DAILY REFEREE HELPER
====================
The scheduled referee task (a Claude session) does the parts that need the web and
the database. This script does everything that should be mechanical and exact:

  python3 daily.py plan   --data DATA [--now T] [--server-times ST.json]
      Tells the session what to look up today: which prices (symbol + dates),
      which new symbols need checking, whether to look up the exchange rate and
      dividends, and which new orders/runs need their database write time.

  python3 daily.py finish --data DATA --candidates CAND.json --out OUT
                          [--server-times ST.json] [--versions VER.json] [--now T]
      Checks the prices the session found (two sources must agree, or one dated
      source if the move is ordinary), merges them in, runs the referee, and writes
      ready-to-send database batches to OUT/batch_1.json, OUT/batch_2.json, ...
      If some documents already exist and their versions are missing from
      VER.json, it lists them and exits with code 2.

DATA is the folder the session exported the dashboard database into
(DATA/<collection>/<doc_id>.json).

CAND.json (written by the session from what it found on the web):
{
  "closes":    [{"date": "2026-11-02", "symbol": "VTI", "close": 370.00, "source": "stockanalysis.com"}],
  "fx":        [{"date": "2026-11-02", "usdcad": 1.3978, "source": "bankofcanada.ca"}],
  "symbols":   [{"symbol": "RY.TO", "name": "Royal Bank of Canada", "exchange": "TSX",
                 "currency": "CAD", "type": "stock", "source": "..."}],
  "dividends": [{"symbol": "RY.TO", "ex_date": "2026-10-24", "pay_date": "2026-11-24",
                 "amount": 1.64, "source": "..."}],
  "splits":    [{"symbol": "MTUM", "ex_date": "2026-11-05", "ratio": 4.0, "source": "..."}]
}
(Splits normally come from the price repo automatically; ratio 4.0 = 4-for-1, 0.1 = 1-for-10.)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import referee as R

PT = ZoneInfo("America/Vancouver")
AGREE = 0.005          # two sources must agree within 0.5%
SINGLE_MAX_MOVE = 0.08  # one source is enough unless the price moved more than 8% from the last close
TRUSTED = "yahoo"       # the trading-prices repo (yfinance via GitHub Actions): a direct data feed
TRUSTED_MAX_MOVE = 0.25 # a feed price is accepted alone unless it moved more than 25% in a day
REFRESH_DAYS = (0, 3)   # Monday and Thursday (Pacific): refresh every holding
BACKFILL_LIMIT = 15     # never ask for more than 15 trading days back per symbol


# ---------------------------------------------------------------------------
def weekdays(a: dt.date, b: dt.date) -> list[str]:
    out, d = [], a
    while d <= b:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def last_trading_day(now: dt.datetime) -> dt.date:
    """Most recent weekday whose 4pm Eastern close has passed (holidays not known)."""
    d = now.astimezone(R.ET).date()
    while d.weekday() >= 5 or R.close_time(d.isoformat()) > now:
        d -= dt.timedelta(days=1)
    return d


def load_json(path):
    return json.loads(Path(path).read_text()) if path else {}


def known_receipts(data: Path) -> tuple[set, set]:
    integ = R.load_collection(data, "integrity").get("main", {})
    o, r = set(integ.get("order_receipts", {})), set(integ.get("run_receipts", {}))
    for doc in R.load_collection(data, "receipts").values():
        o |= set(doc.get("orders", {}))
        r |= set(doc.get("runs", {}))
    return o, r


def close_index(prices: dict) -> dict[str, list[str]]:
    """symbol -> sorted dates that have a close."""
    idx: dict[str, list[str]] = {}
    for d, doc in prices.items():
        for sym, px in (doc.get("closes") or {}).items():
            if px is not None:
                idx.setdefault(sym.upper(), []).append(d)
    return {k: sorted(v) for k, v in idx.items()}


def load_price_repo(repo) -> tuple[dict, dict]:
    """Read the trading-prices repo: ({date: {SYM: close}}, {date: usdcad}). Empty if not given."""
    closes, fx = {}, {}
    if not repo:
        return closes, fx
    repo = Path(repo)
    for f in sorted((repo / "data" / "closes").glob("*.json")):
        for d, vals in json.loads(f.read_text()).items():
            closes.setdefault(d, {}).update({k.upper(): v for k, v in vals.items()})
    for f in sorted((repo / "data" / "fx").glob("*.json")):
        fx.update(json.loads(f.read_text()))
    return closes, fx


def load_repo_splits(repo) -> dict:
    """{SYM: {date: ratio}} from the trading-prices repo (empty if not given)."""
    if not repo:
        return {}
    f = Path(repo) / "data" / "splits.json"
    return {k.upper(): v for k, v in json.loads(f.read_text()).items()} if f.exists() else {}


PAY_LAG_DAYS = 7   # the feed has ex-dates but not pay dates; most ETFs pay within about a week


def load_repo_dividends(repo):
    """{SYM: {ex_date: amount}} from the trading-prices repo, or None if the repo has no dividend file."""
    if not repo:
        return None
    f = Path(repo) / "data" / "dividends.json"
    return {k.upper(): v for k, v in json.loads(f.read_text()).items()} if f.exists() else None


def repo_candidates(data: Path, repo, now: dt.datetime, extra_symbols=()) -> dict:
    """Every close the repo has for a known symbol on a usable day that isn't recorded yet."""
    closes, fx = load_price_repo(repo)
    if not repo:
        return {"closes": [], "fx": [], "splits": [], "dividends": []}
    S = {**R.DEFAULT_SETTINGS, **R.load_collection(data, "settings").get("main", {})}
    symbols = {k.upper() for k in R.load_collection(data, "symbols")} | {str(x).upper() for x in extra_symbols}
    prices = R.load_collection(data, "prices")
    ltd = last_trading_day(now).isoformat()
    out = {"closes": [], "fx": [], "splits": [], "dividends": []}
    known_divs = R.load_collection(data, "dividends")
    for sym, by_date in (load_repo_dividends(repo) or {}).items():
        for d, amt in by_date.items():
            if sym in symbols and S["season_start"] <= d <= ltd and f"{sym}-{d}" not in known_divs:
                pay = (dt.date.fromisoformat(d) + dt.timedelta(days=PAY_LAG_DAYS)).isoformat()
                out["dividends"].append({"symbol": sym, "ex_date": d, "pay_date": pay, "amount": amt,
                                         "source": TRUSTED, "pay_date_estimated": True})
    known_splits = R.load_collection(data, "splits")
    for sym, by_date in load_repo_splits(repo).items():
        for d, r in by_date.items():
            if sym in symbols and S["season_start"] <= d <= ltd and f"{sym}-{d}" not in known_splits:
                out["splits"].append({"symbol": sym, "ex_date": d, "ratio": r, "source": TRUSTED})
    for d, vals in closes.items():
        if not (S["season_start"] <= d <= ltd):
            continue
        have = (prices.get(d, {}).get("closes") or {})
        for sym, px in vals.items():
            if sym in symbols and sym not in have:
                out["closes"].append({"date": d, "symbol": sym, "close": px, "source": TRUSTED})
    for d, v in fx.items():
        if S["season_start"] <= d <= ltd and prices.get(d, {}).get("fx_usdcad") is None:
            out["fx"].append({"date": d, "usdcad": v, "source": TRUSTED})
    return out


# ---------------------------------------------------------------------------
def plan(data: Path, now: dt.datetime, server_times: dict, repo=None) -> dict:
    S = {**R.DEFAULT_SETTINGS, **R.load_collection(data, "settings").get("main", {})}
    season_start = dt.date.fromisoformat(S["season_start"])
    ltd = last_trading_day(now)
    today_pt = now.astimezone(PT).date()

    known_o, known_r = known_receipts(data)
    new_ids = [f"orders/{k}" for k in R.load_collection(data, "orders") if k not in known_o] + \
              [f"runs/{k}" for k in R.load_collection(data, "runs") if k not in known_r]
    missing_times = [k for k in new_ids if k not in server_times]

    res = R.rebuild(data, now, server_times)
    prices = R.load_collection(data, "prices")
    idx = close_index(prices)
    symbols = res["symbols"]

    def missing_for(sym: str, start: dt.date) -> list[str]:
        have = set(idx.get(sym, []))
        cal = R.calendar_for(sym, symbols)
        days = [d for d in weekdays(max(start, season_start), ltd)
                if d not in have and R.is_trading_day(d, cal, S)]
        return days[-BACKFILL_LIMIT:]

    needs: dict[str, set] = {}
    unknown = set()
    # 1) Orders waiting to fill: need the close for their fill day (and after, in case of holidays).
    for o in res["pending"]:
        sym = str(o.get("symbol", "")).upper()
        if sym not in symbols:
            unknown.add(sym)
            continue
        d = dt.date.fromisoformat(R.fill_date(o["received_at"], R.calendar_for(sym, symbols), S))
        needs.setdefault(sym, set()).update(missing_for(sym, d)[:5])
    # New orders the referee hasn't stamped yet also count (their symbols may be unknown).
    for k, o in R.load_collection(data, "orders").items():
        if k not in known_o:
            sym = str(o.get("symbol", "")).upper()
            if sym and sym not in symbols:
                unknown.add(sym)
                # If the symbol checks out, its fill-day close will be needed too.
                t = server_times.get(f"orders/{k}")
                recv = min(R.parse_time(t), now) if t else now
                d = dt.date.fromisoformat(R.fill_date(R.iso(recv), R.calendar_for(sym, symbols), S))
                needs.setdefault(sym, set()).update(missing_for(sym, d)[:5])

    # 2) Index fund control needs VTI from day one until it has bought.
    csym = S["control_symbol"].upper()
    if not res["ports"][R.CONTROL].fills:
        needs.setdefault(csym, set()).update(missing_for(csym, season_start)[:5])

    # 3) Refresh days: every holding (plus VTI) gets its missing closes filled in.
    refresh = today_pt.weekday() in REFRESH_DAYS
    held = sorted({s for p in res["ports"].values() for s in p.holdings} | {csym})
    if refresh:
        for sym in held:
            last = idx.get(sym, [])
            start = dt.date.fromisoformat(last[-1]) + dt.timedelta(days=1) if last else season_start
            m = missing_for(sym, start)
            if m:
                needs.setdefault(sym, set()).update(m)

    # 4) Exchange rate: any recent recorded day without one.
    recent = weekdays(max(season_start, ltd - dt.timedelta(days=14)), today_pt)
    fx_missing = [d for d in recent if d <= ltd.isoformat()
                  and (R.is_trading_day(d, "NYSE", S) or R.is_trading_day(d, "TSX", S))
                  and (d not in prices or prices[d].get("fx_usdcad") is None)]

    # 5) Dividends: first Monday of each month, for everything held.
    div_check = today_pt.weekday() == 0 and today_pt.day <= 7

    # How to fetch the database write times for new documents (inline query results show updatedAt).
    new_orders = R.load_collection(data, "orders")
    new_runs = R.load_collection(data, "runs")
    run_ids = sorted({str(new_orders[k.split("/", 1)[1]].get("run_id")) for k in missing_times if k.startswith("orders/")})
    run_starts = sorted(str(new_runs[k.split("/", 1)[1]].get("started_at", "")) for k in missing_times if k.startswith("runs/"))
    time_queries = []
    for i in range(0, len(run_ids), 30):
        time_queries.append({"collection": "orders", "query": {"where": [["run_id", "in", run_ids[i:i + 30]]], "limit": 200}})
    if run_starts:
        time_queries.append({"collection": "runs", "query": {"where": [["started_at", ">=", run_starts[0]]], "limit": 200}})

    out = {
        "now_utc": R.iso(now), "today_pacific": today_pt.isoformat(), "last_trading_day": ltd.isoformat(),
        "season": S["season_name"], "season_start": S["season_start"],
        "need_server_times": missing_times,
        "server_time_queries": time_queries,
        "unknown_symbols": sorted(unknown - {""}),
        "prices_needed": {s: sorted(d) for s, d in sorted(needs.items()) if d},
        "refresh_day": refresh,
        "fx_dates_needed": fx_missing,
        "dividend_check": {"due": div_check, "symbols": held if div_check else [],
                           "window_start": (today_pt - dt.timedelta(days=40)).isoformat()},
        "pending_orders": len(res["pending"]),
    }
    # Anything the price repo already has doesn't need a web search.
    rcl, rfx = load_price_repo(repo)
    from_repo = 0
    if rcl or rfx:
        for sym in list(out["prices_needed"]):
            left = [d for d in out["prices_needed"][sym] if (rcl.get(d) or {}).get(sym) is None]
            from_repo += len(out["prices_needed"][sym]) - len(left)
            if left:
                out["prices_needed"][sym] = left
            else:
                del out["prices_needed"][sym]
        out["fx_dates_needed"] = [d for d in out["fx_dates_needed"] if d not in rfx]
        fx_missing = out["fx_dates_needed"]
        out["refresh_day"] = False   # the repo refreshes every holding daily
    if load_repo_dividends(repo) is not None:
        out["dividend_check"] = {"due": False, "symbols": [], "window_start": out["dividend_check"]["window_start"],
                                 "note": "dividends come from the price repo"}
        div_check = False
    out["prices_from_repo"] = from_repo
    n = sum(len(v) for v in out["prices_needed"].values())
    out["summary"] = (f"{from_repo} closes come from the price repo; "
                      f"{len(out['prices_needed'])} symbols / {n} closes to look up on the web; "
                      f"{len(out['unknown_symbols'])} new symbols to check; "
                      f"{len(fx_missing)} exchange-rate days; dividends {'due' if div_check else 'not due'}; "
                      f"{len(missing_times)} write times needed.")
    return out


# ---------------------------------------------------------------------------
def check_candidates(data: Path, cand: dict, now: dt.datetime) -> tuple[dict, list, dict, dict, list, dict]:
    """Returns (price_docs_changed, report_lines, new_symbol_docs, new_dividend_docs, problems, new_split_docs)."""
    S = {**R.DEFAULT_SETTINGS, **R.load_collection(data, "settings").get("main", {})}
    ltd = last_trading_day(now).isoformat()
    today = now.astimezone(PT).date().isoformat()
    prices = {d: json.loads(json.dumps(v)) for d, v in R.load_collection(data, "prices").items()}
    symbols = {k.upper(): v for k, v in R.load_collection(data, "symbols").items()}
    report, problems = [], []
    changed: set[str] = set()

    # --- splits (recorded first so price checks can allow for them) ---
    all_splits = {k: v for k, v in R.load_collection(data, "splits").items()}
    new_splits = {}
    for x in cand.get("splits", []):
        try:
            sym = str(x["symbol"]).upper().strip()
            ex = dt.date.fromisoformat(str(x["ex_date"])).isoformat()
            ratio = float(x["ratio"])
        except (KeyError, ValueError, TypeError):
            problems.append(f"Unreadable split entry ignored: {x}")
            continue
        sid = f"{sym}-{ex}"
        if sid in all_splits or ratio <= 0 or abs(ratio - 1) < 1e-9:
            continue
        doc = {"symbol": sym, "ex_date": ex, "ratio": ratio, "source": x.get("source", ""), "added_at": R.iso(now)}
        new_splits[sid] = all_splits[sid] = doc
        report.append(f"Stock split {sym}: {ratio:g}-for-1 on {ex}." if ratio >= 1
                      else f"Reverse split {sym}: 1-for-{1 / ratio:g} on {ex}.")

    def split_factor(sym, after, upto):
        """Product of split ratios for sym with after < ex_date <= upto."""
        f = 1.0
        for v in all_splits.values():
            if str(v.get("symbol", "")).upper() == sym and after < v.get("ex_date", "") <= upto:
                f *= float(v["ratio"])
        return f

    # --- symbols ---
    new_syms = {}
    for s in cand.get("symbols", []):
        sym = str(s.get("symbol", "")).upper().strip()
        if not sym or sym in symbols:
            continue
        ex = str(s.get("exchange", "")).upper().strip()
        doc = {"symbol": sym, "name": s.get("name", ""), "exchange": ex,
               "currency": str(s.get("currency", "")).upper(), "type": str(s.get("type", "")).lower(),
               "source": s.get("source", ""), "added_at": R.iso(now)}
        if ex == "TSX" and not sym.endswith(".TO"):
            problems.append(f"Symbol {sym}: TSX listings must end in .TO; not added.")
            continue
        new_syms[sym] = doc
        symbols[sym] = doc
        report.append(f"New symbol {sym}: {doc['name']} ({ex}, {doc['currency']}, {doc['type']}).")

    # --- closes ---
    groups: dict[tuple, list] = {}
    for c in cand.get("closes", []):
        try:
            d = dt.date.fromisoformat(str(c["date"])).isoformat()
            groups.setdefault((d, str(c["symbol"]).upper().strip()), []).append(
                (float(c["close"]), str(c.get("source", "?")).strip().lower()))
        except (KeyError, ValueError, TypeError):
            problems.append(f"Unreadable price entry ignored: {c}")
    idx = close_index(prices)

    def prev_close(sym, d):
        """Last recorded close before d, adjusted for any split in between (so a 4-for-1
        split doesn't look like a 75% crash)."""
        ds = [x for x in idx.get(sym, []) if x < d]
        if not ds:
            return None
        return prices[ds[-1]]["closes"][sym] / split_factor(sym, ds[-1], d)

    for (d, sym), vals in sorted(groups.items()):
        tag = f"{sym} {d}"
        if dt.date.fromisoformat(d).weekday() >= 5 or d > ltd or d < S["season_start"]:
            problems.append(f"{tag}: date outside the trading days we can use (weekday, season start to {ltd}); ignored.")
            continue
        if sym not in symbols:
            problems.append(f"{tag}: unknown symbol; add it under 'symbols' first.")
            continue
        if not R.is_trading_day(d, R.calendar_for(sym, symbols), S):
            problems.append(f"{tag}: that exchange was closed on {d} (holiday); ignored.")
            continue
        by_src = {}
        for v, src in vals:
            by_src.setdefault(src, v)
        nums = list(by_src.values())
        existing = (prices.get(d, {}).get("closes") or {}).get(sym)
        if existing is not None:
            off = [v for v in nums if abs(v / existing - 1) > AGREE]
            if off:
                problems.append(f"{tag}: already recorded at {existing}; a source now says {off[0]}. Kept the recorded price.")
            continue
        if min(nums) <= 0:
            problems.append(f"{tag}: non-positive price ignored.")
            continue
        if TRUSTED in by_src:
            val = by_src[TRUSTED]
            others = {k: v for k, v in by_src.items() if k != TRUSTED}
            off = {k: v for k, v in others.items() if abs(v / val - 1) > AGREE}
            if off:
                problems.append(f"{tag}: web source disagrees with the price feed ({off}); used the feed ({val}).")
            pc = prev_close(sym, d)
            if pc and abs(val / pc - 1) > TRUSTED_MAX_MOVE and not (others and not off):
                problems.append(f"{tag}: feed price {val} is {val / pc - 1:+.1%} from the last close ({pc}); "
                                f"needs a web source to confirm. Not recorded.")
                continue
            how = "price feed" + (" + " + " + ".join(others) if others and not off else "")
        elif len(nums) >= 2:
            if max(nums) / min(nums) - 1 > AGREE:
                problems.append(f"{tag}: sources disagree ({', '.join(f'{s}={v}' for s, v in by_src.items())}); not recorded.")
                continue
            val, how = nums[0], "confirmed by " + " + ".join(by_src)
        else:
            val = nums[0]
            pc = prev_close(sym, d)
            if pc and abs(val / pc - 1) > SINGLE_MAX_MOVE:
                problems.append(f"{tag}: {val} is {val / pc - 1:+.1%} from the last close ({pc}); needs a second source. Not recorded.")
                continue
            how = f"single source ({next(iter(by_src))})"
        doc = prices.setdefault(d, {"date": d, "closes": {}})
        doc.setdefault("closes", {})[sym] = round(val, 4)
        doc.setdefault("sources", {})[sym] = list(by_src)
        idx.setdefault(sym, []).append(d)
        idx[sym].sort()
        changed.add(d)
        report.append(f"Recorded {tag} = {val} ({how}).")

    # --- exchange rate ---
    for f in cand.get("fx", []):
        try:
            d = dt.date.fromisoformat(str(f["date"])).isoformat()
            v = float(f["usdcad"])
        except (KeyError, ValueError, TypeError):
            problems.append(f"Unreadable exchange-rate entry ignored: {f}")
            continue
        if dt.date.fromisoformat(d).weekday() >= 5 or d > ltd or d < S["season_start"] or not (1.0 < v < 2.0):
            problems.append(f"USD/CAD {d}={v}: outside allowed dates or range; ignored.")
            continue
        doc = prices.setdefault(d, {"date": d, "closes": {}})
        old = doc.get("fx_usdcad")
        if old is not None:
            if abs(old - v) > 1e-6:
                problems.append(f"USD/CAD {d}: already recorded at {old}; new value {v} ignored.")
            continue
        doc["fx_usdcad"] = v
        doc.setdefault("sources", {})["_fx"] = [f.get("source", "?")]
        changed.add(d)
        report.append(f"Recorded USD/CAD {d} = {v}.")

    # --- dividends ---
    divs = R.load_collection(data, "dividends")
    new_divs = {}
    for x in cand.get("dividends", []):
        try:
            sym = str(x["symbol"]).upper()
            ex = dt.date.fromisoformat(str(x["ex_date"])).isoformat()
            pay = dt.date.fromisoformat(str(x.get("pay_date") or x["ex_date"])).isoformat()
            amt = float(x["amount"])
        except (KeyError, ValueError, TypeError):
            problems.append(f"Unreadable dividend entry ignored: {x}")
            continue
        did = f"{sym}-{ex}"
        if did in divs or did in new_divs or amt <= 0:
            continue
        new_divs[did] = {"symbol": sym, "ex_date": ex, "pay_date": pay, "amount": amt,
                         "source": x.get("source", ""), "added_at": R.iso(now)}
        if x.get("pay_date_estimated"):
            new_divs[did]["pay_date_estimated"] = True
        report.append(f"Dividend {sym}: {amt} per share, ex-date {ex}, paid {pay}"
                      + (" (pay date estimated)." if x.get("pay_date_estimated") else "."))

    return {d: prices[d] for d in changed}, report, new_syms, new_divs, problems, new_splits


def finish(data: Path, cand: dict, out_dir: Path, now: dt.datetime, server_times: dict, versions: dict,
           repo=None) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    rc = repo_candidates(data, repo, now, [x.get("symbol", "") for x in cand.get("symbols", [])])
    cand = dict(cand)
    cand["closes"] = list(cand.get("closes", [])) + rc["closes"]
    web_fx_dates = {str(f.get("date")) for f in cand.get("fx", [])}
    cand["fx"] = list(cand.get("fx", [])) + [f for f in rc["fx"] if f["date"] not in web_fx_dates]
    cand["splits"] = rc.get("splits", []) + list(cand.get("splits", []))   # feed first
    web_div_ids = {f"{str(x.get('symbol', '')).upper()}-{x.get('ex_date')}" for x in cand.get("dividends", [])}
    cand["dividends"] = list(cand.get("dividends", [])) + \
        [x for x in rc.get("dividends", []) if f"{x['symbol']}-{x['ex_date']}" not in web_div_ids]
    work = out_dir / "work_data"
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(data, work)

    price_docs, report, new_syms, new_divs, problems, new_splits = check_candidates(data, cand, now)
    writes: dict[str, dict] = {}
    for d, doc in price_docs.items():
        writes[f"prices/{d}"] = doc
    for s, doc in new_syms.items():
        writes[f"symbols/{s}"] = doc
    for k, doc in new_divs.items():
        writes[f"dividends/{k}"] = doc
    for k, doc in new_splits.items():
        writes[f"splits/{k}"] = doc
    for key, doc in writes.items():
        c, i = key.split("/", 1)
        f = work / c / f"{i}.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(doc))

    res = R.rebuild(work, now, server_times)
    outs = R.build_outputs(res)
    old_receipts = R.load_collection(data, "receipts")
    for key, doc in outs.items():
        c, i = key.split("/", 1)
        if c == "receipts" and R.fingerprint(old_receipts.get(i, {})) == R.fingerprint(doc):
            continue
        writes[key] = doc

    # Which of these already exist (and so need a version pin)?
    need = [k for k in writes if (data / k.split("/")[0] / f"{k.split('/', 1)[1]}.json").exists()]
    missing = [k for k in need if k not in versions]

    docs_dir = out_dir / "docs"
    shutil.rmtree(docs_dir, ignore_errors=True)
    entries = []
    for key, doc in writes.items():
        c, i = key.split("/", 1)
        f = docs_dir / c / f"{i}.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(doc, default=str))
        e = {"op": "set", "collection": c, "doc_id": i, "file_path": str(f.resolve())}
        if key in versions:
            e["if_version"] = int(versions[key])
        entries.append((e, f.stat().st_size))

    for old in out_dir.glob("batch_*.json"):
        old.unlink()
    batches, cur, size = [], [], 0
    for e, sz in entries:
        if cur and (len(cur) >= 40 or size + sz > 700_000):
            batches.append(cur)
            cur, size = [], 0
        cur.append(e)
        size += sz
    if cur:
        batches.append(cur)
    for n, b in enumerate(batches, 1):
        (out_dir / f"batch_{n}.json").write_text(json.dumps(b))

    summary = R.summary(outs)
    lines = [f"# Referee run {R.iso(now)}", "", "## Standings", "```", summary, "```", "",
             "## Data recorded today"] + [f"- {x}" for x in report or ["(nothing new)"]] + \
            ["", "## Problems with what was found"] + [f"- {x}" for x in problems or ["(none)"]] + \
            ["", f"## Database writes: {len(entries)} documents in {len(batches)} batch(es)"]
    (out_dir / "report.md").write_text("\n".join(lines))
    print("\n".join(lines))
    if missing:
        print("\nVERSIONS NEEDED (these documents already exist; add their version numbers to the versions file and run finish again):")
        for k in missing:
            print("  " + k)
        return 2
    print(f"\nReady: send {', '.join(f'batch_{n}.json' for n in range(1, len(batches) + 1))} with ArtifactData action 'batch'.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "finish"):
        p = sub.add_parser(name)
        p.add_argument("--data", required=True)
        p.add_argument("--now", default=None)
        p.add_argument("--server-times", default=None)
        p.add_argument("--price-repo", default=None, help="path to a clone of the trading-prices repo")
        if name == "finish":
            p.add_argument("--candidates", required=True)
            p.add_argument("--out", required=True)
            p.add_argument("--versions", default=None)
    a = ap.parse_args(argv)
    now = R.parse_time(a.now) if a.now else dt.datetime.now(dt.timezone.utc)
    st = load_json(a.server_times)
    if a.cmd == "plan":
        print(json.dumps(plan(Path(a.data), now, st, a.price_repo), indent=1))
        return 0
    return finish(Path(a.data), load_json(a.candidates), Path(a.out), now, st, load_json(a.versions), a.price_repo)


if __name__ == "__main__":
    sys.exit(main())
