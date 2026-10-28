"""Randomised multi-day simulation: no stage may error, and the books must always reconcile."""
import datetime as dt
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
from paper_agent import db, pipeline, timeutil  # noqa: E402

HEADLINES = ["{c} beats estimates and raises full-year guidance", "{c} misses estimates and cuts outlook", "{c} wins $%d million contract from the Navy", "{c} announces public offering of shares",
             "{c} receives FDA approval for new device", "{c} faces antitrust probe", "{c} unveils new product line", "{c} announces share repurchase program",
             "Fed cuts interest rates, signals more easing", "Fed hawkish, rate hikes ahead", "China military drills around Taiwan escalate", "OPEC output cut lifts oil prices",
             "US announces new tariffs and export controls", "Ceasefire agreed, peace talks begin", "Hyperscalers raise AI capex, data center demand surges", "Analysts warn of AI bubble as capex cut feared",
             "Bank failure fears spread as credit spreads widen", "Nuclear power demand surges as small modular reactor approved", "Quarterly results land at the border of the second quarter"]
NAMES = {"NVDA": "NVIDIA", "LMT": "Lockheed Martin", "MSFT": "Microsoft", "XOM": "Exxon Mobil", "TGT": "Target Corp"}


class TestSoak(unittest.TestCase):
    def test_twenty_days(self):
        rnd = random.Random(7)
        c, cfg, U = H.fresh_db(), H.cfg(), H.universe()
        prices = {"NVDA": 120.0, "LMT": 480.0, "MSFT": 410.0, "XOM": 115.0, "TGT": 140.0}
        day = dt.datetime(2026, 10, 5, 14, 30, tzinfo=dt.timezone.utc)
        feed = []
        srcs = [{"id": "t", "name": "Wire", "type": "rss", "url": "http://x/rss", "trust": 0.85, "interval": 0}]
        cycles = errors = 0
        while day < dt.datetime(2026, 11, 3, tzinfo=dt.timezone.utc):
            if timeutil.is_trading_day(day.astimezone(timeutil.NY).date()):
                for hour_off in (0, 2, 5):
                    now = day + dt.timedelta(hours=hour_off)
                    for t in prices:
                        prices[t] *= 1 + rnd.gauss(0.0003, 0.012)
                    items = []
                    for _ in range(rnd.randint(2, 6)):
                        tmpl = rnd.choice(HEADLINES)
                        tk = rnd.choice(list(NAMES))
                        title = tmpl.replace("{c}", NAMES[tk])
                        if "%d" in title:
                            title = title % rnd.randint(50, 900)
                        pub = now - dt.timedelta(minutes=rnd.randint(1, 90))
                        items.append((title, "", pub.strftime("%a, %d %b %Y %H:%M:%S +0000")))
                    d = H.deps(items, dict(prices))
                    rep = pipeline.run_cycle(c, cfg, U, srcs, d, now)
                    cycles += 1
                    errors += len(rep["errors"])
                    self.assertEqual(rep["errors"], [], rep["stages"])
                    pipeline.run_guard(c, cfg, d, now + dt.timedelta(minutes=30))
                    self.check_invariants(c)
            day += dt.timedelta(days=1)
        self.assertGreater(cycles, 40)
        n_dec = c.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        n_ev = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.assertGreater(n_ev, 100)
        self.assertGreater(n_dec, 5)
        print("\n  soak: %d cycles, %d events, %d decisions, %d positions (%d closed), %d forecasts, %d predictions" % (
            cycles, n_ev, n_dec, c.execute("SELECT COUNT(*) FROM positions").fetchone()[0], c.execute("SELECT COUNT(*) FROM positions WHERE status='CLOSED'").fetchone()[0],
            c.execute("SELECT COUNT(*) FROM forecasts").fetchone()[0], c.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]))

    def check_invariants(self, c):
        start = db.start_capital(c)
        led = c.execute("SELECT COALESCE(SUM(amount),0) FROM ledger WHERE kind!='START'").fetchone()[0]
        self.assertAlmostEqual(db.cash(c), start + led, places=1)
        self.assertGreaterEqual(db.cash(c), 0)
        for p in c.execute("SELECT p.id,p.status,l.remaining_shares,l.remaining_cost FROM positions p JOIN position_lifecycle l ON l.position_id=p.id"):
            self.assertGreaterEqual(p["remaining_shares"], 0)
            if p["status"] == "CLOSED":
                self.assertAlmostEqual(p["remaining_cost"], 0, places=2)
        open_cost = c.execute("SELECT COALESCE(SUM(remaining_cost),0) FROM position_lifecycle l JOIN positions p ON p.id=l.position_id WHERE p.status='OPEN'").fetchone()[0]
        buys = -c.execute("SELECT COALESCE(SUM(amount),0) FROM ledger WHERE kind='VIRTUAL_BUY'").fetchone()[0]
        sells = c.execute("SELECT COALESCE(SUM(amount),0) FROM ledger WHERE kind LIKE 'VIRTUAL%SELL%'").fetchone()[0]
        released = c.execute("SELECT COALESCE(SUM(cost_released),0) FROM virtual_exit_actions WHERE status='COMMITTED'").fetchone()[0]
        self.assertAlmostEqual(start - buys + sells, db.cash(c), places=1)       # cash reconciles to the ledger
        self.assertAlmostEqual(buys - released, open_cost, delta=1.0)              # capital in = cost released out + still open
        self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        gross = sum(r[0] for r in c.execute("SELECT remaining_cost FROM position_lifecycle l JOIN positions p ON p.id=l.position_id WHERE p.status='OPEN'"))
        self.assertLessEqual(gross, start * 1.0)
        # single-name limit honoured at entry (allow drift from price moves)
        for r in c.execute("SELECT ticker,SUM(remaining_cost) v FROM position_lifecycle l JOIN positions p ON p.id=l.position_id WHERE p.status='OPEN' GROUP BY ticker"):
            self.assertLessEqual(r["v"], start * 0.16, r["ticker"])


if __name__ == "__main__":
    unittest.main()
