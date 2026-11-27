"""
Tests for the referee. Run with:  python3 -m unittest -v test_referee.py

Each test builds a tiny fake market in a temporary folder, writes hand-made
orders, runs the referee (sometimes several times, like real daily runs),
and checks the results.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import referee as R

# A tiny fake market: one Canadian stock, two US, one OTC, one penny stock, VTI.
SYMBOLS = {
    "RY.TO": {"symbol": "RY.TO", "name": "Royal Bank", "exchange": "TSX", "currency": "CAD", "type": "stock"},
    "AAPL": {"symbol": "AAPL", "name": "Apple", "exchange": "NASDAQ", "currency": "USD", "type": "stock"},
    "VTI": {"symbol": "VTI", "name": "Vanguard Total Stock Market ETF", "exchange": "NYSE ARCA", "currency": "USD", "type": "etf"},
    "TINY.TO": {"symbol": "TINY.TO", "name": "Tiny Mining", "exchange": "TSX", "currency": "CAD", "type": "stock"},
    "OTCX": {"symbol": "OTCX", "name": "Some OTC Co", "exchange": "OTC", "currency": "USD", "type": "stock"},
}
# Mon 2 Nov 2026 .. Fri 13 Nov 2026 (two trading weeks)
DATES = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06",
         "2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"]


def price_day(i):
    return {"RY.TO": 150.0 + i, "AAPL": 200.0 + 2 * i, "VTI": 250.0 + i,
            "TINY.TO": 0.50, "OTCX": 5.0}


class Market:
    """Builds a data folder and runs the referee like the daily scheduled task would."""

    def __init__(self, days=len(DATES)):
        self.dir = Path(tempfile.mkdtemp())
        self.data, self.out = self.dir / "data", self.dir / "out"
        self.write("settings", "main", {"season_start": DATES[0], "season_end": "2027-10-29"})
        for k, v in SYMBOLS.items():
            self.write("symbols", k, v)
        for i in range(days):
            self.add_price(i)

    def add_price(self, i, **override):
        closes = price_day(i)
        closes.update(override)
        self.write("prices", DATES[i], {"date": DATES[i], "fx_usdcad": 1.40, "closes": closes})

    def write(self, coll, doc_id, doc):
        f = self.data / coll / f"{doc_id}.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(doc))

    def delete(self, coll, doc_id):
        (self.data / coll / f"{doc_id}.json").unlink()

    def run(self, now, server_times=None):
        """One referee run at time `now`. Its integrity memory is saved back, like the real push."""
        res = R.rebuild(self.data, R.parse_time(now), server_times)
        out = R.build_outputs(res)
        for key, doc in out.items():
            if key.startswith(("integrity/", "receipts/")):
                self.write(*key.split("/"), doc)
        return out

    def order(self, oid, agent, action, symbol, shares, run_id=None):
        self.write("orders", oid, {"agent": agent, "run_id": run_id or f"{agent}-run1",
                                   "action": action, "symbol": symbol, "shares": shares})

    def run_record(self, run_id, agent, started_at):
        self.write("runs", run_id, {"agent": agent, "started_at": started_at})

    def cleanup(self):
        shutil.rmtree(self.dir)


def stmt(out, agent):
    return out[f"statements/{agent}"]


def reasons(out, agent):
    return [r["reason"] for r in stmt(out, agent)["rejections"]]


class RefereeTests(unittest.TestCase):
    def setUp(self):
        self.m = Market()
        # Every agent has a run record on Monday morning (before the close).
        for a in R.AGENTS:
            self.m.run_record(f"{a}-run1", a, "2026-11-02T14:00:00Z")

    def tearDown(self):
        self.m.cleanup()

    # ---------- Fills and costs ----------
    def test_control_buys_vti_on_day_one_with_fx_cost(self):
        out = self.m.run("2026-11-02T13:00:00Z")
        f = json.loads(json.dumps(out["ledger/index"]))["fills"][0]
        # price 250 USD * 1.40 = 350 CAD per share; with 1.5% fee = 355.25 -> 28 shares
        self.assertEqual((f["symbol"], f["shares"], f["date"]), ("VTI", 28, "2026-11-02"))
        self.assertAlmostEqual(f["fx_cost_cad"], 28 * 350 * 0.015, places=2)

    def test_order_fills_at_first_close_after_receipt(self):
        self.m.order("holyfield-1", "holyfield", "BUY", "RY.TO", 10)
        out = self.m.run("2026-11-02T15:00:00Z")      # 10am ET, before the close
        f = stmt(out, "holyfield")["fills"][0]
        self.assertEqual((f["date"], f["price"], f["fx_cost_cad"]), ("2026-11-02", 150.0, 0.0))
        self.assertEqual(stmt(out, "holyfield")["cash_cad"], 10000 - 1500)

    def test_order_received_after_close_waits_for_next_day(self):
        self.m.order("marble-1", "marble", "BUY", "RY.TO", 10)
        out = self.m.run("2026-11-02T22:00:00Z")      # after Monday's close
        self.assertEqual(stmt(out, "marble")["fills"][0]["date"], "2026-11-03")
        self.assertEqual(stmt(out, "marble")["fills"][0]["price"], 151.0)

    def test_pending_when_price_not_in_yet(self):
        m = Market(days=1)
        m.run_record("marble-run1", "marble", "2026-11-02T14:00:00Z")
        m.order("marble-1", "marble", "BUY", "RY.TO", 10)
        out = m.run("2026-11-02T22:00:00Z")           # Tuesday's price doesn't exist yet
        self.assertEqual(len(stmt(out, "marble")["pending_orders"]), 1)
        self.assertEqual(stmt(out, "marble")["fills"], [])
        m.add_price(1)
        out = m.run("2026-11-03T22:00:00Z")
        self.assertEqual(stmt(out, "marble")["fills"][0]["date"], "2026-11-03")
        m.cleanup()

    def test_us_buy_and_sell_pay_conversion_both_ways(self):
        self.m.order("valeri-1", "valeri", "BUY", "AAPL", 5)
        self.m.run("2026-11-02T15:00:00Z")
        self.m.order("valeri-2", "valeri", "SELL", "AAPL", 5)
        out = self.m.run("2026-11-03T15:00:00Z")
        buy, sell = stmt(out, "valeri")["fills"]
        self.assertAlmostEqual(buy["net_cad"], -(5 * 200 * 1.4 * 1.015), places=2)
        self.assertAlmostEqual(sell["net_cad"], 5 * 202 * 1.4 * 0.985, places=2)

    def test_sells_processed_before_buys_same_day(self):
        self.m.order("bertolt-1", "bertolt", "BUY", "RY.TO", 13)      # ~19.5%
        self.m.run("2026-11-02T15:00:00Z")
        # Next day: sell RY, buy AAPL with the proceeds. Buy listed first on purpose.
        self.m.order("bertolt-2", "bertolt", "BUY", "AAPL", 6)
        self.m.order("bertolt-3", "bertolt", "SELL", "RY.TO", 13)
        out = self.m.run("2026-11-03T15:00:00Z")
        actions = [f["action"] for f in stmt(out, "bertolt")["fills"]]
        self.assertEqual(actions, ["BUY", "SELL", "BUY"])

    # ---------- Rejections ----------
    def test_rejects_bad_orders_with_reasons(self):
        m = self.m
        m.order("lauderdale-1", "lauderdale", "BUY", "NOPE", 1)           # not a real symbol
        m.order("lauderdale-2", "lauderdale", "BUY", "RY.TO", 2.5)        # fractional
        m.order("lauderdale-3", "lauderdale", "SELL", "RY.TO", 1)         # short sale
        m.order("lauderdale-4", "lauderdale", "BUY", "OTCX", 1)           # OTC
        m.order("lauderdale-5", "lauderdale", "BUY", "TINY.TO", 100)      # penny stock
        m.order("lauderdale-6", "lauderdale", "SHORT", "RY.TO", 1)        # not BUY/SELL
        m.order("lauderdale-7", "lauderdale", "BUY", "RY.TO", 14)         # 14*150=2100 > 20%
        m.order("lauderdale-8", "lauderdale", "BUY", "RY.TO", 1, run_id="lauderdale-ghost")  # no run
        m.order("holyfield-x", "lauderdale", "BUY", "RY.TO", 1)           # id not theirs
        out = m.run("2026-11-02T15:00:00Z")
        rs = reasons(out, "lauderdale")
        self.assertEqual(len(rs), 9, rs)
        joined = " | ".join(rs)
        for needle in ("was not found", "whole number", "can't sell", "OTC", "penny",
                       "BUY or SELL", "Position limit", "No matching run", "own agent name"):
            self.assertIn(needle, joined)
        self.assertEqual(stmt(out, "lauderdale")["cash_cad"], 10000)

    def test_rejects_when_not_enough_cash(self):
        # 5 buys of ~19% each are fine (first run is exempt from the weekly limit),
        # the 6th can't be paid for.
        syms = ["RY.TO", "AAPL", "VTI"]
        for i, (s, n) in enumerate([("RY.TO", 13), ("AAPL", 6), ("VTI", 5)]):
            self.m.order(f"holyfield-{i}", "holyfield", "BUY", s, n)
        out = self.m.run("2026-11-02T15:00:00Z")
        self.assertEqual(len(stmt(out, "holyfield")["fills"]), 3)
        self.m.write("symbols", "BIG", {"symbol": "BIG", "exchange": "NYSE", "currency": "CAD", "type": "stock"})
        self.m.add_price(1, BIG=1.5)
        self.m.order("holyfield-9", "holyfield", "BUY", "BIG", 5000, run_id="holyfield-run1")
        out = self.m.run("2026-11-03T15:00:00Z")
        self.assertIn("Not enough cash", " ".join(reasons(out, "holyfield")))

    def test_second_week_is_still_exempt(self):
        self.m.order("marble-1", "marble", "BUY", "RY.TO", 13)
        self.m.run("2026-11-02T15:00:00Z")
        self.m.run_record("marble-run2", "marble", "2026-11-09T14:00:00Z")   # 7 days later: inside 14-day grace
        self.m.order("marble-2", "marble", "BUY", "AAPL", 6, run_id="marble-run2")
        self.m.order("marble-3", "marble", "BUY", "VTI", 5, run_id="marble-run2")
        out = self.m.run("2026-11-09T15:00:00Z")
        self.assertEqual(reasons(out, "marble"), [])
        self.assertEqual(len(stmt(out, "marble")["fills"]), 3)

    def test_cboe_listed_etf_is_allowed(self):
        self.m.write("symbols", "MTUM", {"symbol": "MTUM", "exchange": "CBOE BZX", "currency": "USD", "type": "etf"})
        for i in range(10):
            self.m.add_price(i, MTUM=250.0)
        self.m.order("holyfield-1", "holyfield", "BUY", "MTUM", 3)
        out = self.m.run("2026-11-02T15:00:00Z")
        self.assertEqual(stmt(out, "holyfield")["fills"][0]["symbol"], "MTUM")

    def test_weekly_limit_applies_after_first_run(self):
        self.m.write("settings", "main", {"season_start": DATES[0], "season_end": "2027-10-29",
                                          "weekly_limit_grace_days": 3})
        self.m.order("marble-1", "marble", "BUY", "RY.TO", 13)    # first run: exempt anyway
        self.m.run("2026-11-02T15:00:00Z")
        self.m.run_record("marble-run2", "marble", "2026-11-09T14:00:00Z")
        # Week 2: try to buy ~$5,300 (more than 25% of ~$10k) across three orders.
        self.m.order("marble-2", "marble", "BUY", "AAPL", 6, run_id="marble-run2")   # ~1,700
        self.m.order("marble-3", "marble", "BUY", "VTI", 5, run_id="marble-run2")    # ~1,800
        self.m.order("marble-4", "marble", "BUY", "VTI", 1, run_id="marble-run2")    # ~360, fits
        self.m.write("symbols", "MSFT", {"symbol": "MSFT", "exchange": "NASDAQ", "currency": "USD", "type": "stock"})
        for i in range(5, 10):
            self.m.add_price(i, MSFT=100.0)
        self.m.order("marble-5", "marble", "BUY", "MSFT", 5, run_id="marble-run2")  # ~700 -> over 25%
        out = self.m.run("2026-11-09T15:00:00Z")
        rs = reasons(out, "marble")
        # cap ~ $2,513: AAPL ($1,764) + VTI x1 ($357) fit; VTI x5 and MSFT don't.
        self.assertEqual(len(rs), 2, rs)
        self.assertTrue(all("Weekly change limit" in r for r in rs), rs)
        self.assertEqual(len(stmt(out, "marble")["fills"]), 3)

    def test_cooling_off_after_reveal(self):
        self.m.write("reveals", "r1", {"created_at": "2026-11-04T18:00:00Z"})
        self.m.run_record("valeri-run2", "valeri", "2026-11-05T14:00:00Z")   # first run after reveal
        self.m.order("valeri-1", "valeri", "BUY", "RY.TO", 5, run_id="valeri-run2")
        out = self.m.run("2026-11-05T15:00:00Z")
        self.assertIn("Cooling-off", reasons(out, "valeri")[0])
        # The run after that is allowed.
        self.m.run_record("valeri-run3", "valeri", "2026-11-12T14:00:00Z")
        self.m.order("valeri-2", "valeri", "BUY", "RY.TO", 5, run_id="valeri-run3")
        out = self.m.run("2026-11-12T15:00:00Z")
        self.assertEqual(len(stmt(out, "valeri")["fills"]), 1)

    def test_statement_tells_agent_next_run_is_review_only(self):
        out = self.m.run("2026-11-02T15:00:00Z")
        self.assertFalse(stmt(out, "marble")["next_run_is_review_only"])
        self.m.write("reveals", "r1", {"created_at": "2026-11-04T18:00:00Z"})
        out = self.m.run("2026-11-04T19:00:00Z")
        self.assertTrue(stmt(out, "marble")["next_run_is_review_only"])

    # ---------- Dividends ----------
    def test_us_dividend_withholding_and_conversion(self):
        self.m.order("marble-1", "marble", "BUY", "AAPL", 6)
        self.m.order("marble-2", "marble", "BUY", "RY.TO", 10)
        self.m.write("dividends", "aapl-q4", {"symbol": "AAPL", "ex_date": "2026-11-05",
                                              "pay_date": "2026-11-12", "amount": 1.00})
        self.m.write("dividends", "ry-q4", {"symbol": "RY.TO", "ex_date": "2026-11-05",
                                            "pay_date": "2026-11-12", "amount": 1.50})
        self.m.run("2026-11-02T15:00:00Z")            # referee receives the orders Monday
        out = self.m.run("2026-11-13T22:00:00Z")
        d = {x["symbol"]: x for x in stmt(out, "marble")["dividends"]}
        self.assertAlmostEqual(d["AAPL"]["net_cad"], 6 * 1.0 * 0.85 * 1.4 * 0.985, places=2)
        self.assertEqual(d["RY.TO"]["net_cad"], 15.0)
        self.assertEqual(d["AAPL"]["date"], "2026-11-12")

    def test_no_dividend_if_bought_on_ex_date(self):
        self.m.run_record("marble-run2", "marble", "2026-11-05T14:00:00Z")
        self.m.order("marble-1", "marble", "BUY", "RY.TO", 10, run_id="marble-run2")
        self.m.write("dividends", "ry-q4", {"symbol": "RY.TO", "ex_date": "2026-11-05",
                                            "pay_date": "2026-11-12", "amount": 1.50})
        out = self.m.run("2026-11-05T15:00:00Z")
        self.assertEqual(stmt(out, "marble")["fills"][0]["date"], "2026-11-05")
        self.m.run("2026-11-13T22:00:00Z")
        out = self.m.run("2026-11-13T23:00:00Z")
        self.assertEqual(stmt(out, "marble")["dividends"], [])

    # ---------- Anti-cheating ----------
    def test_editing_an_order_after_receipt_is_ignored_and_flagged(self):
        self.m.order("holyfield-1", "holyfield", "BUY", "RY.TO", 5)
        self.m.run("2026-11-02T22:00:00Z")      # received after Monday close -> fills Tuesday
        # Agent tries to rewrite history: bigger order on a stock it now knows went up.
        self.m.order("holyfield-1", "holyfield", "BUY", "AAPL", 9)
        out = self.m.run("2026-11-03T22:00:00Z")
        fills = stmt(out, "holyfield")["fills"]
        self.assertEqual((fills[0]["symbol"], fills[0]["shares"]), ("RY.TO", 5))
        kinds = [i["kind"] for i in out["scoreboard/current"]["new_issues"]]
        self.assertIn("order_edited", kinds)

    def test_deleting_an_order_is_ignored_and_flagged(self):
        self.m.order("holyfield-1", "holyfield", "BUY", "RY.TO", 5)
        self.m.run("2026-11-02T15:00:00Z")
        self.m.delete("orders", "holyfield-1")
        out = self.m.run("2026-11-03T22:00:00Z")
        self.assertEqual(len(stmt(out, "holyfield")["fills"]), 1)
        self.assertIn("order_deleted", [i["kind"] for i in out["scoreboard/current"]["new_issues"]])

    def test_changed_price_and_personality_are_flagged(self):
        self.m.write("personalities", "valeri", {"temperament": "Skeptical"})
        self.m.run("2026-11-13T22:00:00Z")
        self.m.add_price(3, AAPL=999.0)
        self.m.write("personalities", "valeri", {"temperament": "Reckless"})
        out = self.m.run("2026-11-13T23:00:00Z")
        kinds = [i["kind"] for i in out["scoreboard/current"]["new_issues"]]
        self.assertIn("price_changed", kinds)
        self.assertIn("personality_changed", kinds)

    def test_late_price_for_new_symbol_is_not_flagged(self):
        self.m.add_price(0)
        self.m.run("2026-11-13T22:00:00Z")
        doc = json.loads((self.m.data / "prices" / "2026-11-02.json").read_text())
        doc["closes"]["MSFT"] = 100.0            # a close that arrived a few days late
        self.m.write("prices", "2026-11-02", doc)
        out = self.m.run("2026-11-13T23:00:00Z")
        self.assertEqual(out["scoreboard/current"]["new_issues"], [])

    def test_server_write_time_is_the_receipt_time(self):
        # Order written Monday 10am Pacific, but the referee first looks Tuesday afternoon.
        self.m.order("marble-1", "marble", "BUY", "RY.TO", 10)
        st = {"orders/marble-1": "2026-11-02T18:00:00Z", "runs/marble-run1": "2026-11-02T17:55:00Z"}
        out = self.m.run("2026-11-03T22:30:00Z", st)
        self.assertEqual(stmt(out, "marble")["fills"][0]["date"], "2026-11-02")
        # A claimed server time can never be later than when the referee actually looked.
        self.m.order("marble-2", "marble", "BUY", "RY.TO", 1)
        out = self.m.run("2026-11-04T15:00:00Z", {"orders/marble-2": "2030-01-01T00:00:00Z"})
        self.assertEqual(stmt(out, "marble")["fills"][1]["date"], "2026-11-04")

    def test_order_waits_for_its_own_fill_day_price(self):
        m = Market(days=0)
        m.run_record("marble-run1", "marble", "2026-11-02T14:00:00Z")
        m.order("marble-1", "marble", "BUY", "RY.TO", 10)
        # Monday's close for RY.TO is missing (late), Tuesday's is in.
        m.write("prices", DATES[0], {"date": DATES[0], "fx_usdcad": 1.4, "closes": {"VTI": 250.0}})
        m.add_price(1)
        out = m.run("2026-11-03T22:00:00Z", {"orders/marble-1": "2026-11-02T15:00:00Z"})
        self.assertEqual(stmt(out, "marble")["fills"], [])           # still waiting for Monday
        self.assertEqual(len(stmt(out, "marble")["pending_orders"]), 1)
        # Monday's close arrives late: the order fills at Monday's price.
        m.add_price(0)
        out = m.run("2026-11-04T22:00:00Z")
        f = stmt(out, "marble")["fills"][0]
        self.assertEqual((f["date"], f["price"]), ("2026-11-02", 150.0))
        m.cleanup()

    def test_fill_day_price_never_found_falls_back_after_patience(self):
        m = Market(days=0)
        m.run_record("marble-run1", "marble", "2026-11-02T14:00:00Z")
        m.order("marble-1", "marble", "BUY", "RY.TO", 10)
        for i in range(0, 7):
            closes = price_day(i)
            if i == 0:
                del closes["RY.TO"]
            m.write("prices", DATES[i], {"date": DATES[i], "fx_usdcad": 1.4, "closes": closes})
        out = m.run("2026-11-10T22:00:00Z", {"orders/marble-1": "2026-11-02T15:00:00Z"})
        f = stmt(out, "marble")["fills"][0]
        self.assertEqual(f["date"], DATES[5])           # 5 later closes seen -> fills at the 5th
        self.assertIn("fill_day_price_missing", [i["kind"] for i in out["scoreboard/current"]["new_issues"]])
        m.cleanup()

    def test_tsx_holiday_moves_fill_to_next_trading_day(self):
        m = Market(days=0)
        m.write("settings", "main", {"season_start": "2026-10-09", "season_end": "2027-10-29"})
        m.run_record("marble-run1", "marble", "2026-10-09T23:00:00Z")   # Friday evening
        m.order("marble-1", "marble", "BUY", "RY.TO", 10)
        for d in ("2026-10-09", "2026-10-12", "2026-10-13"):           # Mon 12 Oct = Canadian Thanksgiving
            closes = {"VTI": 250.0, "RY.TO": 150.0 if d != "2026-10-13" else 155.0}
            if d == "2026-10-12":
                closes = {"VTI": 251.0}                               # NYSE open, TSX closed
            m.write("prices", d, {"date": d, "fx_usdcad": 1.4, "closes": closes})
        out = m.run("2026-10-13T22:00:00Z", {"orders/marble-1": "2026-10-09T23:05:00Z"})
        f = stmt(out, "marble")["fills"][0]
        self.assertEqual((f["date"], f["price"]), ("2026-10-13", 155.0))
        m.cleanup()

    # ---------- Stock splits ----------
    def test_four_for_one_split_multiplies_shares_and_keeps_value(self):
        # Holyfield buys 3 AAPL on Mon (200 USD); AAPL splits 4-for-1 on Thu Nov 5.
        self.m.order("holyfield-1", "holyfield", "BUY", "AAPL", 3)
        for i in range(3, len(DATES)):
            self.m.add_price(i, AAPL=(200.0 + 2 * i) / 4)
        self.m.write("splits", "AAPL-2026-11-05", {"symbol": "AAPL", "ex_date": "2026-11-05", "ratio": 4.0})
        out = self.m.run("2026-11-02T15:00:00Z")
        st = stmt(out, "holyfield")
        h = [x for x in st["holdings"] if x["symbol"] == "AAPL"][0]
        self.assertEqual(h["shares"], 12)
        self.assertEqual(st["splits"][0]["shares_before"], 3)
        self.assertEqual(st["splits"][0]["shares_after"], 12)
        # Value on split day: 12 * 53.5 = 3 * 214 (no fake loss)
        vals = dict(out["ledger/holyfield"]["values"])
        self.assertAlmostEqual(vals["2026-11-05"], vals["2026-11-04"] + 3 * 2 * 1.4, places=2)

    def test_reverse_split_pays_cash_for_the_fraction(self):
        self.m.order("marble-1", "marble", "BUY", "RY.TO", 13)
        for i in range(3, len(DATES)):
            self.m.add_price(i, **{"RY.TO": (150.0 + i) * 10})
        self.m.write("splits", "RY.TO-2026-11-05", {"symbol": "RY.TO", "ex_date": "2026-11-05", "ratio": 0.1})
        out = self.m.run("2026-11-02T15:00:00Z")
        st = stmt(out, "marble")
        h = [x for x in st["holdings"] if x["symbol"] == "RY.TO"][0]
        self.assertEqual(h["shares"], 1)
        self.assertAlmostEqual(st["splits"][0]["cash_in_lieu_cad"], 0.3 * 1530.0, places=2)

    def test_daily_check_allows_for_split_in_price_move(self):
        import daily as D
        self.m.write("splits", "AAPL-2026-11-05", {"symbol": "AAPL", "ex_date": "2026-11-05", "ratio": 4.0})
        for i in range(3, len(DATES)):
            self.m.delete("prices", DATES[i])
        cand = {"closes": [{"date": "2026-11-05", "symbol": "AAPL", "close": 51.5, "source": "yahoo"}]}
        docs, report, *_ , problems, new_splits = D.check_candidates(self.m.data, cand, R.parse_time("2026-11-06T12:00:00Z"))
        self.assertIn("2026-11-05", docs, problems)
        self.assertEqual(docs["2026-11-05"]["closes"]["AAPL"], 51.5)

    def test_feed_dividends_flow_into_cash(self):
        import daily as D
        repo = self.m.dir / "repo"
        (repo / "data").mkdir(parents=True)
        (repo / "data" / "dividends.json").write_text(json.dumps({"RY.TO": {"2026-11-04": 1.50}}))
        self.m.order("valeri-1", "valeri", "BUY", "RY.TO", 10)
        self.m.run("2026-11-02T15:00:00Z")          # buys 10 RY.TO on Nov 2
        now = R.parse_time("2026-11-14T12:00:00Z")
        rc = D.repo_candidates(self.m.data, repo, now)
        self.assertEqual(rc["dividends"][0]["pay_date"], "2026-11-11")
        *_, new_divs, problems, _ = D.check_candidates(self.m.data, {"dividends": rc["dividends"]}, now)
        for k, doc in new_divs.items():
            self.m.write("dividends", k, doc)
        out = self.m.run("2026-11-14T12:00:00Z")
        d = stmt(out, "valeri")["dividends"][0]
        self.assertEqual((d["symbol"], d["date"], d["net_cad"]), ("RY.TO", "2026-11-11", 15.0))
        plan = D.plan(self.m.data, now, {}, repo)
        self.assertFalse(plan["dividend_check"]["due"])

    def test_orders_before_start_date_are_ignored(self):
        self.m.order("valeri-old", "valeri", "BUY", "RY.TO", 10)      # practice order, Nov 2
        out = self.m.run("2026-11-02T15:00:00Z")
        self.assertTrue(stmt(out, "valeri")["fills"])
        settings = json.loads((self.m.data / "settings" / "main.json").read_text())
        settings.update({"season_start": "2026-11-04", "season_end": "9999-12-31"})
        self.m.write("settings", "main", settings)
        out = self.m.run("2026-11-07T12:00:00Z")
        st = stmt(out, "valeri")
        self.assertEqual(st["fills"], [])
        self.assertEqual(st["holdings"], [])
        self.assertEqual(st["cash_cad"], 10000)
        ctrl = out["ledger/index"]
        self.assertEqual(ctrl["fills"][0]["date"], "2026-11-04")

    def test_rebuild_ignores_tampered_outputs(self):
        self.m.order("bertolt-1", "bertolt", "BUY", "RY.TO", 5)
        out1 = self.m.run("2026-11-02T15:00:00Z")
        # Someone writes a fake statement into the data folder. The referee never reads it.
        self.m.write("statements", "bertolt", {"cash_cad": 1_000_000})
        out2 = self.m.run("2026-11-02T16:00:00Z")
        self.assertEqual(stmt(out1, "bertolt")["cash_cad"], stmt(out2, "bertolt")["cash_cad"])

    def test_same_inputs_same_answer(self):
        self.m.order("holyfield-1", "holyfield", "BUY", "RY.TO", 5)
        a = self.m.run("2026-11-02T15:00:00Z")["scoreboard/current"]["rows"]
        b = self.m.run("2026-11-02T15:30:00Z")["scoreboard/current"]["rows"]
        self.assertEqual(a, b)

    # ---------- Scoring ----------
    def test_scoreboard_has_six_portfolios_and_ranks(self):
        out = self.m.run("2026-11-13T22:00:00Z")
        rows = out["scoreboard/current"]["rows"]
        self.assertEqual(sorted(r["portfolio"] for r in rows), sorted(R.PORTFOLIOS))
        self.assertEqual([r["rank"] for r in rows], [1, 2, 3, 4, 5, 6])
        idx = next(r for r in rows if r["portfolio"] == "index")
        self.assertIsNotNone(idx["score"])     # 10 days of history
        cash_agent = next(r for r in rows if r["portfolio"] == "marble")
        self.assertEqual(cash_agent["return"], 0.0)
        self.assertEqual(cash_agent["cash_pct"], 1.0)


if __name__ == "__main__":
    unittest.main()
