#!/usr/bin/env python3
"""
REFEREE — AI Trading Agents Competition
=======================================

This is a plain script, not an AI. It cannot be persuaded, flattered, or
bargained with. It is the ONLY thing that:
  * fills orders,
  * keeps the ledger,
  * publishes statements and the scoreboard.

How it works, in plain language
-------------------------------
Every run, the referee starts from nothing and replays the whole season:
  1. Start every portfolio with the starting cash (e.g. $10,000 CAD).
  2. Walk forward one trading day at a time using the recorded closing prices.
  3. On each day, fill any orders that are due, pay any dividends, and value
     every portfolio at that day's close.
  4. At the end, write out each agent's private statement, Eric's scoreboard,
     and an integrity report.

Because it rebuilds from the full history every time, nothing it wrote last
time is trusted. If someone edited a balance, the next run overwrites it
with the true numbers.

Anti-cheating details
---------------------
* "Receipts": the first time the referee sees an order (or an agent's run
  record) it saves its own copy plus the time it saw it. From then on it uses
  ITS copy, so editing an order afterwards does nothing except get flagged.
* An order fills at the first market close AFTER the referee received it,
  so an agent can never trade at a price it has already seen.
* Prices, settings and personality files are fingerprinted (hashed). Any
  change is flagged in the integrity report.

Input folder layout (one JSON file per record, mirroring the dashboard's
database collections):
  settings/main.json
  prices/<YYYY-MM-DD>.json       {"date", "fx_usdcad", "closes": {SYM: price}}
  symbols/<SYM>.json             {"symbol","name","exchange","currency","type"}
  dividends/<id>.json            {"symbol","ex_date","pay_date","amount"}
  orders/<order_id>.json         written by agents
  runs/<run_id>.json             written by agents (one per agent run)
  reveals/<reveal_id>.json       written by Eric's Reveal button
  personalities/<agent>.json     written by Eric + Claude at setup
  integrity/main.json            the referee's own memory (receipts, hashes)

Usage:
  python3 referee.py --data DATA_DIR --out OUT_DIR [--now 2026-11-02T22:00:00Z]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/Toronto")          # TSX/NYSE/NASDAQ close at 4pm Eastern
AGENTS = ["holyfield", "marble", "valeri", "lauderdale", "bertolt"]
CONTROL = "index"                          # the do-nothing index fund portfolio
PORTFOLIOS = AGENTS + [CONTROL]
DISPLAY = {"holyfield": "Holyfield", "marble": "Marble", "valeri": "Valeri",
           "lauderdale": "Lauderdale", "bertolt": "Bertolt", CONTROL: "Index fund (VTI)"}

DEFAULT_SETTINGS = {
    "season_name": "Season 1",
    "season_start": "2026-11-02",
    "season_end": "2027-10-29",
    "starting_cash": 10000.0,
    "fx_fee": 0.015,                  # 1.5% each way CAD<->USD
    "us_withholding": 0.15,           # 15% on US dividends, not recoverable in a TFSA
    "commission": 0.0,                # Qtrade: $0 on stocks & ETFs
    "position_limit": 0.20,           # no buy may take one holding above 20%
    "weekly_limit": 0.25,             # max 25% bought AND max 25% sold per week
    "weekly_limit_grace_days": 14,    # an agent's runs in its first 14 days skip the weekly limit (getting invested)
    "min_share_price": 1.00,          # below this counts as a penny stock
    "allowed_exchanges": ["TSX", "NYSE", "NYSE ARCA", "NYSE AMERICAN", "NASDAQ", "CBOE"],
    "allowed_types": ["stock", "etf"],
    "control_symbol": "VTI",
    "score_vol_floor": 0.10,          # bumpiness counted as at least 10%/yr
    "min_days_for_score": 10,
    "max_pending_days": 10,           # reject an order if no price at all for 10 trading days
    "late_price_patience": 5,         # if the fill-day close never turns up but 5 later closes do, fill at the next one
    # Market holidays (weekdays the exchange is closed). Check against the exchanges'
    # published calendars each year.
    "holidays": {
        "NYSE": ["2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
                 "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
                 "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
                 "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"],
        "TSX": ["2026-01-01", "2026-02-16", "2026-04-03", "2026-05-18", "2026-07-01", "2026-08-03",
                "2026-09-07", "2026-10-12", "2026-12-25", "2026-12-28",
                "2027-01-01", "2027-02-15", "2027-03-26", "2027-05-24", "2027-07-01", "2027-08-02",
                "2027-09-06", "2027-10-11", "2027-12-27", "2027-12-28"],
    },
}


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
def r2(x: float) -> float:
    """Round money to cents."""
    return round(x + 0.0, 2)


def fingerprint(obj) -> str:
    """A short hash of a record. If the record changes, so does this."""
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def parse_time(s: str) -> dt.datetime:
    s = s.strip().replace("Z", "+00:00")
    t = dt.datetime.fromisoformat(s)
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(dt.timezone.utc)


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def close_time(date_str: str) -> dt.datetime:
    """The market close (4pm Eastern) for a trading date, in UTC."""
    d = dt.date.fromisoformat(date_str)
    return dt.datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET).astimezone(dt.timezone.utc)


def week_key(date_str: str) -> str:
    y, w, _ = dt.date.fromisoformat(date_str).isocalendar()
    return f"{y}-W{w:02d}"


def exchange_allowed(exchange: str, S: dict) -> bool:
    """Cboe has several venues (BZX, BYX, EDGX...); any of them counts as Cboe."""
    ex = str(exchange or "").upper().strip()
    if ex.startswith("CBOE") or ex.startswith("BATS"):
        ex = "CBOE"
    return ex in [a.upper() for a in S["allowed_exchanges"]]


def calendar_for(sym: str, symbols: dict) -> str:
    return "TSX" if str(symbols.get(sym, {}).get("exchange", "")).upper() == "TSX" or sym.endswith(".TO") else "NYSE"


def is_trading_day(date_str: str, cal: str, S: dict) -> bool:
    return dt.date.fromisoformat(date_str).weekday() < 5 and date_str not in S["holidays"].get(cal, [])


def fill_date(received_at: str, cal: str, S: dict) -> str:
    """First trading day on that exchange whose 4pm Eastern close is after the order was received."""
    recv = parse_time(received_at)
    d = recv.astimezone(ET).date()
    while not is_trading_day(d.isoformat(), cal, S) or close_time(d.isoformat()) <= recv:
        d += dt.timedelta(days=1)
    return d.isoformat()


def first_trading_day(date_str: str, cal: str, S: dict) -> str:
    d = dt.date.fromisoformat(date_str)
    while not is_trading_day(d.isoformat(), cal, S):
        d += dt.timedelta(days=1)
    return d.isoformat()


def unwrap(doc):
    """Database exports may wrap a record as {"data": {...}, "version": n}."""
    if isinstance(doc, dict) and "data" in doc and isinstance(doc["data"], dict) \
            and set(doc.keys()) <= {"data", "version", "id", "doc_id", "path",
                                     "created_at", "updated_at", "createdAt", "updatedAt"}:
        return doc["data"]
    return doc


def load_collection(data_dir: Path, name: str) -> dict:
    folder = data_dir / name
    out = {}
    if folder.is_dir():
        for f in sorted(folder.glob("*.json")):
            try:
                out[f.stem] = unwrap(json.loads(f.read_text()))
            except json.JSONDecodeError as e:
                print(f"WARNING: could not read {f}: {e}", file=sys.stderr)
    return out


# ----------------------------------------------------------------------------
# Portfolio state
# ----------------------------------------------------------------------------
class Portfolio:
    def __init__(self, name: str, cash: float):
        self.name = name
        self.cash = cash
        self.holdings: dict[str, dict] = {}     # sym -> {"shares": int, "cost_cad": float}
        self.fills: list[dict] = []
        self.rejections: list[dict] = []
        self.dividends: list[dict] = []
        self.splits: list[dict] = []
        self.values: list[tuple[str, float]] = []
        self.week_used: dict[str, dict] = {}    # "2026-W45" -> {"buy": x, "sell": y}
        self.week_start_value: dict[str, float] = {}
        self.realized_pl = 0.0

    def value(self, last_price: dict, symbols: dict, fx: float) -> float:
        v = self.cash
        for sym, h in self.holdings.items():
            v += h["shares"] * last_price.get(sym, 0.0) * fx_rate(sym, symbols, fx)
        return v


def fx_rate(sym: str, symbols: dict, fx: float) -> float:
    return fx if symbols.get(sym, {}).get("currency", "CAD").upper() == "USD" else 1.0


def is_usd(sym: str, symbols: dict) -> bool:
    return symbols.get(sym, {}).get("currency", "CAD").upper() == "USD"


# ----------------------------------------------------------------------------
# Integrity: receipts + fingerprints
# ----------------------------------------------------------------------------
def update_integrity(prev: dict, orders: dict, runs: dict, prices: dict, settings_raw: dict,
                     personalities: dict, dividends: dict, now: dt.datetime,
                     server_times: dict | None = None) -> tuple[dict, list]:
    """Stamp anything new, compare everything old, and list problems found this run.

    server_times: {"orders/<id>": iso, "runs/<id>": iso} -- the database's own record of
    when each document was last written. When given for a NEW document, that time is
    used as its receipt time (it can't be faked by an agent). Otherwise: now.
    """
    server_times = server_times or {}
    integ = {
        "order_receipts": dict(prev.get("order_receipts", {})),
        "run_receipts": dict(prev.get("run_receipts", {})),
        "price_marks": {d: dict(v) for d, v in prev.get("price_marks", {}).items()},
        "dividend_hashes": dict(prev.get("dividend_hashes", {})),
        "personality_hashes": dict(prev.get("personality_hashes", {})),
        "settings_hash": prev.get("settings_hash"),
        "issues": list(prev.get("issues", [])),
        "last_run_at": prev.get("last_run_at"),
    }
    new_issues = []
    stamp = iso(now)

    def flag(kind, detail):
        new_issues.append({"found_at": stamp, "kind": kind, "detail": detail})

    def receipt_time(key):
        t = server_times.get(key)
        if t:
            try:
                return iso(min(parse_time(t), now))
            except ValueError:
                pass
        return stamp

    # Orders: first sighting -> save a copy. Later changes -> flagged, copy wins.
    for oid, o in orders.items():
        rec = integ["order_receipts"].get(oid)
        if rec is None:
            integ["order_receipts"][oid] = {"received_at": receipt_time(f"orders/{oid}"),
                                            "order": o, "hash": fingerprint(o)}
        elif rec["hash"] != fingerprint(o) and not rec.get("edit_flagged") == fingerprint(o):
            flag("order_edited", f"Order {oid} was changed after the referee received it. "
                                 f"The original is kept and the change is ignored.")
            rec["edit_flagged"] = fingerprint(o)
    for oid, rec in integ["order_receipts"].items():
        if oid not in orders and not rec.get("delete_flagged"):
            flag("order_deleted", f"Order {oid} was deleted after the referee received it. "
                                  f"The referee's copy is still used.")
            rec["delete_flagged"] = True

    for rid, r in runs.items():
        rec = integ["run_receipts"].get(rid)
        if rec is None:
            keep = {k: r.get(k) for k in ("agent", "started_at", "mode", "reveal_seen")}
            integ["run_receipts"][rid] = {"received_at": receipt_time(f"runs/{rid}"),
                                          "run": keep, "hash": fingerprint(r)}
        elif rec["hash"] != fingerprint(r) and rec.get("edit_flagged") != fingerprint(r):
            flag("run_edited", f"Run record {rid} was changed after the referee received it.")
            rec["edit_flagged"] = fingerprint(r)

    # Prices: every (date, symbol) close is remembered. Adding a new close is normal
    # (prices arrive late); changing or removing a remembered one is flagged.
    marks = integ["price_marks"]
    for d, doc in prices.items():
        m = marks.setdefault(d, {})
        vals = dict(doc.get("closes") or {})
        if doc.get("fx_usdcad") is not None:
            vals["_fx"] = doc["fx_usdcad"]
        for sym, px in vals.items():
            if px is None:
                continue
            old = m.get(sym)
            if old is not None and abs(float(old) - float(px)) > 1e-9:
                flag("price_changed", f"Recorded price for {sym} on {d} changed from {old} to {px}.")
            m[sym] = px
        for sym in list(m):
            if sym not in vals or vals[sym] is None:
                flag("price_deleted", f"Recorded price for {sym} on {d} was removed.")
                del m[sym]
    for d in list(marks):
        if d not in prices:
            if marks[d]:
                flag("price_deleted", f"All recorded prices for {d} were removed.")
            del marks[d]

    for label, coll, key in (("dividend", dividends, "dividend_hashes"),
                             ("personality", personalities, "personality_hashes")):
        for k, doc in coll.items():
            h = fingerprint(doc)
            old = integ[key].get(k)
            if old is not None and old != h:
                flag(f"{label}_changed", f"{label.capitalize()} record '{k}' changed since the last run.")
            integ[key][k] = h
        for k in list(integ[key]):
            if k not in coll:
                flag(f"{label}_deleted", f"{label.capitalize()} record '{k}' was deleted.")
                del integ[key][k]

    sh = fingerprint(settings_raw)
    if integ["settings_hash"] is not None and integ["settings_hash"] != sh:
        flag("settings_changed", "Competition settings changed since the last run.")
    integ["settings_hash"] = sh

    integ["issues"] = (integ["issues"] + new_issues)[-200:]
    integ["last_run_at"] = stamp
    return integ, new_issues


# ----------------------------------------------------------------------------
# The replay
# ----------------------------------------------------------------------------
def rebuild(data_dir: Path, now: dt.datetime, server_times: dict | None = None) -> dict:
    settings_raw = load_collection(data_dir, "settings").get("main", {})
    S = {**DEFAULT_SETTINGS, **settings_raw}
    prices = load_collection(data_dir, "prices")
    symbols = {k.upper(): v for k, v in load_collection(data_dir, "symbols").items()}
    for k, v in list(symbols.items()):
        if v.get("symbol"):
            symbols[v["symbol"].upper()] = v
    dividends = load_collection(data_dir, "dividends")
    splits = load_collection(data_dir, "splits")
    orders = load_collection(data_dir, "orders")
    runs = load_collection(data_dir, "runs")
    reveals = load_collection(data_dir, "reveals")
    personalities = load_collection(data_dir, "personalities")
    prev_integ = dict(load_collection(data_dir, "integrity").get("main", {}))
    # Receipts are stored in monthly records (receipts/2026-11) to stay under size limits.
    o_rec, r_rec = dict(prev_integ.get("order_receipts", {})), dict(prev_integ.get("run_receipts", {}))
    marks = dict(prev_integ.get("price_marks", {}))
    for _, doc in sorted(load_collection(data_dir, "receipts").items()):
        o_rec.update(doc.get("orders", {}))
        r_rec.update(doc.get("runs", {}))
        marks.update(doc.get("prices", {}))
    prev_integ["order_receipts"], prev_integ["run_receipts"], prev_integ["price_marks"] = o_rec, r_rec, marks

    integ, new_issues = update_integrity(prev_integ, orders, runs, prices, settings_raw,
                                         personalities, dividends, now, server_times)

    # ---- Use the referee's own copies of orders and runs ----
    order_list = []
    for oid, rec in integ["order_receipts"].items():
        o = dict(rec["order"])
        o["id"] = oid
        o["received_at"] = rec["received_at"]
        order_list.append(o)
    order_list.sort(key=lambda o: (o["received_at"], o["id"]))

    run_list = []
    for rid, rec in integ["run_receipts"].items():
        r = dict(rec["run"])
        r["id"] = rid
        received = parse_time(rec["received_at"])
        try:
            started = parse_time(r.get("started_at", rec["received_at"]))
        except ValueError:
            started = received
        r["started"] = min(started, received)       # a run can't start after we saw it
        run_list.append(r)

    # ---- Only what happened on or after the start date counts (earlier = practice) ----
    def from_start(t: dt.datetime) -> bool:
        return t.astimezone(ET).date().isoformat() >= S["season_start"]
    order_list = [o for o in order_list if from_start(parse_time(o["received_at"]))]
    run_list = [r for r in run_list if from_start(r["started"])]

    # ---- Cooling-off: first run after each reveal is review-only ----
    reveal_list = sorted(
        [dict(v, id=k, created=parse_time(v.get("created_at", "1970-01-01T00:00:00Z")))
         for k, v in reveals.items()], key=lambda x: x["created"])
    reveal_list = [rv for rv in reveal_list if from_start(rv["created"])]
    cooling_runs: dict[str, str] = {}    # run_id -> reveal_id
    for agent in AGENTS:
        mine = sorted([r for r in run_list if r.get("agent") == agent], key=lambda r: r["started"])
        for rv in reveal_list:
            for r in mine:
                if r["started"] >= rv["created"] or r.get("reveal_seen") == rv["id"]:
                    cooling_runs.setdefault(r["id"], rv["id"])
                    break
    known_runs = {r["id"]: r for r in run_list}

    # ---- Trading days in the season ----
    dates = sorted(d for d in prices
                   if S["season_start"] <= d <= S["season_end"])
    ports = {p: Portfolio(p, float(S["starting_cash"])) for p in PORTFOLIOS}
    last_price: dict[str, float] = {}
    fx = None
    pending = [o for o in order_list]
    deferrals: dict[str, int] = {}
    divs_sorted = sorted(({**d, "id": k} for k, d in dividends.items()),
                         key=lambda d: d.get("ex_date", ""))
    div_done: set[str] = set()
    splits_sorted = sorted(({**x, "id": k} for k, x in splits.items()),
                           key=lambda x: x.get("ex_date", ""))
    split_done: set[str] = set()
    owed: list[dict] = []          # dividends earned, waiting for pay date
    prev_date = None

    def reject(p: Portfolio, o: dict, date: str, reason: str):
        p.rejections.append({"order_id": o["id"], "date": date,
                             "action": str(o.get("action", "")).upper(),
                             "symbol": str(o.get("symbol", "")).upper(),
                             "shares": o.get("shares"), "reason": reason})

    for date in dates:
        day = prices[date]
        fx = float(day.get("fx_usdcad") or fx or 1.35)
        for sym, px in (day.get("closes") or {}).items():
            if px is not None:
                last_price[sym.upper()] = float(px)

        # New week? remember each portfolio's value at the start of it.
        wk = week_key(date)
        for p in ports.values():
            if wk not in p.week_start_value:
                p.week_start_value[wk] = p.values[-1][1] if p.values else float(S["starting_cash"])

        # --- Stock splits: on the split date each share becomes `ratio` shares
        # (4.0 = 4-for-1, 0.1 = 1-for-10). Total cost is unchanged, so no gain or loss.
        # A reverse split that leaves a fraction pays that fraction out in cash at today's close.
        for sp in splits_sorted:
            if sp["id"] in split_done or sp.get("ex_date", "9999") > date:
                continue
            split_done.add(sp["id"])
            sym = str(sp.get("symbol", "")).upper()
            try:
                ratio = float(sp["ratio"])
            except (KeyError, TypeError, ValueError):
                continue
            if ratio <= 0 or abs(ratio - 1.0) < 1e-9:
                continue
            for p in ports.values():
                h = p.holdings.get(sym)
                if not h or h["shares"] <= 0:
                    continue
                old = h["shares"]
                exact = old * ratio
                new = int(round(exact)) if abs(exact - round(exact)) < 1e-6 else int(exact)
                cash_cad = 0.0
                if new < exact - 1e-6 and last_price.get(sym):
                    frac = exact - new
                    rate = fx_rate(sym, symbols, fx)
                    cash_cad = frac * last_price[sym] * rate * (1 - (S["fx_fee"] if rate != 1.0 else 0.0))
                    cost_out = h["cost_cad"] * frac / exact
                    p.cash += cash_cad
                    p.realized_pl += cash_cad - cost_out
                    h["cost_cad"] -= cost_out
                h["shares"] = new
                if new == 0:
                    del p.holdings[sym]
                p.splits.append({"date": date, "symbol": sym, "ratio": ratio,
                                 "shares_before": old, "shares_after": new,
                                 "cash_in_lieu_cad": r2(cash_cad)})

        # --- Dividends: entitled if held at the previous close (before today's trades)
        for d in divs_sorted:
            if d["id"] in div_done or d.get("ex_date", "9999") > date:
                continue
            div_done.add(d["id"])
            sym = d["symbol"].upper()
            for p in ports.values():
                h = p.holdings.get(sym)
                if not h or h["shares"] <= 0:
                    continue
                gross = h["shares"] * float(d["amount"])
                usd = is_usd(sym, symbols)
                withheld = gross * S["us_withholding"] if usd else 0.0
                owed.append({"portfolio": p.name, "symbol": sym, "shares": h["shares"],
                             "per_share": float(d["amount"]), "gross_local": gross,
                             "withheld_local": withheld, "usd": usd,
                             "ex_date": d["ex_date"], "pay_date": d.get("pay_date") or d["ex_date"]})
        still_owed = []
        for o in owed:
            if o["pay_date"] <= date:
                net_local = o["gross_local"] - o["withheld_local"]
                if o["usd"]:
                    cad = net_local * fx
                    conv = cad * S["fx_fee"]
                    cad -= conv
                else:
                    conv, cad = 0.0, net_local
                p = ports[o["portfolio"]]
                p.cash += cad
                p.dividends.append({"date": date, "symbol": o["symbol"], "shares": o["shares"],
                                    "per_share": o["per_share"],
                                    "withheld_usd": r2(o["withheld_local"]) if o["usd"] else 0.0,
                                    "fx_cost_cad": r2(conv), "net_cad": r2(cad)})
            else:
                still_owed.append(o)
        owed = still_owed

        # --- Index fund control buys on the first day and never trades again
        ctrl = ports[CONTROL]
        csym = S["control_symbol"].upper()
        c_day = first_trading_day(S["season_start"], calendar_for(csym, symbols), S)
        c_has = (day.get("closes") or {}).get(csym) is not None
        c_late = sum(1 for d2 in dates if c_day < d2 <= date and (prices[d2].get("closes") or {}).get(csym) is not None)
        if not ctrl.fills and c_has and (date == c_day or (date > c_day and c_late >= S["late_price_patience"])):
            px = last_price[csym]
            rate = fx_rate(csym, symbols, fx)
            fee = S["fx_fee"] if rate != 1.0 else 0.0
            shares = int(ctrl.cash // (px * rate * (1 + fee)))
            if shares > 0:
                gross = shares * px * rate
                conv = gross * fee
                ctrl.cash -= gross + conv
                ctrl.holdings[csym] = {"shares": shares, "cost_cad": gross + conv}
                ctrl.fills.append({"order_id": "control-day-one", "date": date, "action": "BUY",
                                   "symbol": csym, "shares": shares, "price": px,
                                   "currency": "USD" if rate != 1.0 else "CAD", "fx": fx if rate != 1.0 else None,
                                   "gross_cad": r2(gross), "fx_cost_cad": r2(conv),
                                   "net_cad": r2(-(gross + conv))})

        # --- Orders due: each order fills at the close of its own fill day (the first
        # trading day closing after the referee received it). If that close hasn't
        # been recorded yet, the order waits for it rather than filling on a later day.
        due, later = [], []
        for o in pending:
            sym0 = str(o.get("symbol", "")).upper().strip()
            o["_fill_date"] = fill_date(o["received_at"], calendar_for(sym0, symbols), S)
            (due if o["_fill_date"] <= date else later).append(o)
        carry = []
        by_agent: dict[str, list] = {}
        for o in due:
            agent = str(o.get("agent", "")).lower()
            if agent not in AGENTS:
                # Not a real competitor; nothing to attach it to. Log under integrity.
                new_issues.append({"found_at": iso(now), "kind": "bad_order",
                                   "detail": f"Order {o['id']} names unknown agent '{o.get('agent')}'."})
                continue
            p = ports[agent]
            action = str(o.get("action", "")).upper()
            sym = str(o.get("symbol", "")).upper().strip()
            o["_action"], o["_sym"] = action, sym
            shares = o.get("shares")

            # ---- Checks that don't depend on the portfolio ----
            if not o["id"].lower().startswith(agent):
                reject(p, o, date, "Order id must start with your own agent name."); continue
            if o.get("run_id") not in known_runs or known_runs[o["run_id"]].get("agent") != agent:
                reject(p, o, date, "No matching run record for this order (run_id missing or not yours)."); continue
            if o["run_id"] in cooling_runs:
                reject(p, o, date, f"Cooling-off: your first run after reveal '{cooling_runs[o['run_id']]}' "
                                   f"is review-only. Submit orders on your next run."); continue
            if action not in ("BUY", "SELL"):
                reject(p, o, date, "Action must be BUY or SELL (no shorting, options or borrowing)."); continue
            if not isinstance(shares, int) or isinstance(shares, bool) or shares <= 0:
                reject(p, o, date, "Shares must be a whole number greater than zero (no fractional shares)."); continue
            meta = symbols.get(sym)
            if meta is None:
                reject(p, o, date, f"'{sym}' was not found as a stock or ETF on the TSX, NYSE or NASDAQ. "
                                   f"Use TSX symbols with .TO (e.g. RY.TO) and US symbols as-is (e.g. AAPL)."); continue
            if not exchange_allowed(meta.get("exchange", ""), S):
                reject(p, o, date, f"{sym} trades on {meta.get('exchange')}, which isn't allowed "
                                   f"(TSX, NYSE, NASDAQ and Cboe only)."); continue
            if str(meta.get("type", "")).lower() not in S["allowed_types"]:
                reject(p, o, date, f"{sym} is a {meta.get('type')}; only stocks and ETFs are allowed."); continue
            has_today = (day.get("closes") or {}).get(sym) is not None
            fd = o["_fill_date"]
            if date > fd:
                later_closes = sum(1 for d2 in dates if fd < d2 <= date
                                   and (prices[d2].get("closes") or {}).get(sym) is not None)
                waited = sum(1 for d2 in dates if fd < d2 <= date and is_trading_day(d2, calendar_for(sym, symbols), S))
                if has_today and later_closes >= S["late_price_patience"]:
                    new_issues.append({"found_at": iso(now), "kind": "fill_day_price_missing",
                                       "detail": f"Order {o['id']}: no {sym} close was ever recorded for {fd}; "
                                                 f"filled at the {date} close instead."})
                elif waited >= S["max_pending_days"] and later_closes == 0:
                    reject(p, o, date, f"No closing price could be found for {sym} for "
                                       f"{S['max_pending_days']} trading days.")
                    continue
                else:
                    carry.append(o)
                    continue
            elif not has_today:
                carry.append(o)
                continue
            if last_price[sym] < S["min_share_price"]:
                reject(p, o, date, f"{sym} closed at {last_price[sym]:.2f}, below the "
                                   f"${S['min_share_price']:.2f} penny-stock floor."); continue
            by_agent.setdefault(agent, []).append(o)

        for agent, olist in by_agent.items():
            p = ports[agent]
            # Grace period: runs started within N days of the agent's first trading run skip the weekly limit.
            my_runs = sorted([r for r in run_list if r.get("agent") == agent and r["id"] not in cooling_runs],
                             key=lambda r: r["started"])
            grace_end = (my_runs[0]["started"] + dt.timedelta(days=S["weekly_limit_grace_days"])) if my_runs else None
            grace_runs = {r["id"] for r in my_runs if grace_end and r["started"] < grace_end}
            sells = [o for o in olist if o["_action"] == "SELL"]
            buys = [o for o in olist if o["_action"] == "BUY"]
            for o in sells + buys:          # sells first so they free up cash
                sym, n = o["_sym"], o["shares"]
                px = last_price[sym]
                rate = fx_rate(sym, symbols, fx)
                fee = S["fx_fee"] if rate != 1.0 else 0.0
                gross = n * px * rate
                conv = gross * fee
                exempt = o.get("run_id") in grace_runs
                used = p.week_used.setdefault(wk, {"buy": 0.0, "sell": 0.0})
                cap = S["weekly_limit"] * p.week_start_value[wk]
                held = p.holdings.get(sym, {}).get("shares", 0)

                if o["_action"] == "SELL":
                    if n > held:
                        reject(p, o, date, f"You hold {held} shares of {sym}; can't sell {n} (no short selling)."); continue
                    if not exempt and used["sell"] + gross > cap + 0.005:
                        left = max(0.0, cap - used["sell"])
                        reject(p, o, date, f"Weekly change limit: you can sell about ${left:,.0f} more this week "
                                           f"(25% of ${p.week_start_value[wk]:,.0f}). This order was ${gross:,.0f}."); continue
                    h = p.holdings[sym]
                    cost_out = h["cost_cad"] * n / h["shares"]
                    net = gross - conv
                    p.cash += net
                    p.realized_pl += net - cost_out
                    h["shares"] -= n
                    h["cost_cad"] -= cost_out
                    if h["shares"] == 0:
                        del p.holdings[sym]
                    used["sell"] += gross
                    p.fills.append({"order_id": o["id"], "date": date, "action": "SELL", "symbol": sym,
                                    "shares": n, "price": px, "currency": "USD" if rate != 1.0 else "CAD",
                                    "fx": fx if rate != 1.0 else None, "gross_cad": r2(gross),
                                    "fx_cost_cad": r2(conv), "net_cad": r2(net),
                                    "realized_pl_cad": r2(net - cost_out)})
                else:  # BUY
                    total = gross + conv
                    if total > p.cash + 0.005:
                        afford = int(p.cash // (px * rate * (1 + fee)))
                        reject(p, o, date, f"Not enough cash: needs ${total:,.2f}, you have ${p.cash:,.2f}. "
                                           f"You could afford {afford} shares at today's close."); continue
                    if not exempt and used["buy"] + gross > cap + 0.005:
                        left = max(0.0, cap - used["buy"])
                        reject(p, o, date, f"Weekly change limit: you can buy about ${left:,.0f} more this week "
                                           f"(25% of ${p.week_start_value[wk]:,.0f}). This order was ${gross:,.0f}."); continue
                    total_after = p.value(last_price, symbols, fx) - conv
                    new_val = (held + n) * px * rate
                    limit = S["position_limit"] * total_after
                    if new_val > limit + 0.005:
                        max_n = max(0, int((limit - held * px * rate) // (px * rate)))
                        reject(p, o, date, f"Position limit: {sym} would be {new_val / total_after:.1%} of your "
                                           f"portfolio (max {S['position_limit']:.0%}). Most you could add: {max_n} shares."); continue
                    p.cash -= total
                    h = p.holdings.setdefault(sym, {"shares": 0, "cost_cad": 0.0})
                    h["shares"] += n
                    h["cost_cad"] += total
                    used["buy"] += gross
                    p.fills.append({"order_id": o["id"], "date": date, "action": "BUY", "symbol": sym,
                                    "shares": n, "price": px, "currency": "USD" if rate != 1.0 else "CAD",
                                    "fx": fx if rate != 1.0 else None, "gross_cad": r2(gross),
                                    "fx_cost_cad": r2(conv), "net_cad": r2(-total)})
        pending = later + carry

        # --- Value every portfolio at today's close
        for p in ports.values():
            p.values.append((date, r2(p.value(last_price, symbols, fx))))
        prev_date = date

    as_of = dates[-1] if dates else None
    return {"S": S, "ports": ports, "pending": pending, "last_price": last_price, "fx": fx,
            "symbols": symbols, "as_of": as_of, "reveals": reveal_list, "runs": run_list,
            "cooling_runs": cooling_runs, "integrity": integ, "new_issues": new_issues,
            "orders": order_list, "now": now}


# ----------------------------------------------------------------------------
# Scores
# ----------------------------------------------------------------------------
def metrics(p: Portfolio, S: dict) -> dict:
    start = float(S["starting_cash"])
    vals = [v for _, v in p.values]
    if not vals:
        return {"value": start, "return": 0.0, "worst_drop": 0.0, "volatility": None, "score": None, "days": 0}
    peak, worst = start, 0.0
    for v in vals:
        peak = max(peak, v)
        worst = min(worst, v / peak - 1)
    series = [start] + vals
    daily = [series[i] / series[i - 1] - 1 for i in range(1, len(series))]
    n = len(daily)
    vol = score = None
    if n >= 2:
        mean = sum(daily) / n
        var = sum((x - mean) ** 2 for x in daily) / (n - 1)
        vol = math.sqrt(var) * math.sqrt(252)
        if n >= S["min_days_for_score"]:
            score = (mean * 252) / max(vol, S["score_vol_floor"])
    return {"value": vals[-1], "return": vals[-1] / start - 1, "worst_drop": worst,
            "volatility": vol, "score": score, "days": n}


def style(p: Portfolio, R: dict, since: str | None) -> dict:
    fx, lp, sy = R["fx"] or 1.0, R["last_price"], R["symbols"]
    total = p.value(lp, sy, fx) or 1.0
    cad = sum(h["shares"] * lp.get(s, 0) for s, h in p.holdings.items() if not is_usd(s, sy))
    usd = sum(h["shares"] * lp.get(s, 0) * fx for s, h in p.holdings.items() if is_usd(s, sy))
    invested = cad + usd
    return {"cash_pct": p.cash / total,
            "canadian_pct": (cad / invested) if invested else 0.0,
            "us_pct": (usd / invested) if invested else 0.0,
            "holdings": len(p.holdings),
            "trades_since_reveal": sum(1 for f in p.fills if since is None or f["date"] > since)}


def leaderboard(R: dict) -> list[dict]:
    S = R["S"]
    last_reveal = R["reveals"][-1]["created"].astimezone(ET).date().isoformat() if R["reveals"] else None
    rows = []
    for name, p in R["ports"].items():
        m = metrics(p, S)
        rows.append({"portfolio": name, "name": DISPLAY[name], **m, **style(p, R, last_reveal)})
    # Rank by risk-adjusted score once there's enough history, otherwise by return.
    rows.sort(key=lambda r: (r["score"] if r["score"] is not None else -1e9, r["return"]), reverse=True)
    if all(r["score"] is None for r in rows):
        rows.sort(key=lambda r: r["return"], reverse=True)
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


# ----------------------------------------------------------------------------
# Outputs
# ----------------------------------------------------------------------------
def build_outputs(R: dict) -> dict[str, dict]:
    """Returns {"collection/doc_id": document} for everything the referee publishes."""
    S, now = R["S"], iso(R["now"])
    rows = leaderboard(R)
    by_name = {r["portfolio"]: r for r in rows}
    out: dict[str, dict] = {}
    lp, sy, fx = R["last_price"], R["symbols"], R["fx"] or 1.0
    latest_reveal = R["reveals"][-1] if R["reveals"] else None

    out["scoreboard/current"] = {
        "generated_at": now, "as_of": R["as_of"], "season": S["season_name"],
        "season_start": S["season_start"], "fx_usdcad": R["fx"],
        "rows": rows,
        "series": {name: p.values for name, p in R["ports"].items()},
        "pending_orders": len(R["pending"]),
        "integrity_ok": not R["integrity"]["issues"],
        "new_issues": R["new_issues"],
    }

    for name, p in R["ports"].items():
        total = p.value(lp, sy, fx) or 1.0
        holdings = []
        for sym, h in sorted(p.holdings.items()):
            rate = fx_rate(sym, sy, fx)
            mv = h["shares"] * lp.get(sym, 0) * rate
            holdings.append({"symbol": sym, "name": sy.get(sym, {}).get("name", ""),
                             "currency": "USD" if rate != 1.0 else "CAD",
                             "shares": h["shares"], "last_close": lp.get(sym),
                             "market_value_cad": r2(mv), "cost_cad": r2(h["cost_cad"]),
                             "unrealized_pl_cad": r2(mv - h["cost_cad"]),
                             "weight": round(mv / total, 4),
                             "room_to_add_cad": r2(max(0.0, S["position_limit"] * total - mv))})
        pend = [{"order_id": o["id"], "action": str(o.get("action", "")).upper(),
                 "symbol": str(o.get("symbol", "")).upper(), "shares": o.get("shares"),
                 "received_at": o["received_at"]}
                for o in R["pending"] if str(o.get("agent", "")).lower() == name]
        wk = week_key(R["as_of"]) if R["as_of"] else None
        out[f"ledger/{name}"] = {"portfolio": name, "generated_at": now, "cash_cad": r2(p.cash),
                                 "total_value_cad": r2(total), "holdings": holdings, "fills": p.fills,
                                 "rejections": p.rejections, "dividends": p.dividends, "splits": p.splits,
                                 "pending": pend,
                                 "values": p.values}
        if name == CONTROL:
            continue
        my_runs = sorted([r for r in R["runs"] if r.get("agent") == name], key=lambda r: r["started"])
        last_run = my_runs[-1] if my_runs else None
        review_only_next = bool(latest_reveal and (last_run is None or
                                                   last_run["started"] < latest_reveal["created"]))
        m = by_name[name]
        out[f"statements/{name}"] = {
            "agent": name, "generated_at": now, "prices_as_of": R["as_of"], "season": S["season_name"],
            "cash_cad": r2(p.cash), "total_value_cad": r2(total),
            "return_since_start": round(m["return"], 4), "worst_drop": round(m["worst_drop"], 4),
            "risk_adjusted_score": None if m["score"] is None else round(m["score"], 3),
            "realized_pl_cad": r2(p.realized_pl),
            "holdings": holdings,
            "fills": p.fills, "rejections": p.rejections, "dividends": p.dividends,
            "splits": p.splits,
            "pending_orders": pend,
            "weekly_limit": {"week": wk, "cap_cad": r2(S["weekly_limit"] * p.week_start_value.get(wk, total)),
                             "bought_cad": r2(p.week_used.get(wk, {}).get("buy", 0.0)),
                             "sold_cad": r2(p.week_used.get(wk, {}).get("sell", 0.0))},
            "fx_usdcad": R["fx"],
            "latest_reveal_id": latest_reveal["id"] if latest_reveal else None,
            "next_run_is_review_only": review_only_next,
            "rules": {k: S[k] for k in ("starting_cash", "fx_fee", "us_withholding", "position_limit",
                                         "weekly_limit", "weekly_limit_grace_days", "min_share_price",
                                         "allowed_exchanges")},
        }

    integ = dict(R["integrity"])
    months: dict[str, dict] = {}
    for kind in ("orders", "runs"):
        for rid, rec in integ[f"{kind[:-1]}_receipts"].items():
            m = rec["received_at"][:7]
            months.setdefault(m, {"month": m, "orders": {}, "runs": {}})[kind][rid] = rec
    for d, mk in integ["price_marks"].items():
        m = d[:7]
        months.setdefault(m, {"month": m, "orders": {}, "runs": {}}).setdefault("prices", {})[d] = mk
    for m, doc in months.items():
        doc.setdefault("prices", {})
        out[f"receipts/{m}"] = doc
    out["integrity/main"] = {k: v for k, v in integ.items()
                             if k not in ("order_receipts", "run_receipts", "price_marks")}
    # A small public summary for the dashboard (the full file holds every receipt).
    out["integrity/summary"] = {
        "checked_at": now, "ok": not integ["issues"],
        "issues": integ["issues"][-25:], "new_this_run": len(R["new_issues"]),
        "orders_received": len(integ["order_receipts"]), "runs_received": len(integ["run_receipts"]),
        "prices_fingerprinted": len(integ["price_marks"]),
    }
    return out


def write_outputs(out: dict, out_dir: Path) -> list[str]:
    written = []
    for key, doc in out.items():
        coll, doc_id = key.split("/", 1)
        f = out_dir / coll / f"{doc_id}.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(doc, indent=1, default=str))
        written.append(str(f))
    (out_dir / "manifest.json").write_text(json.dumps(sorted(out.keys()), indent=1))
    return written


def summary(out: dict) -> str:
    sb = out["scoreboard/current"]
    lines = [f"Prices as of {sb['as_of']}  ·  USD/CAD {sb['fx_usdcad']}",
             f"{'#':>2}  {'Portfolio':<18}{'Value':>11}{'Return':>9}{'Worst':>8}{'Score':>8}{'Cash':>7}"]
    for r in sb["rows"]:
        sc = "  —" if r["score"] is None else f"{r['score']:.2f}"
        lines.append(f"{r['rank']:>2}  {r['name']:<18}{r['value']:>11,.2f}{r['return']:>9.2%}"
                     f"{r['worst_drop']:>8.2%}{sc:>8}{r['cash_pct']:>7.0%}")
    if sb["new_issues"]:
        lines.append("\nINTEGRITY FLAGS THIS RUN:")
        lines += [f"  - {i['kind']}: {i['detail']}" for i in sb["new_issues"]]
    else:
        lines.append("\nIntegrity: no new issues.")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Referee for the AI Trading Agents Competition")
    ap.add_argument("--data", required=True, help="folder with the input collections")
    ap.add_argument("--out", required=True, help="folder to write results into")
    ap.add_argument("--now", default=None, help="pretend the current time is this (UTC ISO); for testing")
    ap.add_argument("--server-times", default=None, help="JSON file of database write times for new orders/runs")
    a = ap.parse_args(argv)
    now = parse_time(a.now) if a.now else dt.datetime.now(dt.timezone.utc)
    st = json.loads(Path(a.server_times).read_text()) if a.server_times else None
    R = rebuild(Path(a.data), now, st)
    out = build_outputs(R)
    write_outputs(out, Path(a.out))
    print(summary(out))


if __name__ == "__main__":
    main()
