"""Capacity rotation: when the portfolio is full a clearly better idea may replace the weakest holding. A real replay showed GNRC bought at 13:45 and sold at 13:50 for a 'superior opportunity':
a pointless round trip. New positions are now protected for a while."""
import datetime as dt
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
from paper_agent import config, opportunity, settings as S  # noqa: E402

FORECAST = {"ev_net": 0.05, "sigma_h": 0.05, "horizon_days": 3}                      # candidate score = 1.0, well above the cash hurdle


def rem(pid, ticker, per_risk, age_h, remaining_ev=0.01):
    return {"position_id": pid, "ticker": ticker, "remaining_ev": remaining_ev, "per_risk": per_risk, "days_left": 2, "return": 0.0, "age_h": age_h}


def evaluate(holdings, hours=6, sizing_ok=False, headroom=0.0):
    cfg = H.cfg()
    cfg["risk"]["rotation_min_hold_hours"] = hours
    by = {h["position_id"]: h for h in holdings}
    with mock.patch.object(opportunity, "position_remaining", side_effect=lambda conn, h, now=None: by[h["position_id"]]):
        return opportunity.evaluate(H.fresh_db(), FORECAST, sizing_ok, {"holdings": [{"position_id": p} for p in by]}, cfg, None, headroom_pct=headroom)


class TestRotationProtectsNewPositions(unittest.TestCase):
    def test_the_weakest_holding_is_rotated_out_only_if_it_is_old_enough(self):
        young, old = rem(1, "GNRC", 0.10, age_h=0.1), rem(2, "SHEL", 0.40, age_h=30)
        r = evaluate([young, old], hours=6)
        self.assertEqual((r["rotate_out"], r["rotate_ticker"]), (2, "SHEL"))                         # the 5-minute-old GNRC is protected; the older, next-weakest holding goes
        r0 = evaluate([young, old], hours=0)
        self.assertEqual((r0["rotate_out"], r0["rotate_ticker"]), (1, "GNRC"))                       # protection off: the old behaviour (the churn that was found)

    def test_when_everything_is_new_nothing_is_rotated_and_it_says_why(self):
        r = evaluate([rem(1, "GNRC", 0.10, 0.1), rem(2, "MSFT", 0.20, 3.0)], hours=6)
        self.assertFalse(r["ok"])
        self.assertIsNone(r["rotate_out"])
        self.assertIn("too new to rotate out (protected for 6 hours", r["reason"])

    def test_the_boundary_and_a_clearly_not_superior_candidate(self):
        self.assertEqual(evaluate([rem(1, "A", 0.10, age_h=6.0)], hours=6)["rotate_out"], 1)          # exactly old enough: eligible
        self.assertIsNone(evaluate([rem(1, "A", 0.10, age_h=5.99)], hours=6)["rotate_out"])
        strong = rem(1, "A", 0.90, age_h=40)                                                          # candidate score 1.0 is NOT >= 1.5 x 0.90 + 0.05
        r = evaluate([strong], hours=6)
        self.assertIsNone(r["rotate_out"])
        self.assertIn("not superior", r["reason"])

    def test_free_capacity_means_no_rotation_at_all(self):
        r = evaluate([rem(1, "A", 0.10, age_h=40)], hours=6, sizing_ok=True, headroom=1.0)
        self.assertEqual((r["ok"], r["rotate_out"]), (True, None))                                   # room to buy: nothing is sold to make way
        full = evaluate([rem(1, "A", 0.10, age_h=40)], hours=6, sizing_ok=True, headroom=0.0)
        self.assertEqual(full["rotate_out"], 1)                                                      # no headroom: now rotation is considered

    def test_the_setting_exists_with_a_sane_range(self):
        base = config.load_config("/nonexistent", with_overrides=False)
        self.assertEqual(base["risk"]["rotation_min_hold_hours"], 6)
        f = next(x for x in S.FIELDS if x.path == "risk.rotation_min_hold_hours")
        self.assertEqual((S._parse(f, "0"), S._parse(f, "72")), (0, 72))
        for bad in ("-1", "73", "x"):
            with self.assertRaises(ValueError):
                S._parse(f, bad)


class TestAgeIsMeasuredFromTheRealPosition(unittest.TestCase):
    def test_position_remaining_reports_how_long_the_position_has_been_open(self):
        c = H.fresh_db()
        now = dt.datetime(2026, 10, 29, 14, 0, tzinfo=dt.timezone.utc)
        c.execute("INSERT INTO positions(id,ticker,opened,entry_price,shares,cost,status) VALUES(1,'NVDA',?,100,10,1000,'OPEN')", ((now - dt.timedelta(minutes=5)).isoformat(),))
        cols = [r[1] for r in c.execute("PRAGMA table_info(position_forecast_plans)")]
        info = list(c.execute("PRAGMA table_info(position_forecast_plans)"))
        vals = {r[1]: (0 if r[2].upper() in ("INTEGER", "REAL") else "x") for r in info if r[3] and r[4] is None and not r[5]}                  # every required column gets a harmless filler...
        vals.update({"position_id": 1, "created": now.isoformat(), "target_return": 0.03, "stop_return": -0.015, "max_hold_days": 3, "target_probability": 0.6, "active": 1})  # ...then the ones that matter
        use = {k: v for k, v in vals.items() if k in cols}
        c.execute("INSERT INTO position_forecast_plans(%s) VALUES(%s)" % (",".join(use), ",".join("?" * len(use))), tuple(use.values()))
        r = opportunity.position_remaining(c, {"position_id": 1, "ticker": "NVDA", "price": 101.0}, now)
        self.assertAlmostEqual(r["age_h"], 5 / 60.0, places=3)


if __name__ == "__main__":
    unittest.main()
