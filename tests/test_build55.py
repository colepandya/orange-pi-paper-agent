"""Build 55: capacity is a wait, not a rejection; a slot budget; the cash-hurdle verdict is an EDGE verdict; the EV model says what the OTHER side is worth; sell-the-news puts on by default."""
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402,F401
import test_guards as TG  # noqa: E402
import test_options as TO  # noqa: E402
import test_trade_lifecycle as TL  # noqa: E402
from paper_agent import config, ev, explain, opportunity, settings as S, settings_ui as U, sizing  # noqa: E402
from paper_agent.timeutil import utcnow  # noqa: E402

DEF = lambda: config.load_config("/nonexistent", with_overrides=False)  # noqa: E731
T_OPEN = TL.T_OPEN


class TestOpportunityOutcomes(unittest.TestCase):
    def ev(self, c, f, sizing_ok=True, holdings=None, headroom=0.02, min_pct=0.005, cfg=None, rem=None):
        state = {"holdings": holdings if holdings is not None else [], "equity": 1e6, "cash": 1e6}
        with unittest.mock.patch.object(opportunity, "position_remaining", lambda conn, h, now: dict(h)):
            return opportunity.evaluate(c, f, sizing_ok, state, cfg or DEF(), utcnow(), headroom_pct=headroom, min_pct=min_pct)

    def f(self, ev_=0.02):
        return {"horizon_days": 3, "ev_net": ev_, "sigma_h": 0.02}

    def test_below_the_cash_hurdle_is_an_edge_verdict(self):
        r = self.ev(H.fresh_db(), self.f(0.001))                                                            # hurdle = 4% x 3/252 + 0.1% = 0.148%
        self.assertEqual((r["ok"], r["kind"]), (False, "edge"))
        self.assertIn("does not beat cash hurdle", r["reason"])

    def test_room_is_ok_and_carries_no_kind(self):
        r = self.ev(H.fresh_db(), self.f())
        self.assertTrue(r["ok"])
        self.assertNotIn("kind", r)

    def test_every_way_a_full_book_says_no_is_a_capacity_wait(self):
        c = H.fresh_db()
        none = self.ev(c, self.f(), sizing_ok=False, holdings=[], headroom=0.0)
        self.assertEqual((none["ok"], none["kind"]), (False, "capacity"))
        self.assertIn("no capacity and no rotation candidate", none["reason"])
        new = self.ev(c, self.f(), sizing_ok=False, headroom=0.0, holdings=[{"ticker": "OLD", "position_id": 7, "age_h": 1.0, "per_risk": 0.2, "remaining_ev": 0.01}])
        self.assertEqual(new["kind"], "capacity")
        self.assertIn("too new to rotate out", new["reason"])
        strong = self.ev(c, self.f(), sizing_ok=False, headroom=0.0, holdings=[{"ticker": "OLD", "position_id": 7, "age_h": 10.0, "per_risk": 5.0, "remaining_ev": 0.05}])
        self.assertEqual(strong["kind"], "capacity")
        self.assertIn("not superior", strong["reason"])

    def test_a_broken_holding_can_be_rotated_out_at_any_age(self):
        c = H.fresh_db()
        broken = {"ticker": "OLD", "position_id": 7, "age_h": 0.5, "per_risk": -0.5, "remaining_ev": -0.01}                  # bought 30 minutes ago, its edge already gone
        r = self.ev(c, self.f(), sizing_ok=False, headroom=0.0, holdings=[broken])
        self.assertEqual((r["rotate_out"], r["rotate_ticker"], r.get("superior")), (7, "OLD", True))
        fresh_and_fine = dict(broken, per_risk=0.5, remaining_ev=0.03)
        self.assertIsNone(self.ev(c, self.f(), sizing_ok=False, headroom=0.0, holdings=[fresh_and_fine])["rotate_out"])           # the protection still holds for a healthy new position

    def test_the_minimum_position_now_defaults_to_half_a_percent(self):
        d = DEF()
        self.assertEqual((d["risk"]["min_position_pct"], d["risk"]["position_slots"]), (0.005, 40))
        self.assertTrue(self.ev(H.fresh_db(), self.f(), headroom=0.006, min_pct=None, cfg=d)["ok"])                  # 0.6% of room is enough now


class TestTheTraderMapsEachOutcomeToItsOwnStatus(unittest.TestCase):
    def run_world(self, opp):
        w = TG.World()
        w.setUp()
        self.addCleanup(w.tearDown)
        real = opportunity.evaluate
        calls = []

        def fake(*a, **k):
            calls.append(1)
            return opp
        with unittest.mock.patch.object(opportunity, "evaluate", fake):
            w.cyc(TG.TL.T_PRE, [TG.TL.NVDA_NEWS])
            w.cyc(TG.TL.T_OPEN)
        self.assertTrue(calls)
        return dict(w.c.execute("SELECT * FROM decisions WHERE ticker='NVDA' ORDER BY id LIMIT 1").fetchone())

    def test_an_edge_verdict_is_REJECTED_not_a_risk_rejection(self):
        d = self.run_world({"ok": False, "kind": "edge", "reason": "EV 0.10% does not beat cash hurdle 0.15%", "rotate_out": None})
        self.assertEqual((d["status"], d["action"]), ("REJECTED", "NO_TRADE"))

    def test_a_capacity_verdict_waits(self):
        d = self.run_world({"ok": False, "kind": "capacity", "reason": "no capacity and no rotation candidate", "rotate_out": None})
        self.assertEqual(d["status"], "AWAITING_CAPACITY")

    def test_a_verdict_without_a_kind_is_still_a_risk_rejection(self):
        d = self.run_world({"ok": False, "reason": "something else", "rotate_out": None})
        self.assertEqual(d["status"], "RISK_REJECTED")

    def test_a_real_full_book_queues_the_second_idea_instead_of_rejecting_it(self):
        w = TG.World()
        w.setUp()
        self.addCleanup(w.tearDown)
        w.cfg["risk"]["max_gross_pct"] = 0.05
        w.cfg["risk"]["slippage"] = 0
        w.cyc(TG.TL.T_PRE, [TG.TL.NVDA_NEWS, TG.SECOND])
        w.cyc(TG.TL.T_OPEN)
        w.cyc(TG.TL.T_OPEN + __import__("datetime").timedelta(minutes=40))
        statuses = [r[0] for r in w.c.execute("SELECT status FROM decisions")]
        self.assertIn("EXECUTED", statuses)
        self.assertNotIn("RISK_REJECTED", statuses)


class TestTheSlotBudget(unittest.TestCase):
    def size(self, slots):
        cfg = H.cfg()
        cfg["risk"]["position_slots"] = slots
        f = {"ticker": "XYZ", "verdict": "BUY", "ev_net": 0.04, "sigma_h": 0.05, "stop_return": -0.03, "confidence": 0.8, "risk_score": 20, "horizon_days": 3}
        return cfg, sizing.size_position(f, {"adv_usd": 5e9, "sector": "Tech", "beta": 1.0}, {"equity": 1e6, "cash": 1e6, "holdings": [], "scenario_probs": {}}, cfg, {"risk_mult": 1.0}, {"state": "Active", "score": 0.0, "primary": None})

    def test_no_new_position_may_take_more_than_its_share_of_the_invested_limit(self):
        cfg, s = self.size(20)
        cap = cfg["risk"]["max_gross_pct"] / 20
        mult = (0.9 * (0.85 + 0.15 * 0.5)) * 1.0 * (0.5 + 0.5 * 0.8) * (1.0 - 0.4 * 20 / 100.0)             # thesis x regime x confidence x risk = 0.6893
        self.assertAlmostEqual(s["pct"], cap * mult, places=6)
        self.assertTrue(any("slot budget" in r for r in s["reasons"]))

    def test_with_the_budget_off_the_same_idea_is_bigger(self):
        cfg, off = self.size(0)
        _, on = self.size(20)
        self.assertGreater(off["pct"], on["pct"])
        self.assertFalse(any("slot budget" in r for r in off["reasons"]))

    def test_a_small_book_budget_binds_harder(self):
        _, s5 = self.size(5)
        _, s20 = self.size(20)
        self.assertGreater(s5["pct"], s20["pct"])


class TestTheOtherSideIsSaid(TO.Base):
    def build(self, runup, hair=None, direction=1.0):
        import datetime as dt2
        self.c.execute("DELETE FROM market_series WHERE symbol='NVDA'")
        closes = [100.0 + (i % 2) for i in range(54)] + [100.0 * (1 + runup * i / 5) for i in range(6)]       # 54 quiet days (about 1% a day), then a run of `runup` over the last 5 sessions
        end = dt2.date(2026, 10, 19).toordinal()
        for i, cl in enumerate(closes):
            d = dt2.date.fromordinal(end - len(closes) + 1 + i).isoformat()
            self.c.execute("INSERT INTO market_series(symbol,day,open,high,low,close,volume) VALUES('NVDA',?,?,?,?,?,5e7)", (d, cl, cl * 1.005, cl * 0.995, cl))
        self.c.commit()
        if hair is not None:
            self.cfg["instruments"]["options_reversal_haircut"] = hair
        cand = {"ticker": "NVDA", "event_type": "guidance_raise", "direction": direction, "magnitude": 1.0, "conf": 0.9, "credibility": 0.95, "age_hours": 0.5, "horizon": 3, "headline": "NVIDIA outlook"}
        return ev.build(self.c, cand, self.cfg, {"regime": "neutral", "risk_mult": 0.8}, T_OPEN, None)

    def test_good_news_after_a_big_run_up_says_the_model_expects_a_fall_and_what_it_would_earn(self):
        self.cfg["instruments"]["options_reversal_haircut"] = 0.6
        f = self.build(0.25, hair=0.6)
        self.assertEqual(f["verdict"], "REJECT_NEGATIVE_EV")
        self.assertEqual(f["why"][0], "EV net %.2f%% <= 0" % (f["ev_net"] * 100))
        opp = f["inputs"]["opposite"]
        expect = (-f["ev_net"] - 2 * f["inputs"]["cost"]) * 0.6
        self.assertAlmostEqual(opp["ev_net_after_haircut"], expect)
        self.assertEqual((opp["side"], opp["haircut"]), ("SHORT", 0.6))
        self.assertGreater(expect, 0)
        self.assertEqual(f["why"][1], "the model expects a FALL: a short or put would earn about %+.1f%% after costs and a 60%% haircut" % (expect * 100))

    def test_when_the_opposite_side_does_not_pay_either_it_says_so(self):
        found = None
        for k in range(0, 301):                                                                              # 0.1% steps: the band where neither side pays is only about 0.4% of EV wide
            f = self.build(k / 1000.0, hair=0.6)
            if f["verdict"] == "REJECT_NEGATIVE_EV" and f["inputs"]["opposite"]["ev_net_after_haircut"] <= 0:
                found = f
                break
        self.assertIsNotNone(found, "a run-up that leaves a slightly negative EV must exist between 0% and 30%")
        self.assertTrue(found["why"][1].startswith("and the opposite side does not pay either (about "))

    def test_bad_news_on_a_fallen_stock_never_gets_a_bounce_side_because_that_direction_did_not_come_true(self):
        f = self.build(-0.25, hair=0.6, direction=-1.0)                                                       # a 25% fall, then bearish news: the model's priced-in term flips its forecast to a rise
        self.assertEqual(f["verdict"], "REJECT_NEGATIVE_EV")
        self.assertIsNone(f["inputs"]["opposite"])                                                            # on resolved forecasts those bounces did NOT happen (-2.1% against +8.5% predicted)
        self.assertEqual(len(f["why"]), 1)

    def test_an_ordinary_idea_has_no_opposite_side_note(self):
        f = self.build(0.0)
        self.assertIsNone(f["inputs"]["opposite"])
        self.assertEqual(len(f["why"]), 0 if f["verdict"] in ("BUY", "SHORT") else len(f["why"]))

    def test_the_decisions_tab_explains_both_lines_in_plain_english(self):
        g1 = "the model expects a FALL: a short or put would earn about +2.7% after costs and a 60% haircut"
        _, t1 = explain.translate_gate(g1)
        self.assertNotEqual(t1, g1)                                                                          # it was translated, not passed through
        for need in ("already priced in", "+2.7%", "60%"):
            self.assertIn(need, t1)
        g2 = "and the opposite side does not pay either (about -0.3% after costs and a 60% haircut)"
        _, t2 = explain.translate_gate(g2)
        self.assertNotEqual(t2, g2)
        for need in ("no edge in either direction", "-0.3%"):
            self.assertIn(need, t2)


class TestSettingsAndDefaults(unittest.TestCase):
    def test_sell_the_news_puts_ship_on_with_the_evidence_based_calibration(self):
        i = DEF()["instruments"]
        self.assertEqual((i["options_reversal_enabled"], i["options_reversal_min_move"], i["options_reversal_haircut"], i["options_reversal_per_trade_pct"], i["options_reversal_cooldown_hours"]), (True, 0.03, 0.6, 0.006, 24))

    def test_the_new_setting_and_the_layout(self):
        f = {x.path: x for x in S.FIELDS}
        self.assertIn("risk.position_slots", f)
        self.assertEqual((f["risk.position_slots"].lo, f["risk.position_slots"].hi), (0, 100))
        self.assertEqual(U.layout_problems(), [])


if __name__ == "__main__":
    unittest.main()


# ================================================================ the rest of Build 55: the original size, the small-trade breaker, the outcome breakdown, the 30-a-day defaults
import datetime as dt  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import tempfile  # noqa: E402

import test_build53 as T53  # noqa: E402
from paper_agent import dashboard, db, executor, funnel  # noqa: E402
from paper_agent.timeutil import iso  # noqa: E402


class TestTheOriginalSizeIsKept(unittest.TestCase):
    def test_older_rows_are_filled_in_from_their_decision_and_nothing_is_overwritten(self):
        path = tempfile.mkdtemp(prefix="pa-orig-", dir="/tmp") + "/t.db"
        c = db.open_db(path)
        c.execute("ALTER TABLE positions DROP COLUMN entry_shares")                                           # the schema before Build 55
        c.execute("ALTER TABLE positions DROP COLUMN entry_cost")
        for i, (sh, amt) in enumerate(((100, 5000.0), (7, 700.0)), 1):
            c.execute("INSERT INTO decisions(created,ticker,action,status,reason,shares,amount) VALUES(?,?,?,?,?,?,?)", ("2026-11-01T15:00:00+00:00", "T%d" % i, "BUY", "EXECUTED", "{}", sh, amt))
            did = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute("INSERT INTO positions(ticker,theme,opened,entry_price,shares,cost,decision_id,status,side) VALUES(?,?,?,?,?,?,?,'CLOSED','LONG')", ("T%d" % i, "t", "2026-11-01T15:00:00+00:00", 50.0, 0, 0.0, did))
        c.execute("INSERT INTO positions(ticker,theme,opened,entry_price,shares,cost,decision_id,status,side) VALUES('NOLINK','t','2026-11-01T15:00:00+00:00',1,0,0,NULL,'CLOSED','LONG')")
        c.commit()
        c.close()
        c2 = db.open_db(path)
        rows = [tuple(r) for r in c2.execute("SELECT ticker,entry_shares,entry_cost,shares,cost FROM positions ORDER BY id")]
        self.assertEqual(rows, [("T1", 100, 5000.0, 0, 0.0), ("T2", 7, 700.0, 0, 0.0), ("NOLINK", None, None, 0, 0.0)])           # the remaining quantity is still 0; the ORIGINAL is back
        c2.execute("UPDATE positions SET entry_shares=999 WHERE ticker='T1'")
        c2.commit()
        c2.close()
        self.assertEqual(db.open_db(path).execute("SELECT entry_shares FROM positions WHERE ticker='T1'").fetchone()[0], 999)           # reopening never overwrites a value that is there

    def test_a_real_position_records_its_size_and_a_real_stop_out_leaves_it_alone(self):
        w = TG.World()
        w.setUp()
        self.addCleanup(w.tearDown)
        T53.run_cycle(w)
        p = dict(w.c.execute("SELECT * FROM positions WHERE ticker='NVDA'").fetchone())
        d = dict(w.c.execute("SELECT shares,amount FROM decisions WHERE id=?", (p["decision_id"],)).fetchone())
        self.assertGreater(d["shares"], 0)
        self.assertEqual((p["entry_shares"], p["entry_cost"]), (d["shares"], d["amount"]))
        self.assertEqual((p["entry_shares"], p["entry_cost"]), (p["shares"], p["cost"]))                       # while it is open nothing has been sold
        executor.run(w.c, w.cfg, T_OPEN + dt.timedelta(minutes=120), H.Quotes({"NVDA": 1.0}))                   # the price collapses: the real stop-out path closes it
        q = dict(w.c.execute("SELECT * FROM positions WHERE id=?", (p["id"],)).fetchone())
        self.assertEqual((q["status"], q["shares"], q["cost"]), ("CLOSED", 0, 0))                              # the remaining quantity is zero BY DESIGN ...
        self.assertEqual((q["entry_shares"], q["entry_cost"]), (d["shares"], d["amount"]))                     # ... and the original size is still there


def small_trade(c, pnl, day, low=True, status="CLOSED"):
    reason = {"low_edge": {"cap_pct": 0.005, "why": ["x"]}} if low else {}
    c.execute("INSERT INTO decisions(created,ticker,action,status,reason,shares,amount) VALUES(?,?,?,?,?,?,?)", ("2026-11-01T15:00:00+00:00", "T", "BUY", "EXECUTED", json.dumps(reason), 10, 5000.0))
    did = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.execute("INSERT INTO positions(ticker,theme,opened,entry_price,shares,cost,decision_id,status,closed,pnl,side) VALUES('T','t','2026-11-01T15:00:00+00:00',50,0,0,?,?,?,?,'LONG')", (did, status, "2026-11-%02dT20:00:00+00:00" % day, pnl))


class TestTheSmallTradeBreaker(unittest.TestCase):
    def cfg(self, **kw):
        c = H.cfg()
        c["risk"].update(kw)
        return c

    def test_it_pauses_when_the_last_thirty_lost_more_than_half_a_percent_of_equity(self):
        c = H.fresh_db()
        for i in range(30):
            small_trade(c, -200.0, 1 + i % 28)                                                                   # 30 x -$200 = -$6,000
        msg = sizing.low_edge_breaker(c, self.cfg(), 1_000_000.0)
        self.assertEqual(msg, "the small-trade tier is paused: its last 30 closed trades lost $6000, more than 0.50% of equity")

    def test_it_does_not_pause_on_a_smaller_loss_a_shorter_record_or_other_trades(self):
        for pnl, n, low, want in ((-130.0, 30, True, None),                                                      # -$3,900: under $5,000
                                  (-200.0, 29, True, None),                                                      # only 29 closed small trades: not enough to judge
                                  (-500.0, 30, False, None)):                                                    # ordinary trades are not the small tier
            c = H.fresh_db()
            for i in range(n):
                small_trade(c, pnl, 1 + i % 28, low=low)
            self.assertEqual(sizing.low_edge_breaker(c, self.cfg(), 1_000_000.0), want, (pnl, n, low))

    def test_only_the_last_thirty_count_and_open_trades_do_not(self):
        c = H.fresh_db()
        for i in range(10):
            small_trade(c, -1000.0, 1 + i)                                                                       # old, bad (days 1-10) ...
        for i in range(30):
            small_trade(c, 10.0, 11 + i % 18)                                                                    # ... then 30 small winners (days 11-28)
        small_trade(c, -99999.0, 29, status="OPEN")                                                              # an open position has no result yet
        self.assertIsNone(sizing.low_edge_breaker(c, self.cfg(), 1_000_000.0))

    def test_switches_and_junk(self):
        c = H.fresh_db()
        for i in range(30):
            small_trade(c, -400.0, 1 + i % 28)
        self.assertIsNotNone(sizing.low_edge_breaker(c, self.cfg(), 1_000_000.0))
        self.assertIsNone(sizing.low_edge_breaker(c, self.cfg(low_edge_breaker_trades=0), 1_000_000.0))                  # 0 = never pause
        self.assertIsNone(sizing.low_edge_breaker(c, self.cfg(low_edge_breaker_trades=-1), 1_000_000.0))                 # a negative number is "off" too (SQLite would read LIMIT -1 as "no limit")
        for cfg in ({}, None, {"risk": None}, {"risk": {"low_edge_breaker_pct": "x"}}, {"risk": {"low_edge_breaker_trades": "many"}}):
            sizing.low_edge_breaker(c, cfg, 1_000_000.0)                                                         # never raises
        for eq in (0, -5, float("nan"), None, "x"):
            self.assertIsNone(sizing.low_edge_breaker(c, self.cfg(), eq))

    def test_through_a_real_cycle_a_paused_tier_rejects_small_ideas_but_not_ordinary_ones(self):
        for ev_net, paused in ((0.003, True), (0.02, False)):
            w = TG.World()
            w.setUp()
            self.addCleanup(w.tearDown)
            w.cfg["risk"]["low_edge_max_pct"] = 0.005
            w.cfg["risk"]["cash_hurdle_margin"] = 0.001
            for i in range(30):
                small_trade(w.c, -300.0, 1 + i % 28)                                                             # -$9,000 over the last 30 small trades
            w.c.commit()
            T53.run_cycle(w, T53.forecast_as(ev_net))
            d = dict(w.c.execute("SELECT * FROM decisions WHERE ticker='NVDA' ORDER BY id DESC LIMIT 1").fetchone())
            if paused:
                self.assertEqual(d["status"], "RISK_REJECTED")
                self.assertIn("small-trade tier is paused", d["reason"])
                self.assertEqual(w.one("SELECT COUNT(*) FROM positions WHERE ticker='NVDA'")[0], 0)
            else:
                self.assertEqual(d["status"], "EXECUTED")                                                        # a full-size idea is not a small trade: unaffected


def decision(c, status, reason, minutes_ago, ticker="AAA", now=None):
    now = now or T_OPEN
    c.execute("INSERT INTO decisions(created,ticker,action,status,reason) VALUES(?,?,?,?,?)", (iso(now - dt.timedelta(minutes=minutes_ago)), ticker, "BUY", status, reason))


class TestWhatHappenedToTheIdeas(unittest.TestCase):
    def world(self):
        c = H.fresh_db()
        g = lambda *x: json.dumps({"gates": list(x)})  # noqa: E731
        for t, why in (("ROKU", "capacity full and candidate not superior to weakest holding ROKU"), ("AMD", "capacity full and candidate not superior to weakest holding AMD"), ("MSFT", "capacity full and candidate not superior to weakest holding NVDA"),
                       ("A", "capacity full and candidate not superior to weakest holding ZZ"), ("B", "capacity full and candidate not superior to weakest holding YY"), ("PLTR", "primary thesis defence_expansion is Broken")):
            decision(c, "RISK_REJECTED", g(why), 30, t)
        for why in ("EV net 0.31% below minimum 0.60%", "EV net 0.05% below minimum 0.00%", "EV net -1.00% <= 0.00", "EV net -8.21% <= 0.00"):
            decision(c, "REJECTED", g(why), 60)
        decision(c, "VETOED", json.dumps({"veto": "later news points the other way"}), 90)
        decision(c, "DUPLICATE", g("already holding AAA (position #3)"), 100)
        for _ in range(2):
            decision(c, "EXECUTED", "{}", 120)
        decision(c, "REJECTED", g("old news"), 60 * 25)                                                         # 25 hours ago: outside the window
        decision(c, "RISK_REJECTED", g("old risk"), 60 * 30)
        c.commit()
        return c

    def test_counts_shares_and_reasons_by_hand(self):
        b = funnel.outcome_breakdown(self.world(), T_OPEN, 24.0)
        self.assertEqual(b["total"], 14)                                                                         # 6 + 4 + 1 + 1 + 2; the two old ones are excluded
        self.assertEqual(b["status"], [("RISK_REJECTED", 6, 42.9), ("REJECTED", 4, 28.6), ("EXECUTED", 2, 14.3), ("VETOED", 1, 7.1), ("DUPLICATE", 1, 7.1)])
        rr = b["reasons"]["RISK_REJECTED"]
        self.assertEqual(rr["total"], 6)
        self.assertEqual(rr["top"][0], ("capacity full and candidate not superior to weakest holding <STOCK>", 5, 83.3))      # five different stocks, one reason
        self.assertEqual(rr["top"][1], ("primary thesis defence_expansion is Broken", 1, 16.7))
        r = {g: (k, p) for g, k, p in b["reasons"]["REJECTED"]["top"]}
        self.assertEqual(r["EV net #% below minimum #%"], (2, 50.0))                                             # two different numbers, one reason
        self.assertEqual(r["EV net #% <= #"], (2, 50.0))
        self.assertEqual(b["reasons"]["VETOED"]["top"][0][0], "later news points the other way")
        self.assertNotIn("EXECUTED", b["reasons"])                                                               # only outcomes that did NOT trade get reasons

    def test_the_window_and_the_top_limit(self):
        c = self.world()
        self.assertEqual(funnel.outcome_breakdown(c, T_OPEN, 48.0)["total"], 16)                                  # a wider window brings the older two in
        small = funnel.outcome_breakdown(c, T_OPEN, 24.0, top=1)
        self.assertEqual((len(small["reasons"]["RISK_REJECTED"]["top"]), small["reasons"]["RISK_REJECTED"]["other"]), (1, 1))
        self.assertEqual(funnel.outcome_breakdown(H.fresh_db(), T_OPEN, 24.0), {"hours": 24.0, "total": 0, "status": [], "reasons": {}})

    def test_junk_window_and_limit_fall_back_to_24_hours_and_8_reasons(self):
        c = self.world()
        base = funnel.outcome_breakdown(c, T_OPEN, 24.0, 8)
        for hours in (None, "x", float("nan"), float("inf"), -5, 0, 1e9):
            self.assertEqual(funnel.outcome_breakdown(c, T_OPEN, hours, 8), dict(base, hours=base["hours"]), hours)
        for top in (None, "x", 0, -1, 1000, float("inf")):
            self.assertEqual(funnel.outcome_breakdown(c, T_OPEN, 24.0, top), base, top)

    def test_an_absurd_limit_is_capped_at_the_default_when_there_are_many_reasons(self):
        c = H.fresh_db()
        for i in range(12):
            decision(c, "REJECTED", json.dumps({"gates": ["distinct reason number %s" % ("x" * i)]}), 10)       # 12 different reasons (they differ in letters, which are not normalised away)
        c.commit()
        r = funnel.outcome_breakdown(c, T_OPEN, 24.0, 1000)["reasons"]["REJECTED"]
        self.assertEqual((len(r["top"]), r["other"]), (8, 4))                                                    # 1000 is not accepted; the default of 8 applies
        r = funnel.outcome_breakdown(c, T_OPEN, 24.0, 50)["reasons"]["REJECTED"]
        self.assertEqual((len(r["top"]), r["other"]), (12, 0))                                                   # 50 is within range: all 12 shown

    def test_malformed_records_are_grouped_not_fatal(self):
        c = H.fresh_db()
        for raw in (None, "", "not json", "[]", "123", json.dumps({"gates": "x"}), json.dumps({"gates": []}), json.dumps({"gates": [None]})):
            decision(c, "REJECTED", raw, 10)
        b = funnel.outcome_breakdown(c, T_OPEN, 24.0)
        self.assertEqual(b["total"], 8)
        self.assertEqual(funnel.reason_text("not json"), "(unreadable record)")
        self.assertEqual(funnel.reason_text(None), "(no reason recorded)")
        self.assertEqual(funnel.reason_text(json.dumps({"gates": ["first", "second"]})), "first")                    # the FIRST thing a decision recorded
        self.assertEqual(funnel.normalise_reason("moves almost exactly with MRNA, which is already held on the same side (correlation 0.97 over 60 sessions)"),
                         "moves almost exactly with <STOCK>, already held on the same side (correlation #)")

    def test_the_overview_shows_it_and_survives_a_malformed_record(self):
        import html
        page = html.unescape(re.sub(r"<[^>]+>", " ", dashboard.outcomes_section(self.world(), T_OPEN)))                  # the page escapes <STOCK>; compare the text a reader sees
        for need in ("What happened to the ideas", "14 decisions", "RISK_REJECTED: why (6)", "42.9%", "83.3%", "weakest holding <STOCK>", "EV net #% below minimum #%"):
            self.assertIn(need, page)
        self.assertIn("No decisions in the last 24 hours", re.sub(r"<[^>]+>", " ", dashboard.outcomes_section(H.fresh_db(), T_OPEN)))
        full = dashboard.render(self.world(), DEF(), "overview", {}, {"authed": True})
        self.assertIn("What happened to the ideas", full)
        with unittest.mock.patch.object(funnel, "outcome_breakdown", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bad record"))):
            self.assertIn("could not be displayed", dashboard.outcomes_section(H.fresh_db(), T_OPEN))


class TestTheThirtyADayDefaults(unittest.TestCase):
    def test_defaults_and_settings(self):
        d = DEF()
        self.assertEqual((d["portfolio"]["daily_trade_target"], d["risk"]["position_slots"], d["risk"]["low_edge_breaker_trades"], d["risk"]["low_edge_breaker_pct"]), (30, 40, 30, 0.005))
        f = {x.path: x for x in S.FIELDS}
        for p in ("risk.low_edge_breaker_trades", "risk.low_edge_breaker_pct"):
            self.assertIn(p, f)
        self.assertEqual((f["portfolio.daily_trade_target"].lo, f["portfolio.daily_trade_target"].hi), (0, 200))
        self.assertEqual(U.layout_problems(), [])
        self.assertAlmostEqual(d["risk"]["max_gross_pct"] / d["risk"]["position_slots"], 0.02, places=3) if d["risk"]["max_gross_pct"] == 0.8 else None


class TestTextIsEscapedExactlyOnce(unittest.TestCase):
    """table() escapes every cell itself; escaping first as well showed '&lt;=' on the page."""

    def test_the_outcome_reasons_show_angle_brackets_and_ampersands_as_typed(self):
        c = H.fresh_db()
        decision(c, "REJECTED", json.dumps({"gates": ["EV net -1.00% <= 0.00 & a <b> tag"]}), 5)
        c.commit()
        page = dashboard.outcomes_section(c, T_OPEN)
        self.assertIn("EV net #% &lt;= # &amp; a &lt;b&gt; tag", page)                                           # escaped ONCE for the browser ...
        self.assertNotIn("&amp;lt;", page)                                                                       # ... never twice
        self.assertNotIn("&amp;amp;", page)

    def test_the_research_tables_too(self):
        c = H.fresh_db()
        studies = {"post_move": {"study": "post_move", "title": "T <&>", "status": "ok", "rows": [{"label": "a<b&c", "sign": 1, "model_subtracts_pct": 1.0, "h": {"1": {"mean_pct": 1.0, "t": 2.0, "obs": 5, "days": 5, "train": None, "test": None}}}], "notes": [], "tests": 1}}
        c.execute("INSERT INTO research_runs(started,status,studies_json) VALUES(?,?,?)", (iso(T_OPEN), "complete", json.dumps(studies)))
        c.commit()
        page = dashboard.research_section(c, DEF())
        self.assertIn("a&lt;b&amp;c", page)
        self.assertNotIn("&amp;lt;", page)
        self.assertNotIn("&amp;amp;", page)
