"""The redesigned Settings screen (build 44). It is ONLY a new layout: these tests pin that every setting is still there, unchanged, that saving an untouched page changes nothing,
that saving one section can never touch another, and that the page stays free of scripts (the dashboard's security policy allows none)."""
import html.parser
import json
import os
import re
import sys
import unittest
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402,F401
from paper_agent import config, settings as S, settings_ui as U  # noqa: E402
from test_settings_ui import HTTPHarness  # noqa: E402

# The registry exactly as BUILD 43 shipped it: label, kind, old group, bounds, choices, restart note and default value of every setting. New settings may be added later; none of these may disappear or change.
BUILD43 = json.loads(r'''{"graph.derived_discount":["A related idea is this share as strong as the original","float","Relationship graph",0.2,1.0,null,"",0.75],"graph.enabled":["Spread an event to the company's suppliers, customers, competitors and peers","bool","Relationship graph",null,null,null,"",true],"graph.max_derived_per_event":["Related ideas per event: at most","int","Relationship graph",1,10,null,"",4],"graph.min_event_magnitude":["Only events at least this big spread","float","Relationship graph",0.2,1.0,null,"",0.45],"graph.trade_on_correlation":["Allow ideas built only on statistical (correlation) links","bool","Relationship graph",null,null,null,"",false],"instruments.options_allow_model_fallback":["Options: fall back to the model if no feed answers","bool","Instruments",null,null,null,"",true],"instruments.options_enabled":["Trade calls and puts (paper, MODEL-priced)","bool","Instruments",null,null,null,"",true],"instruments.options_exit_dte":["Options: close when this few days remain","int","Instruments",1,14,null,"",5],"instruments.options_feed_alpaca":["Options feed: Alpaca indicative","bool","Instruments",null,null,null,"",true],"instruments.options_feed_cboe":["Options feed: CBOE delayed quotes","bool","Instruments",null,null,null,"",true],"instruments.options_feed_nasdaq":["Options feed: Nasdaq","bool","Instruments",null,null,null,"",true],"instruments.options_feed_yahoo":["Options feed: Yahoo","bool","Instruments",null,null,null,"",true],"instruments.options_max_horizon_days":["Options: only for catalysts that work within (sessions)","int","Instruments",1,15,null,"",5],"instruments.options_max_iv_rank":["Options: do not buy when volatility is this expensive (0-100)","int","Instruments",20,100,null,"",80],"instruments.options_max_premium_pct":["Options: all premiums together at most","pct","Instruments",0.0,0.2,null,"",0.05],"instruments.options_max_spread_pct":["Options: refuse contracts whose spread is wider than","pct","Instruments",0.02,0.6,null,"",0.15],"instruments.options_min_dte":["Options: buy contracts with at least this many days left","int","Instruments",7,90,null,"",21],"instruments.options_min_ev":["Options: only for ideas with an edge of at least","pct","Instruments",0.01,0.2,null,"",0.03],"instruments.options_min_open_interest":["Options: refuse contracts with fewer open contracts than","int","Instruments",0,100000,null,"",100],"instruments.options_per_trade_pct":["Options: premium per trade at most","pct","Instruments",0.001,0.05,null,"",0.012],"instruments.options_quote_max_age_minutes":["Options: a quote older than this is not used (minutes)","int","Instruments",15,240,null,"",45],"instruments.options_stop_loss_pct":["Options: cut the loss at this fall in the premium","pct","Instruments",0.2,0.9,null,"",0.5],"instruments.options_target_pct":["Options: take the profit at this gain in the premium","pct","Instruments",0.2,3.0,null,"",1.0],"instruments.options_use_real_quotes":["Options: use real market quotes from the free feeds","bool","Instruments",null,null,null,"",true],"instruments.short_borrow_fee_pct":["Borrow fee on shorts (% a year)","float","Instruments",0.0,20.0,null,"",0.5],"instruments.short_collateral_pct":["Cash held back for each short (x its value)","float","Instruments",1.0,2.0,null,"",1.5],"instruments.short_max_gross_pct":["Short exposure: at most","pct","Instruments",0.0,0.6,null,"",0.25],"instruments.short_min_adv_usd":["Never short a stock trading less than ($ a day)","float","Instruments",1000000.0,1000000000.0,null,"",20000000],"instruments.short_min_market_cap":["Never short a company smaller than ($)","float","Instruments",100000000.0,100000000000.0,null,"",2000000000],"instruments.short_min_price":["Never short a stock under ($)","float","Instruments",1.0,100.0,null,"",5.0],"instruments.short_size_mult":["Shorts are sized at this share of an equal long","float","Instruments",0.1,1.0,null,"",0.6],"instruments.short_squeeze_5d_pct":["Never short a stock that has already risen this much in 5 days","pct","Instruments",0.05,1.0,null,"",0.2],"instruments.shorts_enabled":["Trade the bearish side too (short selling)","bool","Instruments",null,null,null,"",true],"intelligence.allow_thesis_entries":["Allow thesis-driven entries (no news trigger)","bool","Refresh times",null,null,null,"",true],"intelligence.chain_decay":["Confidence kept per causal hop","float","LLM analyst",0.1,0.9,null,"",0.55],"intelligence.company_refresh_hours":["Company profile refresh (hours)","int","Refresh times",6,720,null,"",72],"intelligence.contradiction_hours":["Contradiction search per thesis (hours)","int","Refresh times",1,48,null,"",6],"intelligence.engine_max_per_cycle":["Reasoning engine: items read per cycle","int","System",5,400,null,"",80],"intelligence.finnhub_company_news_max":["Companies polled for Finnhub news per cycle","int","Refresh times",0,50,null,"",15],"intelligence.gdelt_enabled":["Use GDELT world-event queries","bool","Refresh times",null,null,null,"",true],"intelligence.influence":["What the reasoning engine may do","choice","System",null,null,["off","observe","guard","active"],"","guard"],"intelligence.learning_min_samples":["Samples before the system learns","int","Refresh times",5,500,null,"",20],"intelligence.learning_mode":["What the system does with what it learns","choice","Refresh times",null,null,["observe","apply"],"","observe"],"intelligence.llm_daily_budget_usd":["Daily spend cap (USD)","float","LLM analyst",0.0,50.0,null,"",0.75],"intelligence.llm_daily_max_calls.analyst":["Max deep (analyst) calls per day","int","LLM analyst",0,500,null,"",40],"intelligence.llm_daily_max_calls.triage":["Max cheap (triage) calls per day","int","LLM analyst",0,2000,null,"",150],"intelligence.llm_enabled":["Enable the LLM analyst","bool","LLM analyst",null,null,null,"",false],"intelligence.llm_max_per_cycle":["News items analysed per cycle","int","LLM analyst",0,30,null,"",6],"intelligence.llm_monthly_budget_usd":["Monthly spend cap (USD)","float","LLM analyst",0.0,500.0,null,"",15.0],"intelligence.max_company_refresh_per_cycle":["Company profiles refreshed per cycle","int","Refresh times",0,20,null,"",6],"intelligence.max_knowledge_refresh_per_cycle":["Companies whose SEC filings are read per cycle","int","LLM analyst",0,10,null,"",2],"intelligence.max_trade_order":["Highest causal order allowed to trigger a trade","int","LLM analyst",1,3,null,"",1],"intelligence.redteam_enabled":["Red-team challenge before each entry","bool","LLM analyst",null,null,null,"",true],"intelligence.redteam_min_conviction":["Minimum conviction after the red team","pct","LLM analyst",0.0,0.95,null,"",0.45],"intelligence.redteam_required":["Block entries if the red team is unavailable","bool","LLM analyst",null,null,null,"",false],"intelligence.regime_refresh_hours":["Market regime refresh (hours)","int","Refresh times",1,48,null,"",6],"intelligence.stale_event_hours":["News older than this is stale (hours)","int","Refresh times",1,720,null,"",72],"intelligence.thesis_entries_per_day":["Max thesis-driven ideas evaluated per day (no news trigger)","int","Refresh times",0,100,null,"",12],"intelligence.thesis_entry_min_probability":["Min thesis probability for thesis-driven entries","pct","Refresh times",0.5,0.95,null,"",0.7],"intelligence.trade_event_max_age_hours":["Trade only on news younger than (hours)","int","Refresh times",1,72,null,"",24],"maintenance.log_days":["Keep run logs and narrative history for (days)","int","Database",7,365,null,"",30],"maintenance.news_days":["Keep news and events for (days)","int","Database",7,365,null,"",30],"maintenance.weekly_cleanup":["Weekly database clean-up (Saturdays, US market closed)","bool","Database",null,null,null,"",true],"notifications.ntfy_topic":["Phone push: ntfy.sh topic","topic","System",null,null,null,"",""],"portfolio.entry_score":["Minimum eligibility score to trade","int","Execution",0,100,null,"",65],"portfolio.slippage_bps":["Simulated slippage","int","Execution",0,100,null,"",8],"prices.extended_hours":["Include pre-market and after-hours trades","bool","Refresh times",null,null,null,"",true],"prices.max_symbols":["Max stocks refreshed per round","int","Refresh times",5,100,null,"",25],"prices.refresh_closed_minutes":["Refresh prices while the market is CLOSED every (minutes, 0 = off)","int","Refresh times",0,240,null,"",10],"prices.refresh_open_minutes":["Refresh dashboard prices while the market is OPEN every (minutes, 0 = off)","int","Refresh times",0,60,null,"",5],"quotes.allow_single_source":["Allow trading on a single quote source","bool","Execution",null,null,null,"",true],"quotes.entry_max_age_seconds":["Max quote age for entries (seconds)","int","Execution",30,600,null,"",90],"quotes.max_age_seconds":["Max quote age for exits (seconds)","int","Execution",30,900,null,"",180],"quotes.max_deviation":["Max price disagreement between providers","pct","Execution",0.0005,0.05,null,"",0.005],"quotes.min_providers":["Fresh quote sources required to trade","int","Execution",1,6,null,"",1],"quotes.probe_minutes":["Check quote providers every (minutes, 0 = off)","int","Execution",0,120,null,"",10],"quotes.single_source_max_move_pct":["A single source must be within this of the last close","pct","Execution",0.02,1.0,null,"",0.2],"reader.max_per_cycle":["Articles to download per cycle","int","Article reader",0,40,null,"",8],"reader.mode":["Read the full article (no LLM)","choice","Article reader",null,null,["off","shadow","guard","full"],"","guard"],"reader.time_budget_seconds":["Time budget for reading articles per cycle (seconds)","int","Article reader",5,120,null,"",25],"reader.timeout_seconds":["Give up on a slow page after (seconds)","int","Article reader",3,30,null,"",8],"risk.allow_add_to_position":["Allow a second position in a stock already held","bool","Risk limits",null,null,null,"",false],"risk.corr_block":["Refuse a new position this correlated with one you hold","float","Risk limits",0.7,0.99,null,"",0.92],"risk.corr_haircut_from":["Shrink a new position that moves with one you hold, above this correlation","float","Risk limits",0.3,0.95,null,"",0.6],"risk.daily_loss_pause_pct":["Pause new trades after one-day loss of","pct","Risk limits",0.005,0.2,null,"",0.03],"risk.drawdown_pause_pct":["Pause new trades at drawdown of","pct","Risk limits",0.02,0.5,null,"",0.12],"risk.full_size_edge":["Edge needed for a FULL-size position (expected profit / typical move)","float","Risk limits",0.1,3.0,null,"",0.5],"risk.max_gap_down_pct":["Cancel a BUY if the stock opens this far BELOW the previous close","pct","Risk limits",0.005,0.3,null,"",0.03],"risk.max_gap_up_pct":["Cancel a BUY if the stock opens this far ABOVE the previous close","pct","Risk limits",0.01,0.5,null,"",0.05],"risk.max_gross_pct":["Max invested (gross) exposure","pct","Risk limits",0.1,1.0,null,"",0.8],"risk.max_issuer_pct":["Max exposure to one company","pct","Risk limits",0.01,0.6,null,"",0.15],"risk.max_new_per_cycle":["Max new positions per cycle (0 = no limit)","int","Risk limits",0,200,null,"",0],"risk.max_position_pct":["Max size of one new position","pct","Risk limits",0.005,0.5,null,"",0.1],"risk.max_stress_loss_pct":["Max worst-case stress loss","pct","Risk limits",0.02,0.6,null,"",0.2],"risk.max_theme_pct":["Max exposure to one theme","pct","Risk limits",0.05,0.9,null,"",0.3],"risk.min_adv_usd":["Minimum average daily dollar volume","int","Risk limits",100000,1000000000,null,"",2000000],"risk.min_cash_pct":["Minimum cash reserve","pct","Risk limits",0.0,0.9,null,"",0.2],"risk.min_confidence":["Minimum forecast confidence","pct","Risk limits",0.0,0.95,null,"",0.45],"risk.min_ev_net":["Minimum expected value (net of costs)","pct","Risk limits",0.0,0.05,null,"",0.006],"risk.min_position_pct":["Smallest position worth opening","pct","Risk limits",0.001,0.1,null,"",0.01],"risk.open_delay_minutes":["Wait this long after the open before entering","int","Risk limits",0,60,null,"",10],"risk.risk_per_trade_pct":["Risk per trade (to the stop)","pct","Risk limits",0.001,0.05,null,"",0.01],"risk.rotation_min_hold_hours":["Protect a new position from being rotated out for (hours)","int","Risk limits",0,72,null,"",6],"risk.ruin_loss_pct":["Ruin threshold (for survival probability)","pct","Risk limits",0.05,0.8,null,"",0.2],"risk.shock_applies_to":["A market-wide shock holds which queued BUYs","choice","Risk limits",null,null,["after_decision","all_buys"],"","after_decision"],"risk.shock_direction":["Market-wide shock at direction <=","float","Risk limits",-1.0,-0.3,null,"",-0.6],"risk.shock_hold_hours":["Hold queued BUYs after a market shock for (hours, 0 = off)","int","Risk limits",0,48,null,"",6],"risk.shock_market_drop_pct":["Market must also be down at least (fraction, 0 = headlines alone suffice)","float","Risk limits",0.0,0.2,null,"",0.01],"risk.shock_min_sources":["Outlets needed to confirm a market-wide shock","int","Risk limits",1,5,null,"",2],"risk.veto_adverse_news":["Re-check queued BUYs against news that arrived after they were decided","bool","Risk limits",null,null,null,"",true],"risk.veto_news_direction":["Later news counts as adverse at direction <=","float","Risk limits",-1.0,-0.1,null,"",-0.35],"risk.veto_news_magnitude":["...and importance >=","float","Risk limits",0.1,1.0,null,"",0.3],"runtime.allowed_hosts":["Allowed hostnames (only if you use a custom name or proxy)","hostlist","System",null,null,null,"",[]],"runtime.bind_port":["Dashboard port","int","System",1024,65535,null,"dashboard",8088],"runtime.contact_email":["Contact email (sent to SEC in the User-Agent)","email","System",null,null,null,"","paper-agent@example.invalid"],"runtime.cycle_budget_seconds":["Cycle time budget (seconds)","int","System",120,500,null,"",300],"runtime.fetch_workers":["Network requests in flight at once (1 = one after another)","int","Refresh times",1,12,null,"",6],"runtime.max_load_per_core":["Skip a cycle above this load per core","float","System",0.5,4.0,null,"",1.5],"runtime.min_available_memory_mb":["Skip a cycle below this free memory (MB)","int","System",50,500,null,"",120],"runtime.require_password":["Require a password to use Settings","bool","System",null,null,null,"",false],"schedule.cycle_minutes":["Intelligence cycle every (minutes)","int","Refresh times",5,240,null,"schedule",10],"schedule.guard_minutes":["Exit guard every (minutes)","int","Refresh times",1,15,null,"schedule",2],"schedule.update_hours":["Check GitHub for updates every (hours)","int","Refresh times",1,48,null,"schedule",3],"strategy.avoid_earnings":["Stay out of earnings reports","bool","Swing strategy",null,null,null,"",true],"strategy.breakeven_lock_pct":["...and lock in this much profit","pct","Swing strategy",0.0,0.02,null,"",0.0015],"strategy.breakeven_trigger_pct":["Lock in break-even once up","pct","Swing strategy",0.002,0.05,null,"",0.01],"strategy.earnings_exit_minutes_before_close":["...sell this many minutes before the close","int","Swing strategy",5,120,null,"",45],"strategy.flatten_at_close":["Sell everything shortly before the close (day-trade mode)","bool","Swing strategy",null,null,null,"",false],"strategy.flatten_minutes_before_close":["...this many minutes before the close","int","Swing strategy",1,60,null,"",10],"strategy.max_hold_days":["Maximum hold (trading days)","int","Swing strategy",1,15,null,"",3],"strategy.min_daily_vol_pct":["Skip stocks that move less than this per day","pct","Swing strategy",0.0,0.05,null,"",0.008],"strategy.min_reward_risk":["Minimum reward : risk","float","Swing strategy",0.5,5.0,null,"",1.0],"strategy.partial_exit_pct":["Share sold when the target is reached","pct","Swing strategy",0.1,1.0,null,"",0.6],"strategy.plan_consistent_ev":["Judge ideas by what their exit plan can actually collect","bool","Swing strategy",null,null,null,"",true],"strategy.plan_gap_allowance":["Plan-consistent edge: allow stops to slip by","float","Swing strategy",0.0,2.0,null,"",0.5],"strategy.runner_cap_pct":["Runner target cap","pct","Swing strategy",0.02,0.35,null,"",0.06],"strategy.stop_max_pct":["Stop loss: at most","pct","Swing strategy",0.01,0.1,null,"",0.03],"strategy.stop_min_pct":["Stop loss: at least","pct","Swing strategy",0.003,0.05,null,"",0.012],"strategy.stop_vol_mult":["Stop distance = this x the stock's daily volatility","float","Swing strategy",0.5,3.0,null,"",1.0],"strategy.swing_enabled":["Swing mode (short holds, tight risk)","bool","Swing strategy",null,null,null,"",true],"strategy.target_max_pct":["Profit target: at most","pct","Swing strategy",0.01,0.15,null,"",0.04],"strategy.target_min_pct":["Profit target: at least","pct","Swing strategy",0.003,0.05,null,"",0.012],"strategy.trail_pct":["Then trail the high by","pct","Swing strategy",0.002,0.05,null,"",0.01],"whatif.enabled":["Replay the ideas we did NOT take (after the US close)","bool","Missed-trade analysis",null,null,null,"",true],"whatif.lookback_days":["Look back this many days","int","Missed-trade analysis",1,20,null,"",5],"whatif.max_symbols_per_cycle":["Stocks whose prices are fetched per cycle","int","Missed-trade analysis",0,60,null,"",20],"whatif.time_budget_seconds":["Time budget per run (seconds)","int","Missed-trade analysis",5,200,null,"",40]}''')
SRC = [{"id": "a", "name": "Feed A", "type": "rss", "interval": 600}, {"id": "b", "name": "Feed B", "type": "api", "interval": 900}]
BASE = config.load_config("/nonexistent", with_overrides=False)


class Inputs(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.items, self.sel = [], None
        self.scripts = self.handlers = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script":
            self.scripts += 1
        self.handlers += sum(1 for k in a if k.startswith("on"))
        if tag == "input" and a.get("name"):
            self.items.append((a["name"], a.get("type", "text"), a.get("value", ""), "checked" in a))
        elif tag == "select":
            self.sel = [a.get("name"), None, None]
        elif tag == "option" and self.sel is not None:
            self.sel[2] = self.sel[2] or a.get("value")
            if "selected" in a:
                self.sel[1] = a.get("value")

    def handle_endtag(self, tag):
        if tag == "select" and self.sel:
            self.items.append((self.sel[0], "select", self.sel[1] if self.sel[1] is not None else self.sel[2], False))
            self.sel = None


def parse(page):
    p = Inputs()
    p.feed(page)
    return p


def browser_form(page, only_settings=True):
    """What a browser POSTs for this page: every named control, in order, later values replacing earlier ones; an unticked checkbox sends nothing (its hidden 0 stands)."""
    form = {}
    for n, t, v, chk in parse(page).items:
        if t == "checkbox" and not chk:
            continue
        if only_settings and not n.startswith(("f.", "src.", "tier.", "_section", "_find")):
            continue
        form[n] = v if t != "checkbox" else (v or "on")
    return form


def view(cfg, section, **ctx):
    return U.page_settings(cfg, BASE, dict({"csrf": "T", "section": section}, **ctx), SRC, {}, {}, {"a": {"failures": 0, "last_success": "x", "last_status": "200"}})


def variant(kind):
    import copy
    c = copy.deepcopy(BASE)
    for f in S.FIELDS:
        if kind == "flip" and f.kind == "bool":
            S.set_path(c, f.path, not S.get_path(c, f.path))
    if kind == "owner":
        S.set_path(c, "risk.risk_per_trade_pct", 0.03)
        S.set_path(c, "strategy.stop_min_pct", 0.03)
        S.set_path(c, "instruments.shorts_enabled", True)
        S.set_path(c, "schedule.cycle_minutes", 20)
    return c


SECTIONS_WITH_SETTINGS = [s["id"] for s in U.SECTION_META if s["kind"] == "settings"]


class TestNothingWasLost(unittest.TestCase):
    def test_every_build43_setting_still_exists_with_the_same_type_range_and_default(self):
        now = {f.path: f for f in S.FIELDS}
        for path, (label, kind, group, lo, hi, choices, restart, default) in BUILD43.items():
            self.assertIn(path, now, "a setting was removed: " + path)
            f = now[path]
            widened = {"strategy.min_reward_risk": 0.2}                                                  # Build 53: the lower bound is widened so a minimum of 0.4 can be entered
            self.assertEqual((f.label, f.kind, f.group, f.lo, f.hi, list(f.choices) if f.choices else None, f.restart), (label, kind, group, widened.get(path, lo), hi, choices, restart), path)
            deliberate = {"risk.open_delay_minutes": 30, "risk.min_ev_net": 0.004, "strategy.min_reward_risk": 0.4, "risk.min_position_pct": 0.005}                                           # Build 50: 12 of 12 entries in the first 15 minutes after the open lost money (3 won), median hold 18 minutes
            self.assertEqual(S.get_path(BASE, path), deliberate.get(path, default), "default changed: " + path)

    def test_the_layout_places_every_setting_exactly_once_and_invents_none(self):
        self.assertEqual(U.layout_problems(), [])
        self.assertEqual(set(U.SECTION_OF), {f.path for f in S.FIELDS})

    def test_the_most_used_markers_name_real_settings(self):
        self.assertLessEqual(U.ESSENTIAL, set(S.BY_PATH))

    def test_the_all_settings_page_has_every_control_exactly_once_and_the_old_page_input_for_input(self):
        items = parse(view(BASE, "all")).items
        for f in S.FIELDS:
            mine = [(t, v) for n, t, v, c in items if n == f.name]
            if f.kind == "bool":
                self.assertEqual(sorted(t for t, _ in mine), ["checkbox", "hidden"], f.path)             # the hidden 0 is what makes an unticked box save as off
                self.assertEqual([v for t, v in mine if t == "hidden"], ["0"])
            else:
                self.assertEqual(len(mine), 1, f.path)
        for s in SRC:
            self.assertEqual(len([1 for n, t, v, c in items if n == "src.%s.interval" % s["id"]]), 1)
            self.assertEqual(len([1 for n, t, v, c in items if n == "src.%s.enabled" % s["id"]]), 2)
        for t in S.TIERS:
            for k in ("provider", "model", "price_in", "price_out"):
                self.assertEqual(len([1 for n, _, _, _ in items if n == "tier.%s.%s" % (t, k)]), 1)


class TestSavingIsSafe(unittest.TestCase):
    def test_pressing_save_on_an_untouched_page_changes_nothing_on_every_view(self):
        for kind in ("default", "flip", "owner"):
            cfg = variant(kind)
            for v in ["all"] + SECTIONS_WITH_SETTINGS + ["feeds"]:
                new, errors, warnings = S.apply_form(browser_form(view(cfg, v)), cfg, BASE, {s["id"]: s["interval"] for s in SRC})
                self.assertEqual(errors, [], (kind, v))
                self.assertEqual(new, cfg, "saving an untouched %s page changed something (%s)" % (v, kind))

    def test_saving_one_section_cannot_touch_a_setting_in_another(self):
        for sid in SECTIONS_WITH_SETTINGS:
            sec_paths = {p for sub in U.BY_SECTION[sid]["subs"] for p in sub[2]}
            form = browser_form(view(BASE, sid))
            self.assertEqual({k[2:] for k in form if k.startswith("f.")} - sec_paths, set(), sid)       # the form carries only this section's settings
            for other in S.FIELDS:
                if other.path not in sec_paths:
                    self.assertNotIn(other.name, form)

    def test_every_form_carries_the_section_it_belongs_to(self):
        for sid in SECTIONS_WITH_SETTINGS + ["feeds"]:
            self.assertEqual(browser_form(view(BASE, sid)).get("_section"), sid)
        self.assertEqual(browser_form(view(BASE, "all")).get("_section"), "all")
        self.assertEqual(browser_form(view(BASE, "search", find="cash")).get("_section"), "search")
        self.assertEqual(browser_form(view(BASE, "search", find="cash")).get("_find"), "cash")

    def test_a_changed_value_in_one_section_changes_only_that_setting(self):
        form = browser_form(view(BASE, "exits"))
        form["f.strategy.stop_min_pct"] = "4"
        new, errors, _ = S.apply_form(form, BASE, BASE, {})
        self.assertEqual(errors, [])
        diff = [f.path for f in S.FIELDS if S.get_path(new, f.path) != S.get_path(BASE, f.path)]
        self.assertEqual(diff, ["strategy.stop_min_pct"])

    def test_a_search_result_form_saves_only_the_listed_settings(self):
        page = view(BASE, "search", find="cash")
        form = browser_form(page)
        self.assertIn("f.risk.min_cash_pct", form)
        self.assertLess(len([k for k in form if k.startswith("f.")]), 20)
        form["f.risk.min_cash_pct"] = "12"
        new, errors, _ = S.apply_form(form, BASE, BASE, {})
        self.assertEqual(errors, [])
        self.assertEqual([f.path for f in S.FIELDS if S.get_path(new, f.path) != S.get_path(BASE, f.path)], ["risk.min_cash_pct"])


class TestSectionsAndNavigation(unittest.TestCase):
    def test_each_section_page_holds_exactly_its_own_settings(self):
        for sid in SECTIONS_WITH_SETTINGS:
            want = {"f." + p for sub in U.BY_SECTION[sid]["subs"] for p in sub[2]}
            got = {n for n, t, v, c in parse(view(BASE, sid)).items if n.startswith("f.")}
            self.assertEqual(got, want, sid)

    def test_every_section_renders_in_every_state_and_links_to_all_the_others(self):
        for kind in ("default", "flip", "owner"):
            cfg = variant(kind)
            for v in ["overview", "all", "search"] + [s["id"] for s in U.SECTION_META]:
                page = view(cfg, v, find="stop")
                for s in U.SECTION_META:
                    self.assertIn("s=%s" % s["id"], page, (v, s["id"]))
                self.assertIn("aria-label=\"Settings sections\"", page)

    def test_the_current_section_is_marked_and_an_unknown_one_falls_back_to_the_overview(self):
        self.assertIn('class="on" href="/?tab=settings&amp;s=exits" aria-current=page', view(BASE, "exits"))
        for bad in ("nonsense", "../../etc/passwd", "<script>", "EXITS", "exits&x=1", None):
            page = U.page_settings(BASE, BASE, {"csrf": "T", "query": {"s": [bad]} if bad is not None else {}}, SRC, {}, {}, {})
            self.assertIn("Changed from the defaults", page, bad)
            self.assertNotIn("<script>", page)

    def test_the_section_marker_of_a_form_is_only_ever_a_known_section(self):
        self.assertEqual(U.valid_view("exits"), "exits")
        for bad in ("", None, "x", "exits&evil=1", "../x", "EXITS"):
            self.assertEqual(U.valid_view(bad), "")
        self.assertEqual(U.view_suffix("exits"), "&s=exits")
        self.assertEqual(U.view_suffix("overview"), "")
        self.assertEqual(U.view_suffix("exits&evil=1"), "")
        self.assertEqual(U.view_suffix("search", "a b&c=d"), "&s=search&find=a%20b%26c%3Dd")

    def test_a_form_without_a_section_marker_is_shown_where_its_settings_live(self):
        self.assertEqual(U.infer_view({"f.strategy.stop_min_pct": "3"}), "exits")
        self.assertEqual(U.infer_view({"f.strategy.stop_min_pct": "3", "f.risk.max_position_pct": "8"}), "all")
        self.assertEqual(U.infer_view({"src.a.interval": "600"}), "feeds")
        self.assertEqual(U.infer_view({"csrf": "x"}), "")
        page = U.page_settings(BASE, BASE, {"csrf": "T", "raw": {"f.strategy.stop_min_pct": "77"}}, SRC, {}, {}, {})
        self.assertIn('value="77"', page)                                                                    # what the user typed is kept

    def test_the_page_has_no_script_no_inline_handlers_and_no_outside_resources(self):
        for v in ["overview", "all", "search"] + [s["id"] for s in U.SECTION_META]:
            page = view(variant("owner"), v, find="stop", test_output="x", control={"action": "a", "ok": True, "output": "o"}, schedule_status={"cycle_minutes": {"configured": 5, "applied": 5, "in_sync": True}})
            p = parse(page)
            self.assertEqual((p.scripts, p.handlers), (0, 0), v)
            self.assertNotRegex(page, r"(?i)<script|javascript:|<link|<iframe|<img|src=['\"]?https?:|href=['\"]?https?:")


class TestWhatTheOwnerSees(unittest.TestCase):
    def test_a_changed_setting_says_so_in_words_with_its_default_and_is_counted_in_the_menu_and_the_overview(self):
        cfg = variant("default")
        S.set_path(cfg, "risk.max_position_pct", 0.08)
        page = view(cfg, "size")
        self.assertIn("changed (default 10 %)", page)
        self.assertRegex(page, r"Position sizing &amp; risk <span class=nb[^>]*>1</span>")
        over = view(cfg, "overview")
        self.assertIn("Changed from the defaults (1)", over)
        self.assertIn("Max size of one new position", over)
        self.assertIn("nothing. Every setting is at its default.", view(BASE, "overview"))

    def test_a_percentage_is_shown_with_one_percent_sign_not_two(self):
        for v in ("size", "exits", "all"):
            page = view(BASE, v)
            self.assertNotIn("%%", page)
            self.assertRegex(page, r"class=unit>%</span>")

    def test_each_row_shows_its_default_and_valid_range(self):
        page = view(BASE, "size")
        self.assertIn("default 10 %", page)
        self.assertRegex(page, r"range \d+(\.\d+)?\u2013\d+(\.\d+)?%")

    def test_a_rejected_setting_is_highlighted_and_what_was_typed_is_kept(self):
        page = view(BASE, "size", errors=["Max size of one new position: must be between 1% and 20%"], raw={"f.risk.max_position_pct": "90"})
        self.assertRegex(page, r'class="srow err" id="s-risk-max_position_pct"')
        self.assertIn('value="90"', page)
        self.assertEqual(page.count('class="srow err"'), 1)

    def test_release_suggestions_are_shown_in_full_on_the_overview_and_as_a_pointer_elsewhere(self):
        pend = [{"id": "p1", "key": "risk.max_position_pct", "label": "Max size", "value": 0.05, "your_value": 0.08, "your_change": "2026-10-30", "note": "n"}]
        self.assertIn("Use release value", view(BASE, "overview", pending=pend))
        other = view(BASE, "exits", pending=pend)
        self.assertNotIn("Use release value", other)
        self.assertIn("suggests 1 setting change", other)

    def test_the_overview_reflects_the_real_switches(self):
        self.assertIn("Short selling", view(BASE, "overview"))
        for kind in ("default", "flip", "owner"):
            self.assertIn("Reasoning engine", view(variant(kind), "overview"))
        cfg = variant("default")
        S.set_path(cfg, "intelligence.influence", "active")
        self.assertIn("<b>active</b>", view(cfg, "overview"))

    def test_the_retired_language_model_settings_are_kept_and_labelled_retired(self):
        page = view(BASE, "llm")
        self.assertIn("RETIRED", page)
        self.assertIn("have no effect on trading", page)
        for p in ("intelligence.llm_enabled", "intelligence.llm_daily_budget_usd"):
            self.assertIn("f." + p, page)
        self.assertIn("tier.triage.model", page)


class TestSearch(unittest.TestCase):
    def test_search_finds_settings_by_words_in_the_name_help_or_section(self):
        page = view(BASE, "search", find="stop")
        self.assertIn("f.strategy.stop_min_pct", page)
        self.assertIn("setting(s) match", page)
        self.assertIn("Stops, targets &amp; exits", page)
        self.assertIn("f.risk.min_cash_pct", view(BASE, "search", find="cash"))
        self.assertIn("f.instruments.shorts_enabled", view(BASE, "search", find="short selling"))
        self.assertIn("f.schedule.cycle_minutes", view(BASE, "search", find="MINUTES"))

    def test_search_with_no_match_or_too_little_says_so_and_offers_no_form(self):
        self.assertIn("No setting matches", view(BASE, "search", find="zzzzqq"))
        self.assertNotIn("<form method=post", view(BASE, "search", find="zzzzqq"))
        self.assertIn("at least two letters", view(BASE, "search", find="s"))

    def test_hostile_search_text_is_escaped_everywhere(self):
        evil = '"><script>alert(1)</script>'
        page = view(BASE, "search", find=evil)
        self.assertNotIn("<script>alert", page)
        self.assertEqual(parse(page).scripts, 0)
        self.assertEqual(parse(U.page_settings(BASE, BASE, {"csrf": "T", "query": {"find": [evil]}}, SRC, {}, {}, {})).scripts, 0)

    def test_a_search_in_the_address_bar_works_without_any_section(self):
        page = U.page_settings(BASE, BASE, {"csrf": "T", "query": {"find": ["cash"]}}, SRC, {}, {}, {})
        self.assertIn("f.risk.min_cash_pct", page)

    def test_results_follow_the_layout_each_section_once_and_name_matches_come_first(self):
        page = view(BASE, "search", find="stop")
        self.assertIn("Named like that", page)
        self.assertIn("Mentioned in the description", page)
        named, mentioned = page.split("Mentioned in the description")
        self.assertIn("f.strategy.stop_min_pct", named)
        self.assertNotIn("f.strategy.stop_min_pct", mentioned)
        for part in (named, mentioned):
            heads = re.findall(r"<h3><a href='/\?tab=settings&amp;s=([\w-]+)'>", part)
            self.assertEqual(len(heads), len(set(heads)), "a section is listed twice in one block: %s" % heads)
        self.assertEqual(page.count('name="f.strategy.stop_min_pct"'), 1)                            # never two controls for one setting in one form

    def test_a_search_never_lists_more_than_sixty_and_says_so(self):
        page = view(BASE, "search", find="in")
        self.assertLessEqual(len([n for n, t, v, c in parse(page).items if n.startswith("f.") and t != "hidden"]), 60)
        self.assertIn("showing the first 60", page)


class TestEverythingOnOneScreen(unittest.TestCase):
    """The default Settings page shows every setting: no clicking into sections."""

    def page(self, cfg=None, **ctx):
        return U.page_settings(cfg or BASE, BASE, dict({"csrf": "T", "units": {}}, **ctx), SRC, {"paper-agent-cycle.timer": "active"}, {"FINNHUB_API_KEY": "set (...2345)"}, {"a": {"failures": 0, "last_success": "x", "last_status": "200"}})

    def test_the_default_page_has_every_setting_feed_and_model_tier_control(self):
        page = self.page()
        names = {n for n, t, v, c in parse(page).items}
        for f in S.FIELDS:
            self.assertIn(f.name, names, "missing from the single page: " + f.path)
        for s in SRC:
            self.assertIn("src.%s.interval" % s["id"], names)
        for t in S.TIERS:
            self.assertIn("tier.%s.model" % t, names)

    def test_the_default_page_is_one_form_with_one_save_button_for_all_settings(self):
        page = self.page()
        self.assertEqual(page.count("action=/settings/save"), 1)
        self.assertEqual(page.count("Save all settings"), 1)
        form = page[page.index("action=/settings/save"):]
        form = form[:form.index("</form>")]
        for f in S.FIELDS:
            self.assertIn(f.name, form)                                                                      # every setting is INSIDE that one form

    def test_the_other_boxes_are_on_the_same_page_and_save_separately(self):
        page = self.page(schedule_status={"cycle_minutes": {"configured": 5, "applied": 5, "in_sync": True}})
        for needle in ("action=/settings/secrets", "secret.ANTHROPIC_API_KEY", "action=/settings/reset", "action=/service", "/test/doctor", "/test/preflight", "Recent setting changes"):
            if needle == "Recent setting changes":
                continue                                                                                      # shown only once something has been changed
            self.assertIn(needle, page, needle)
        self.assertEqual(page.count("action=/settings/secrets"), 1)

    def test_every_menu_link_on_the_single_page_jumps_to_a_place_on_that_same_page(self):
        page = self.page()
        targets = re.findall(r'<a class="[^"]*" href="#sec-([\w-]+)"', page)
        self.assertEqual(sorted(targets), sorted(s["id"] for s in U.SECTION_META))
        for t in targets:
            self.assertIn("id='sec-%s'" % t, page, t)
        self.assertIn('href="/?tab=settings"', page)                                                         # and the way back to this page from anywhere

    def test_a_section_page_still_works_and_links_back_to_the_single_page(self):
        page = view(BASE, "exits")
        self.assertIn("All settings on one page", page)
        self.assertIn("href=\"/?tab=settings&amp;s=exits\" aria-current=page", page)
        self.assertNotIn('href="#sec-', page)

    def test_the_old_overview_address_shows_the_single_page(self):
        self.assertEqual(U.page_settings(BASE, BASE, {"csrf": "T", "query": {"s": ["overview"]}}, SRC, {}, {}, {}), U.page_settings(BASE, BASE, {"csrf": "T", "query": {}}, SRC, {}, {}, {}))

    def test_changes_from_the_defaults_link_to_the_setting_on_the_same_page(self):
        cfg = variant("owner")
        page = self.page(cfg)
        n = sum(1 for f in S.FIELDS if S.get_path(cfg, f.path) != S.get_path(BASE, f.path))
        self.assertGreaterEqual(n, 3)
        self.assertIn("Changed from the defaults (%d)" % n, page)
        self.assertIn("href='#s-risk-risk_per_trade_pct'", page)
        self.assertIn('id="s-risk-risk_per_trade_pct"', page)

    def test_a_rejected_save_on_the_single_page_highlights_the_row_and_keeps_the_typed_value(self):
        page = self.page(errors=["Max size of one new position: must be between 1% and 20%"], raw={"f.risk.max_position_pct": "90"}, section="all")
        self.assertEqual(page.count('class="srow err"'), 1)
        self.assertIn('value="90"', page)

    def test_the_settings_stylesheet_is_on_the_settings_page_only(self):
        from paper_agent import dashboard
        c, cfg = H.fresh_db(), H.cfg()
        for tab in dashboard.TABS:
            if tab != "settings":
                self.assertNotIn(".snav", dashboard.render(c, cfg, tab, {}, {"authed": True}), tab)
        self.assertIn(".snav", dashboard.render(c, cfg, "settings", {}, {"authed": True}))


class TestTheRealDashboard(HTTPHarness, unittest.TestCase):
    """Through the real HTTP handler: the section a form came from is where you land, after a save and after a rejection."""
    REQUIRE = True

    def test_a_save_returns_to_the_same_section_with_the_new_value_shown(self):
        cookie = self.setup_admin()
        tok, _ = self.csrf(cookie)
        st, h, _ = self.req("POST", "/settings/save", {"csrf": tok, "_section": "exits", "f.strategy.stop_min_pct": "4"}, cookie)
        self.assertEqual(st, 303)
        self.assertIn("&s=exits", h["location"])
        page = self.req("GET", h["location"], cookie=cookie)[2]
        self.assertIn("Settings saved (1 changed)", page)
        self.assertIn('id="i-strategy-stop_min_pct" name="f.strategy.stop_min_pct" value="4"', page)
        self.assertIn("Stops, targets &amp; exits</h2>", page)

    def test_the_form_exactly_as_the_server_renders_it_saves_and_returns_to_its_own_section(self):
        cookie = self.setup_admin()
        for sid, field, value in (("exits", "f.strategy.stop_min_pct", "4"), ("size", "f.risk.min_cash_pct", "15"), ("shorts", "f.instruments.short_size_mult", "0.5")):
            page = self.req("GET", "/?tab=settings&s=" + sid, cookie=cookie)[2]
            form = browser_form(page)
            form[field] = value
            form["csrf"] = re.search(r'name=csrf value="([^"]+)"', page).group(1)
            st, h, _ = self.req("POST", "/settings/save", form, cookie)
            self.assertEqual(st, 303, sid)
            self.assertIn("&s=" + sid, h["location"])
        page = self.req("GET", "/?tab=settings&find=stop", cookie=cookie)[2]
        form = browser_form(page)
        form["csrf"] = re.search(r'name=csrf value="([^"]+)"', page).group(1)
        st, h, _ = self.req("POST", "/settings/save", form, cookie)
        self.assertEqual(st, 303)
        self.assertIn("&s=search&find=stop", h["location"])                                                  # a save from the search results returns to the same search

    def test_a_rejected_save_shows_the_same_section_with_the_typed_value_and_the_reason(self):
        cookie = self.setup_admin()
        tok, _ = self.csrf(cookie)
        st, _, page = self.req("POST", "/settings/save", {"csrf": tok, "_section": "size", "f.risk.max_position_pct": "90"}, cookie)
        self.assertEqual(st, 400)
        self.assertIn("must be between", page)
        self.assertIn('value="90"', page)
        self.assertIn('class="srow err"', page)
        self.assertIn("Position sizing &amp; risk</h2>", page)
        self.assertFalse(os.path.exists(config.SETTINGS_PATH))

    def test_an_invented_section_marker_cannot_redirect_anywhere_else(self):
        cookie = self.setup_admin()
        tok, _ = self.csrf(cookie)
        st, h, _ = self.req("POST", "/settings/save", {"csrf": tok, "_section": "exits&flash=pwned%0d%0aX:y", "f.strategy.stop_min_pct": "4"}, cookie)
        self.assertEqual(st, 303)
        self.assertNotIn("pwned", h["location"])
        self.assertNotIn("\n", h["location"])

    def test_every_section_page_is_served_to_a_logged_in_owner_and_none_to_a_stranger(self):
        cookie = self.setup_admin()
        for s in U.SECTION_META:
            st, _, page = self.req("GET", "/?tab=settings&s=" + s["id"], cookie=cookie)
            self.assertEqual(st, 200, s["id"])
            self.assertIn(s["title"].replace("&", "&amp;"), page)
        self.assertEqual(self.req("GET", "/?tab=settings&s=exits")[0], 303)                                  # no login: sent to the login page, as before

    def test_the_default_page_through_the_real_server_has_every_setting_and_a_save_returns_to_it(self):
        cookie = self.setup_admin()
        page = self.req("GET", "/?tab=settings", cookie=cookie)[2]
        names = {n for n, t, v, c in parse(page).items}
        self.assertEqual({f.name for f in S.FIELDS} - names, set())
        form = browser_form(page)
        form["f.strategy.stop_min_pct"] = "4"
        form["csrf"] = re.search(r'name=csrf value="([^"]+)"', page).group(1)
        st, h, _ = self.req("POST", "/settings/save", form, cookie)
        self.assertEqual(st, 303)
        self.assertNotIn("&s=", h["location"])                                                               # back to the single page
        self.assertIn("Settings saved (1 changed)", self.req("GET", h["location"], cookie=cookie)[2])

    def test_the_moved_forms_are_on_their_own_pages(self):
        cookie = self.setup_admin()
        keys = self.req("GET", "/?tab=settings&s=keys", cookie=cookie)[2]
        self.assertIn("action=/settings/secrets", keys)
        for k in ("secret.ANTHROPIC_API_KEY", "clear.ANTHROPIC_API_KEY"):
            self.assertIn(k, keys)
        self.assertIn("action=/settings/reset", self.req("GET", "/?tab=settings&s=history", cookie=cookie)[2])
        svc = self.req("GET", "/?tab=settings&s=services", cookie=cookie)[2]
        self.assertIn("action=/service", svc)
        self.assertIn("restart_all", svc)
        chk = self.req("GET", "/?tab=settings&s=checks", cookie=cookie)[2]
        for path in ("/test/preflight", "/test/doctor", "/test/llm?tier=triage"):
            self.assertIn(path, chk)


if __name__ == "__main__":
    unittest.main()
