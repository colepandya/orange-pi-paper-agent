import datetime as dt, os, sys, tempfile, unittest, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from paper_agent import db, momentum, redteam


def make_db(n_days=330, syms=None, falling=()):
    d = tempfile.mkdtemp()
    conn = db.open_db(os.path.join(d, "t.db"), 1_000_000)
    days = []
    x = dt.date(2025, 1, 1)
    while len(days) < n_days:
        if x.weekday() < 5:
            days.append(x.isoformat())
        x += dt.timedelta(days=1)
    r = random.Random(7)
    for sym, drift in list((syms or {}).items()) + [("SPY", 0.0003)]:
        p = 50.0
        for i, dd in enumerate(days):
            p *= 1 + (drift if sym not in falling or i < n_days - 10 else -0.06) + r.gauss(0, 0.002)
            conn.execute("INSERT INTO market_series(symbol,day,open,high,low,close,volume) VALUES(?,?,?,?,?,?,?)", (sym, dd, p, p, p, p, 1_000_000))
    conn.commit()
    return conn, days


CFG = {"momentum": {"enabled": True, "names": 5, "leverage": 2.0}}
UNI = {"W%d" % i: 0.004 - i * 0.0004 for i in range(10)}      # W0 strongest ... W9 weakest


class Momentum(unittest.TestCase):
    def test_rank_orders_by_12_1_and_skips_junk(self):
        conn, days = make_db(syms=dict(UNI, CHEAP=0.01))
        conn.execute("UPDATE market_series SET close=2.0 WHERE symbol='CHEAP'")
        conn.execute("UPDATE market_series SET volume=1 WHERE symbol='W9'")
        conn.commit()
        t = [x["ticker"] for x in momentum.rank(conn, CFG, days[-1])]
        self.assertEqual(t[0], "W0")
        self.assertNotIn("CHEAP", t)
        self.assertNotIn("W9", t)
        self.assertNotIn("SPY", t)

    def test_rank_uses_no_future_data(self):
        conn, days = make_db(syms=UNI)
        a = momentum.rank(conn, CFG, days[-30])
        conn.execute("UPDATE market_series SET close=close*5 WHERE day>?", (days[-30],))
        conn.commit()
        self.assertEqual(a, momentum.rank(conn, CFG, days[-30]))

    def test_run_is_idempotent_and_leverage_capped(self):
        conn, days = make_db(syms=UNI)
        cfg = {"momentum": {"enabled": True, "names": 5, "leverage": 10.0}}          # asks for 10x: clamps to 3x
        a = momentum.run(conn, cfg)
        self.assertEqual(a["status"], "ok")
        self.assertLessEqual(a["gross"] / a["nav"], 3.0 + 1e-3)
        n = conn.execute("SELECT COUNT(*) FROM momentum_trades").fetchone()[0]
        self.assertEqual(momentum.run(conn, cfg)["status"], "up_to_date")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM momentum_trades").fetchone()[0], n)
        self.assertEqual(len(conn.execute("SELECT * FROM momentum_holdings").fetchall()), 5)

    def test_disabled_does_nothing(self):
        conn, days = make_db(syms=UNI)
        self.assertEqual(momentum.run(conn, {"momentum": {"enabled": False}})["status"], "disabled")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM momentum_trades").fetchone()[0], 0)

    def test_brake_cuts_leverage_after_a_crash(self):
        conn, days = make_db(syms=UNI, falling=tuple(UNI))
        momentum.run(conn, CFG, as_of=days[-12])
        out = momentum.run(conn, CFG, as_of=days[-1])
        self.assertGreater(out["drawdown"], 0.25)
        self.assertLess(out["cap"], 2.0)

    def test_financing_is_charged_on_borrowed_money(self):
        conn, days = make_db(syms=UNI)
        momentum.run(conn, CFG, as_of=days[-10])
        cash0 = momentum._get(conn, "cash")
        self.assertLess(cash0, 0)                                  # leveraged: cash is borrowed money
        momentum.run(conn, CFG, as_of=days[-9])
        self.assertLess(momentum._get(conn, "cash"), cash0 + 1e-6)

    def test_cap_for(self):
        s = momentum.settings(CFG)
        self.assertEqual(momentum.cap_for(0.0, 2.0, s), 2.0)
        self.assertEqual(momentum.cap_for(0.30, 2.0, s), 1.0)
        self.assertEqual(momentum.cap_for(0.45, 2.0, s), 0.0)
        self.assertEqual(momentum.cap_for(0.20, 1.0, s), 1.0)      # between 15% and 25%: stay at 1x
        self.assertEqual(momentum.cap_for(0.10, 1.0, s), 2.0)      # recovered to within 15%: back to full


class Tripwire(unittest.TestCase):
    def test_headline_must_name_the_company_and_state_a_fact(self):
        f = redteam.headline_is_about
        self.assertFalse(f("Elon Musk Warns Against Pushing Founders Out", "NVDA", "NVIDIA CORP"))
        self.assertFalse(f("BofA now sees Fed hiking in January", "BAC", "BANK OF AMERICA CORP /DE/"))
        self.assertFalse(f("Grid Dynamics (GDYN) Stock Sees Fair Value Lift After Higher Analyst Targets", "JPM", "JPMORGAN CHASE & CO"))
        self.assertFalse(f("What Analyst Projections for Key Metrics Reveal About Home Depot (HD) Q3 Earnings", "HD", "HOME DEPOT, INC."))
        self.assertTrue(f("Uber Jumps 7% on 20% Bookings Growth", "UBER", "Uber Technologies, Inc"))
        self.assertTrue(f("Nvidia raises full-year guidance", "NVDA", "NVIDIA CORP"))
        self.assertFalse(f(None, "NVDA", "NVIDIA CORP"))


if __name__ == "__main__":
    unittest.main()
