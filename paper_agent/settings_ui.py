"""HTML for the Settings area. Pure functions: easy to test, and secrets are only ever rendered as masked status text."""
from __future__ import annotations

import html
from typing import Dict, List, Optional

from . import settings as S

E = lambda x: html.escape("" if x is None else str(x), quote=True)  # noqa: E731
FORM_CSS = """input[type=text],input[type=password],input[type=email],select{padding:5px 7px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit}
input.num{width:110px;text-align:right}input.wide{width:100%;max-width:360px}.flash{padding:9px 12px;border-radius:6px;margin:0 0 12px;background:var(--card);border:1px solid var(--line)}
.flash.err{border-color:var(--bad);color:var(--bad)}.flash.warn{border-color:var(--warn);color:var(--warn)}.flash.ok{border-color:var(--ok);color:var(--ok)}
.help{color:var(--mut);font-size:12px}.chg{color:var(--warn);font-size:12px}.savebar{position:sticky;bottom:0;background:var(--bg);padding:10px 0;border-top:1px solid var(--line)}
button.primary{background:var(--acc);color:#fff;border:0;border-radius:6px;padding:8px 16px;font-weight:600;cursor:pointer}button.danger{background:var(--bad);color:#fff;border:0;border-radius:6px;padding:6px 12px;cursor:pointer}
button.sec{border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:6px;padding:6px 12px;cursor:pointer}.card2{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;margin:8px 0}"""


def csrf_input(csrf: str) -> str:
    return '<input type=hidden name=csrf value="%s">' % E(csrf)


def flash_html(flash: Optional[str], errors: Optional[List[str]] = None, warnings: Optional[List[str]] = None) -> str:
    out = ""
    if flash:
        out += '<div class="flash ok">%s</div>' % E(flash[:400])
    for e in (errors or []):
        out += '<div class="flash err">%s</div>' % E(e)
    for w in (warnings or []):
        out += '<div class="flash warn">%s</div>' % E(w)
    return out


def page_login(flash: str = "", locked: bool = False) -> str:
    return flash_html(flash) + """<div class=card2 style="max-width:380px"><h2>Log in</h2>%s<form method=post action=/login>
<p><input type=password name=password placeholder="Admin password" class=wide autofocus autocomplete=current-password></p><button class=primary>Log in</button></form>
<p class=help>Settings, API keys and service control need this password. The connection is plain HTTP on your LAN.</p></div>""" % ('<p class=bad>Too many attempts. Wait two minutes.</p>' if locked else "")


def page_setup(flash: str = "") -> str:
    return flash_html(flash) + """<div class=card2 style="max-width:420px"><h2>Create the admin password</h2>
<p class=help>First-time setup. Anyone on your network who reaches this page first could claim it, so do it now. Minimum %d characters; use one you do not reuse elsewhere.</p>
<form method=post action=/setup><p><input type=password name=password placeholder="New password" class=wide autocomplete=new-password></p>
<p><input type=password name=confirm placeholder="Repeat password" class=wide autocomplete=new-password></p><button class=primary>Create password</button></form></div>""" % 10


def _when(m: Optional[dict]) -> str:
    if not m:
        return ""
    from .timeutil import to_nz
    who = {"user": "you", "deploy": "a release"}.get(m.get("by"), m.get("by") or "?")
    return '<span class=help> &middot; %s %s by %s%s</span>' % ("reset" if m.get("removed") else "changed", E(to_nz(m.get("modified"), "%d %b %Y, %H:%M")), E(who), (" (%s)" % E(m["note"])) if m.get("note") else "")


def _fmt(path: str, v) -> str:
    f = S.BY_PATH.get(path)
    if f is None:
        return str(v)
    if f.kind == "pct" and isinstance(v, (int, float)):
        return ("%.4f" % (v * 100)).rstrip("0").rstrip(".") + " %"
    if f.kind == "bool":
        return "on" if v else "off"
    if f.kind == "hostlist":
        return ", ".join(v) or "none"
    return str(v)


def pending_panel(pending: List[dict], csrf: str) -> str:
    if not pending:
        return ""
    rows = ""
    for p in pending:
        rows += ("<tr><td>%s<br><span class=help>%s</span></td><td>%s<br><span class=help>%s</span></td><td><b>%s</b></td><td>"
                 "<form method=post action=/settings/deploy-accept style='display:inline'>%s<input type=hidden name=id value='%s'><button class=primary>Use release value</button></form> "
                 "<form method=post action=/settings/deploy-dismiss style='display:inline'>%s<input type=hidden name=id value='%s'><button class=sec>Keep mine</button></form></td></tr>"
                 % (E(p.get("label")), E(p.get("note") or ""), E(_fmt(p["key"], p.get("your_value"))), E("you changed this " + str(p.get("your_change") or "")[:10]), E(_fmt(p["key"], p.get("value"))), csrf_input(csrf), E(p["id"]), csrf_input(csrf), E(p["id"])))
    return ('<div class=card2><b>The latest release suggests %d setting change(s)</b><p class=help>You changed these yourself, so nothing was overwritten. Choose per setting.</p>'
            "<table><tr><th>Setting</th><th>Your value</th><th>Release suggests</th><th></th></tr>%s</table></div>" % (len(pending), rows))


def changes_panel(meta: Dict[str, dict], limit: int = 15) -> str:
    items = sorted(((k, v) for k, v in (meta or {}).items() if v.get("modified")), key=lambda kv: kv[1]["modified"], reverse=True)[:limit]
    if not items:
        return ""
    from .timeutil import to_nz
    rows = "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (E(k), E(to_nz(v["modified"], "%d %b %Y, %H:%M")), E({"user": "you", "deploy": "a release"}.get(v.get("by"), v.get("by"))), E(("removed. " if v.get("removed") else "") + (v.get("note") or ""))) for k, v in items)
    return "<h2>Recent setting changes</h2><div class=wrap><table><tr><th>Setting</th><th>When</th><th>By</th><th>Note</th></tr>%s</table></div><p class=help>Releases never overwrite a setting you changed; they can only suggest (above) unless a release explicitly forces a value once.</p>" % rows


def _tiers(cfg: dict, raw: Dict[str, str]) -> str:
    rows = ""
    for t in S.TIERS:
        tier = cfg["intelligence"]["llm_tiers"][t]
        price = cfg["intelligence"]["llm_prices"].get(tier["model"], ["", ""])
        g = lambda k, d: E(raw.get("tier.%s.%s" % (t, k), d))  # noqa: E731
        opts = "".join('<option value="%s"%s>%s</option>' % (p, " selected" if raw.get("tier.%s.provider" % t, tier["provider"]) == p else "", p) for p in S.PROVIDERS)
        rows += ("<tr><td><b>%s tier</b><br><span class=help>%s</span></td><td>provider <select name='tier.%s.provider'>%s</select> model <input type=text class=wide style='max-width:230px' name='tier.%s.model' value='%s'><br>"
                 "price per million tokens: in <input type=text class=num style='width:70px' name='tier.%s.price_in' value='%s'> out <input type=text class=num style='width:70px' name='tier.%s.price_out' value='%s'> USD</td></tr>"
                 % (t, "cheap, high-volume headline reading" if t == "triage" else "deep analysis, red-team and filings", t, opts, t, g("model", tier["model"]), t, g("price_in", price[0]), t, g("price_out", price[1])))
    return rows


DEAD_CODES = ("404", "410")


def dead_feed_ids(sources: List[dict], cfg: dict, health: Dict[str, dict]) -> List[str]:
    """Feeds that are still enabled but have failed with 404/410 (permanently gone) at least 3 times."""
    so = cfg.get("source_overrides", {})
    return [s["id"] for s in sources if so.get(s["id"], {}).get("enabled", True)
            and str((health.get(s["id"]) or {}).get("last_status")) in DEAD_CODES and int((health.get(s["id"]) or {}).get("failures") or 0) >= 3]


def _feed_status(h: Optional[dict]) -> str:
    if not h:
        return '<span class=mut>not polled yet</span>'
    if int(h.get("failures") or 0) == 0 and h.get("last_success"):
        return '<span class=ok>OK</span>'
    return '<span class=warn>failing x%s (%s)</span>' % (E(h.get("failures")), E(h.get("last_status")))


# ================================================================================================================================ the Settings screen (build 44)
# The registry in settings.py (every setting, its type, range and default) is UNCHANGED. This is only how the page is laid out: which heading a setting appears under, in what order, and how a row looks.
# The dashboard sends `Content-Security-Policy: default-src 'none'`: NO JavaScript runs, so everything here is plain HTML and CSS plus server-side navigation (one section per page view, a server-side search).
NAV_GROUPS = ["Trading", "Instruments", "Intelligence", "Data & system", "Connections", "Operations", "Retired"]


def _sec(id_, group, title, blurb, subs, kind="settings"):
    return {"id": id_, "group": group, "title": title, "blurb": blurb, "subs": subs, "kind": kind}


SECTION_META = [
    _sec("size", "Trading", "Position sizing & risk", "How much the bot risks, and how big any trade or exposure is allowed to get.", [
        ("How big each trade is", "", ["risk.risk_per_trade_pct", "risk.max_position_pct", "risk.min_position_pct", "risk.position_slots", "risk.full_size_edge", "risk.allow_add_to_position", "risk.add_cooldown_hours", "risk.add_only_when_winning"]),
        ("Concentration limits", "Caps on how much can sit in one company, one theme, or the whole book.", ["risk.max_issuer_pct", "risk.max_theme_pct", "risk.max_gross_pct"]),
        ("Cash and stress buffers", "", ["risk.min_cash_pct", "risk.max_stress_loss_pct", "risk.ruin_loss_pct"]),
        ("Circuit breakers", "New trades pause automatically when losses reach these levels.", ["risk.drawdown_pause_pct", "risk.daily_loss_pause_pct"])]),
    _sec("entry", "Trading", "Entry rules", "What an idea has to pass before the bot buys it.", [
        ("Quality bar", "An idea is skipped unless it clears all of these.", ["risk.min_ev_net", "risk.min_confidence", "portfolio.entry_score", "strategy.min_reward_risk", "strategy.min_daily_vol_pct", "risk.min_adv_usd", "risk.cash_hurdle_margin"]),
        ("Low-edge ideas are traded small", "An idea that clears the quality bar but not the full-size bar below gets a small position, so more trades cost little.", ["risk.full_size_min_ev", "risk.full_size_min_rr", "risk.low_edge_max_pct", "risk.low_edge_breaker_trades", "risk.low_edge_breaker_pct"]),
        ("Activity target", "", ["portfolio.daily_trade_target"]),
        ("How an idea is judged", "", ["strategy.plan_consistent_ev", "strategy.plan_gap_allowance"]),
        ("Timing", "", ["risk.open_delay_minutes", "intelligence.trade_event_max_age_hours", "risk.max_new_per_cycle"]),
        ("Avoiding doubling up", "Ideas that would just add to a position you already hold in disguise.", ["risk.corr_haircut_from", "risk.corr_block"])]),
    _sec("checks-before", "Trading", "Safety checks before buying", "Last-minute checks on a queued buy: news that arrived since, opening gaps, and market-wide shocks.", [
        ("News that arrives after an idea was queued", "", ["risk.veto_adverse_news", "risk.veto_news_direction", "risk.veto_news_magnitude"]),
        ("Opening gaps", "", ["risk.max_gap_down_pct", "risk.max_gap_up_pct"]),
        ("Market-wide shocks", "", ["risk.shock_min_sources", "risk.shock_direction", "risk.shock_market_drop_pct", "risk.shock_hold_hours", "risk.shock_applies_to"])]),
    _sec("exits", "Trading", "Stops, targets & exits", "How a position is protected, and when profit is taken.", [
        ("Style and holding time", "", ["strategy.swing_enabled", "strategy.max_hold_days", "strategy.flatten_at_close", "strategy.flatten_minutes_before_close", "risk.rotation_min_hold_hours"]),
        ("Profit targets", "", ["strategy.target_min_pct", "strategy.target_max_pct", "strategy.partial_exit_pct", "strategy.runner_cap_pct"]),
        ("Stop losses", "", ["strategy.stop_vol_mult", "strategy.stop_min_pct", "strategy.stop_max_pct"]),
        ("Protecting profit as a trade works", "", ["strategy.breakeven_trigger_pct", "strategy.breakeven_lock_pct", "strategy.trail_pct"]),
        ("Earnings reports", "", ["strategy.avoid_earnings", "strategy.earnings_exit_minutes_before_close"]),
        ("Exits on new evidence", "", ["strategy.escalation_grace_minutes", "strategy.escalation_grace_loss_pct"])]),
    _sec("shorts", "Instruments", "Short selling", "Profiting from falls. The strict rules about which stocks may be shorted are here.", [
        ("Switch", "", ["instruments.shorts_enabled"]),
        ("How much and how big", "", ["instruments.short_max_gross_pct", "instruments.short_size_mult"]),
        ("Which stocks may be shorted", "", ["instruments.short_min_price", "instruments.short_min_market_cap", "instruments.short_min_adv_usd", "instruments.short_squeeze_5d_pct"]),
        ("Costs", "", ["instruments.short_borrow_fee_pct", "instruments.short_collateral_pct"])]),
    _sec("options", "Instruments", "Options", "Calls and puts, in paper trading only. Prices come from free, delayed feeds where one answers.", [
        ("Switch and budget", "", ["instruments.options_enabled", "instruments.options_max_premium_pct", "instruments.options_per_trade_pct"]),
        ("When an option is used instead of the stock", "", ["instruments.options_min_ev", "instruments.options_max_horizon_days"]),
        ("Put trial: smaller puts on weaker ideas", "More puts, at half size, so the evidence arrives sooner. It pauses itself if it loses.", ["instruments.options_reversal_enabled", "instruments.options_reversal_min_move", "instruments.options_reversal_haircut", "instruments.options_reversal_per_trade_pct", "instruments.options_reversal_cooldown_hours", "instruments.options_rescue_enabled", "instruments.options_rescue_per_trade_pct", "instruments.options_min_option_ev", "instruments.options_trial_enabled", "instruments.options_trial_min_ev", "instruments.options_trial_per_trade_pct", "instruments.options_trial_puts_only", "instruments.options_trial_breaker_trades", "instruments.options_trial_breaker_pct"]),
        ("Choosing a contract", "", ["instruments.options_min_dte", "instruments.options_exit_dte", "instruments.options_max_spread_pct", "instruments.options_min_open_interest", "instruments.options_quote_max_age_minutes", "instruments.options_max_iv_rank"]),
        ("Taking profit and cutting losses", "", ["instruments.options_target_pct", "instruments.options_stop_loss_pct"]),
        ("Where the prices come from", "The bot takes the median of whichever free feeds answer.", ["instruments.options_use_real_quotes", "instruments.options_allow_model_fallback", "instruments.options_feed_cboe", "instruments.options_feed_alpaca", "instruments.options_feed_nasdaq", "instruments.options_feed_yahoo"])]),
    _sec("related", "Instruments", "Related companies", "Spreading a big event at one company to its suppliers, customers, competitors and peers.", [
        ("", "", ["graph.enabled", "graph.trade_on_correlation", "graph.max_derived_per_event", "graph.min_event_magnitude", "graph.derived_discount"])]),
    _sec("engine", "Intelligence", "Reasoning engine", "The bot's own rules-and-data engine: what it may do, and how it argues against a trade before it is made.", [
        ("What it may do", "", ["intelligence.influence", "intelligence.engine_max_per_cycle"]),
        ("Arguing against a trade (the red team)", "", ["intelligence.redteam_enabled", "intelligence.redteam_min_conviction", "intelligence.redteam_required"]),
        ("Cause-and-effect chains", "", ["intelligence.max_trade_order", "intelligence.chain_decay"]),
        ("Ideas with no news trigger", "", ["intelligence.allow_thesis_entries", "intelligence.thesis_entries_per_day", "intelligence.thesis_entry_min_probability"]),
        ("Learning from results", "", ["intelligence.learning_mode", "intelligence.learning_min_samples"]),
        ("Missed-trade analysis", "Replays the ideas the bot did NOT take, after the US close.", ["whatif.enabled", "whatif.lookback_days", "whatif.max_symbols_per_cycle", "whatif.time_budget_seconds"])]),
    _sec("news", "Intelligence", "Reading the news", "How news is gathered, how fresh it must be, and how company financials are read.", [
        ("Reading full articles", "", ["reader.mode", "reader.max_per_cycle", "reader.time_budget_seconds", "reader.timeout_seconds"]),
        ("What counts as fresh", "", ["intelligence.stale_event_hours"]),
        ("How each story is judged", "", ["intelligence.news_intel", "intelligence.quality_weight", "intelligence.reputation_apply", "intelligence.reputation_min_n", "intelligence.reputation_prior_n"]),
        ("Extra news sources", "", ["intelligence.gdelt_enabled", "intelligence.finnhub_company_news_max"]),
        ("Company financials from the SEC", "SEC asks for a contact address in every request.", ["intelligence.max_knowledge_refresh_per_cycle", "runtime.contact_email"])]),
    _sec("kb", "Intelligence", "Company knowledge", "What the bot knows about each company and how it uses it: knowledge-base refresh, event-aware expiry, relationship effects, materiality, event state and market regime. Every mode starts on observe.", [
        ("Switch", "", ["kb.enabled"]),
        ("What each part may do", "observe records and measures only; active lets that part affect trades.", ["kb.relationship_mode", "kb.expiry_mode", "kb.materiality_mode", "kb.state_mode", "kb.regime_mode"]),
        ("Relationship reach", "", ["kb.max_hops", "kb.propagate_events_per_cycle"]),
        ("Refreshing from the SEC", "A full refresh of the whole universe is run from the command line, outside the trading cycle.", ["kb.refresh_companies_per_cycle", "kb.sec_requests_per_second", "kb.max_network_seconds", "kb.max_db_seconds", "kb.retry_delay_hours", "kb.failure_backoff_max_hours"]),
        ("How fresh is fresh", "", ["kb.max_data_age_days", "kb.financial_max_age_days"]),
        ("Filing events and financial facts", "What the weekly job stores and how much it fetches.", ["kb.filing_window_days", "kb.filing_rows_per_company", "kb.weekly_facts_companies"])]),
    _sec("research", "Intelligence", "Research lab", "Tests the engine's own assumptions against years of stored prices before any rule is changed to use them. Observation only: it never changes a trade.", [
        ("Price history", "", ["history.enabled", "history.years", "history.symbols_per_cycle", "history.max_seconds"]),
        ("Studies", "", ["research.enabled", "research.rerun_days", "research.max_seconds"])]),
    _sec("shadow", "Intelligence", "Shadow book", "Every idea, traded or not, followed to a costed outcome so gates and thresholds are judged on hundreds of ideas instead of the few that traded. Observation only: it never changes a trade.", [
        ("Switch", "", ["shadow.enabled"]),
        ("How outcomes are counted", "Daily bars only; the cost is taken off every outcome.", ["shadow.cost_bps", "shadow.min_abs_direction"]),
        ("Work per cycle", "", ["shadow.measure_per_cycle", "shadow.max_seconds"])]),
    _sec("quotes", "Data & system", "Prices & quotes", "Where prices come from, how fresh they must be, and the simulated cost of trading.", [
        ("Simulated trading cost", "", ["portfolio.slippage_bps"]),
        ("Quote quality", "", ["quotes.min_providers", "quotes.max_deviation", "quotes.entry_max_age_seconds", "quotes.max_age_seconds", "quotes.allow_single_source", "quotes.single_source_max_move_pct", "quotes.probe_minutes"]),
        ("Price refresh", "", ["prices.refresh_open_minutes", "prices.refresh_closed_minutes", "prices.max_symbols", "prices.extended_hours"])]),
    _sec("schedule", "Data & system", "Schedule & Pi limits", "How often the bot works, and when it should hold back to protect the Pi.", [
        ("How often it runs", "Schedule changes are passed to the system timers automatically.", ["schedule.cycle_minutes", "schedule.guard_minutes", "schedule.update_hours"]),
        ("Background refresh", "", ["intelligence.regime_refresh_hours", "intelligence.company_refresh_hours", "intelligence.max_company_refresh_per_cycle", "intelligence.contradiction_hours"]),
        ("Protecting the Pi", "", ["runtime.cycle_budget_seconds", "runtime.min_available_memory_mb", "runtime.max_load_per_core", "runtime.fetch_workers"])]),
    _sec("security", "Data & system", "Security, alerts & storage", "Who can reach the dashboard, where alerts go, and how long history is kept.", [
        ("Access", "", ["runtime.require_password", "runtime.allowed_hosts", "runtime.bind_port"]),
        ("Phone alerts", "", ["notifications.ntfy_topic"]),
        ("Database", "", ["maintenance.weekly_cleanup", "maintenance.news_days", "maintenance.log_days"])]),
    _sec("feeds", "Connections", "News & data feeds", "Turn a feed off, or change how often it is polled. Failing feeds already back off automatically.", [], kind="feeds"),
    _sec("keys", "Connections", "API keys", "Keys are stored on the Pi in an owner-only file and are never shown again.", [], kind="keys"),
    _sec("services", "Operations", "Services & timers", "What is running, and buttons to run or restart things.", [], kind="services"),
    _sec("checks", "Operations", "Checks & tests", "Health checks that contact your feeds and providers.", [], kind="checks"),
    _sec("history", "Operations", "Change history & reset", "What changed, when and by whom; and the way back to the defaults.", [], kind="history"),
    _sec("llm", "Retired", "Language-model analyst", "RETIRED. The bot now reads and argues with its own reasoning engine and never calls a language model, so these settings have no effect on trading. They are kept so nothing is lost.", [
        ("Old limits", "", ["intelligence.llm_enabled", "intelligence.llm_daily_budget_usd", "intelligence.llm_monthly_budget_usd", "intelligence.llm_max_per_cycle", "intelligence.llm_daily_max_calls.triage", "intelligence.llm_daily_max_calls.analyst"])]),
]
BY_SECTION = {s["id"]: s for s in SECTION_META}
SECTION_OF = {p: s["id"] for s in SECTION_META for sub in s["subs"] for p in sub[2]}
SUB_OF = {p: sub[0] for s in SECTION_META for sub in s["subs"] for p in sub[2]}
VIEWS = ["overview", "all", "search"] + [s["id"] for s in SECTION_META]
ESSENTIAL = {"risk.risk_per_trade_pct", "risk.max_position_pct", "risk.max_gross_pct", "risk.min_cash_pct", "risk.drawdown_pause_pct", "risk.daily_loss_pause_pct", "strategy.swing_enabled", "strategy.stop_min_pct", "strategy.max_hold_days",
             "instruments.shorts_enabled", "instruments.options_enabled", "graph.enabled", "intelligence.influence", "intelligence.redteam_enabled", "schedule.cycle_minutes", "schedule.guard_minutes", "runtime.require_password",
             "notifications.ntfy_topic", "runtime.contact_email"}
INFLUENCE_TEXT = {"off": "off: the reasoning engine does not run", "observe": "observe: it reads and records, and changes nothing", "guard": "guard: it may veto a factually broken idea or shrink a trade, never create one",
                  "active": "active: its analysis and every objection fully influence trading"}


def layout_problems() -> List[str]:
    """Every setting must appear on the page exactly once, and the layout must name no setting that does not exist."""
    out, seen = [], {}
    for s in SECTION_META:
        for sub in s["subs"]:
            for p in sub[2]:
                if p not in S.BY_PATH:
                    out.append("layout names a setting that does not exist: %s" % p)
                if p in seen:
                    out.append("%s appears twice (%s and %s)" % (p, seen[p], s["id"]))
                seen[p] = s["id"]
    out += ["setting not placed anywhere: %s" % f.path for f in S.FIELDS if f.path not in seen]
    return out


def valid_view(v: Optional[str]) -> str:
    return v if v in VIEWS else ""


def view_suffix(section: Optional[str], find: Optional[str] = None) -> str:
    """The query string that returns to the same section after a save (the section is checked against the known ids, the search text is quoted)."""
    import urllib.parse
    sec = valid_view(section)
    if sec == "search" and (find or "").strip():
        return "&s=search&find=" + urllib.parse.quote((find or "").strip()[:60])
    return ("&s=" + sec) if sec and sec not in ("overview", "all") else ""


NEW_CSS = """
.setwrap{margin-top:6px}.setwrap:after{content:"";display:block;clear:both}
.snav{float:left;width:236px;padding-right:14px;font-size:13.5px}.sbody{margin-left:250px;min-width:0}
.snav form{margin:0 0 10px}.snav input[type=text]{width:100%;max-width:none}
.snav a{display:block;padding:4px 8px;border-radius:6px;color:var(--fg);text-decoration:none;margin:1px 0}
.snav a:hover{background:var(--card)}.snav a.on{background:var(--acc);color:#fff;font-weight:600}
.snav .sg{margin:12px 8px 3px;color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.snav .nb{float:right;background:var(--warn);color:#fff;border-radius:9px;padding:0 6px;font-size:11px;font-weight:600}.snav a.on .nb{background:#fff;color:var(--acc)}
.stitle{margin:2px 0 2px;font-size:20px}.sblurb{color:var(--mut);margin:0 0 12px;max-width:760px}
.sub{background:var(--card);border:1px solid var(--line);border-radius:9px;padding:6px 14px 4px;margin:0 0 14px}
.sub h3{margin:8px 0 2px;font-size:14px}.sub .subnote{color:var(--mut);font-size:12.5px;margin:0 0 4px}
.srow{display:table;width:100%;border-top:1px solid var(--line);padding:9px 0}.sub h3+.srow,.sub .subnote+.srow{border-top:0}
.sl,.sv{display:table-cell;vertical-align:top}.sl{width:62%;padding-right:16px}.sv{width:38%}
.sl label{font-weight:600}.sl .help{margin-top:2px;max-width:640px}.smeta{color:var(--mut);font-size:12px;margin-top:3px}
.star{color:var(--warn);margin-left:4px;cursor:help}.srow.err{background:rgba(179,38,30,.10);border-left:3px solid var(--bad);padding-left:8px}
.sv .unit{color:var(--mut);margin-left:4px}.sv label.tg{display:inline-block;padding:4px 0}
.tiles{margin:6px 0 4px}.tile{display:inline-block;vertical-align:top;width:172px;margin:0 8px 8px 0;background:var(--card);border:1px solid var(--line);border-radius:9px;padding:6px 10px;color:var(--fg);text-decoration:none}
.tile:hover{border-color:var(--acc)}.tile b{display:block;font-size:15px;margin:0}.tile span{color:var(--mut);font-size:12px}.tile .dot{display:inline-block;width:9px;height:9px;border-radius:5px;margin-right:6px;background:var(--mut)}
.tile .dot.on{background:var(--ok)}.tile .dot.off{background:var(--mut)}.tile .dot.warn{background:var(--warn)}
.secs a{display:block;padding:7px 10px;border-bottom:1px solid var(--line);color:var(--fg);text-decoration:none}.secs a:hover{background:var(--card)}.secs b{color:var(--acc)}.secs span{color:var(--mut);font-size:12.5px}
.banner{background:var(--card);border:1px solid var(--warn);border-radius:8px;padding:8px 12px;margin:0 0 12px}
.savebar{margin-top:6px}
@media(min-width:861px){.snav{position:sticky;top:8px;max-height:calc(100vh - 16px);overflow-y:auto}}
@media(max-width:860px){.snav{float:none;width:auto;padding:0 0 10px}.sbody{margin-left:0}.srow,.sl,.sv{display:block;width:auto}.sl{padding-right:0}.sv{padding-top:5px}.tile{width:100%;margin-right:0}.snav a{display:inline-block;margin:2px 4px 2px 0;padding:5px 10px;border:1px solid var(--line)}.snav .sg{margin:10px 0 2px}.snav .nb{float:none;margin-left:6px}}
"""
NEW_CSS += ".focus{font-size:12px;font-weight:400;margin-left:10px}.srow:target{outline:2px solid var(--acc);outline-offset:2px}details.chgs{margin:4px 0 10px;background:var(--card);border:1px solid var(--line);border-radius:9px;padding:6px 12px}\ndetails.chgs summary{cursor:pointer}\n"


def _iid(path: str) -> str:
    return "i-" + path.replace(".", "-")


def _input(f: S.Field, cfg: dict, base: dict, raw: Dict[str, str]) -> str:
    cur = S.display_value(f, cfg)
    val = raw.get(f.name, cur)
    iid = _iid(f.path)
    if f.kind == "bool":
        chk = " checked" if val in ("1", "true", "on") else ""
        return '<input type=hidden name="%s" value=0><label class=tg><input type=checkbox id="%s" name="%s" value=1%s> On</label>' % (E(f.name), iid, E(f.name), chk)
    if f.kind == "choice":
        opts = "".join('<option value="%s"%s>%s</option>' % (E(c), " selected" if c == val else "", E(c)) for c in (f.choices or []))
        return '<select id="%s" name="%s">%s</select>' % (iid, E(f.name), opts)
    unit = ' <span class=unit>%</span>' if f.kind == "pct" else ""
    cls = "wide" if f.kind in ("email", "hostlist", "topic") else "num"
    mode = "numeric" if f.kind == "int" else "decimal"
    return '<input type=text class=%s id="%s" name="%s" value="%s" inputmode=%s>%s' % (cls, iid, E(f.name), E(val), mode, unit)


def _default_text(f: S.Field, base: dict) -> str:
    try:
        d = S.display_value(f, base)
    except KeyError:
        return ""
    if f.kind == "bool":
        return "on" if d == "1" else "off"
    return (d or "(empty)") + (" %" if f.kind == "pct" and d else "")


def _is_changed(f: S.Field, cfg: dict, base: dict) -> bool:
    try:
        return S.display_value(f, cfg) != S.display_value(f, base)
    except KeyError:
        return False


def _range_text(f: S.Field) -> str:
    if f.lo is not None and f.kind in ("int", "float", "pct"):
        sc = 100 if f.kind == "pct" else 1
        return "range %g\u2013%g%s" % (f.lo * sc, f.hi * sc, "%" if f.kind == "pct" else "")
    return ""


def _row(f: S.Field, cfg: dict, base: dict, raw: Dict[str, str], meta: Optional[Dict[str, dict]] = None, bad: bool = False) -> str:
    chd = _is_changed(f, cfg, base)
    badge = ' <span class=chg> changed (default %s)</span>' % E(_default_text(f, base)) if chd else ""
    bits = ["default %s" % E(_default_text(f, base))] if _default_text(f, base) != "" or f.kind in ("bool",) else []
    if _range_text(f):
        bits.append(_range_text(f))
    if f.restart == "dashboard":
        bits.append("<b>restart needed</b>")
    star = ' <span class=star title="One of the most-used settings">&#9733;</span>' if f.path in ESSENTIAL else ""
    return ('<div class="srow%s%s" id="s-%s"><div class=sl><label for="%s">%s</label>%s<div class=help>%s</div><div class=smeta>%s%s</div></div><div class=sv>%s</div></div>'
            % (" chd" if chd else "", " err" if bad else "", E(f.path.replace(".", "-")), _iid(f.path), E(f.label), star, E(f.help), " &middot; ".join(bits), _when((meta or {}).get(f.path)), _input(f, cfg, base, raw) + badge))


def _bad_labels(errors: Optional[List[str]]) -> set:
    return {e.split(":")[0].strip() for e in (errors or []) if ":" in e}


def _subs_html(sec: dict, cfg: dict, base: dict, raw: Dict[str, str], meta, bad: set, only: Optional[set] = None) -> str:
    out = []
    for title, note, paths in sec["subs"]:
        rows = [_row(S.BY_PATH[p], cfg, base, raw, meta, S.BY_PATH[p].label in bad) for p in paths if only is None or p in only]
        if not rows:
            continue
        out.append("<div class=sub>%s%s%s</div>" % ("<h3>%s</h3>" % E(title) if title else "", "<p class=subnote>%s</p>" % E(note) if note else "", "".join(rows)))
    return "".join(out)


def _changed_in(sec: dict, cfg: dict, base: dict) -> int:
    return sum(1 for sub in sec["subs"] for p in sub[2] if _is_changed(S.BY_PATH[p], cfg, base))


def _form_open(csrf: str, view: str, find: str = "") -> str:
    return '<form method=post action=/settings/save>%s<input type=hidden name=_section value="%s"><input type=hidden name=_find value="%s">' % (csrf_input(csrf), E(view), E(find))


SAVE_NOTE = "Saves the settings on this page only. Most changes apply on the next cycle; schedule changes are applied to the system timers automatically."


def _savebar(label: str = "Save these settings") -> str:
    return '<div class=savebar><button class=primary>%s</button> <span class=help>%s</span></div></form>' % (E(label), E(SAVE_NOTE))


def _feeds_table(cfg: dict, raw: Dict[str, str], sources: List[dict], health) -> str:
    so = cfg.get("source_overrides", {})
    out = ["<div class=sub><div class=wrap><table><tr><th>Feed</th><th>Type</th><th>Status</th><th>Enabled</th><th>Poll every (seconds)</th></tr>"]
    for s in sources:
        o = so.get(s["id"], {})
        en = o.get("enabled", True)
        iv = raw.get("src.%s.interval" % s["id"], o.get("interval", s.get("interval", 600)))
        out.append("<tr><td>%s</td><td>%s</td><td>%s</td><td><input type=hidden name='src.%s.enabled' value=0><input type=checkbox name='src.%s.enabled' value=1%s></td><td><input type=text class=num name='src.%s.interval' value='%s'></td></tr>"
                   % (E(s["name"]), E(s["type"]), _feed_status((health or {}).get(s["id"])), E(s["id"]), E(s["id"]), " checked" if en else "", E(s["id"]), E(iv)))
    out.append("</table></div></div>")
    return "".join(out)


def _tiers_block(cfg: dict, raw: Dict[str, str]) -> str:
    return "<div class=sub><h3>Model tiers (retired)</h3><div class=wrap><table>%s</table></div></div>" % _tiers(cfg, raw)


def _keys_html(csrf: str, ctx: dict, secret_status: Dict[str, str]) -> str:
    out = ["<form method=post action=/settings/secrets>%s<div class=card2 style='margin-top:0'><p class=help>Leave a box empty to keep the current key. Tick <b>clear</b> to remove one.</p><div class=wrap><table><tr><th>Key</th><th>Status</th><th>New value</th><th>Clear</th></tr>" % csrf_input(csrf)]
    for name, label, help_ in S.SECRETS:
        updated = _when((ctx.get("meta") or {}).get("secret." + name)).replace("changed ", "updated ")
        out.append("<tr><td>%s<br><span class=help>%s</span></td><td>%s%s</td><td><input type=password class=wide name='secret.%s' autocomplete=off placeholder='paste new key'></td><td><input type=checkbox name='clear.%s' value=1></td></tr>"
                   % (E(label), E(help_), E(secret_status.get(name, "not set")), updated, E(name), E(name)))
    out.append("</table></div><p><button class=primary>Save keys</button></p></div></form>")
    return "".join(out)


def _services_html(csrf: str, ctx: dict, units: Dict[str, str]) -> str:
    out = []
    if ctx.get("control_path") in ("inactive", "failed", "dead"):
        out.append('<div class="flash err">The control helper (paper-agent-control.path) is %s, so restart requests and schedule changes will NOT be carried out. Fix: sudo systemctl enable --now paper-agent-control.path</div>' % E(ctx["control_path"]))
    ss = ctx.get("schedule_status")
    if ss:
        names = {"cycle_minutes": "Intelligence cycle (minutes)", "guard_minutes": "Exit guard (minutes)", "update_hours": "Auto-update check (hours)"}
        rows = "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (E(names[k]), v["configured"], v["applied"], '<span class=ok>in effect</span>' if v["in_sync"] else '<span class=warn>NOT applied yet</span>') for k, v in ss.items())
        out.append("<div class=card2 style='margin-top:0'><b>Timer schedule: your settings vs. what systemd is actually running</b><table><tr><th>Setting</th><th>Configured</th><th>Running</th><th></th></tr>%s</table>" % rows)
        if not all(v["in_sync"] for v in ss.values()):
            out.append("<form method=post action=/service>%s<input type=hidden name=action value=reapply_schedule><button class=primary>Apply schedule now</button> <span class=help>The bot also re-checks this every cycle and fixes drift by itself.</span></form>" % csrf_input(csrf))
        out.append("</div>")
    out.append("<div class=card2 style='margin-top:0'><table><tr><th>Unit</th><th>State</th><th></th></tr>")
    for u, st in units.items():
        out.append("<tr><td>%s</td><td class=%s>%s</td><td><form method=post action=/service style='display:inline'>%s<input type=hidden name=action value=restart><input type=hidden name=unit value='%s'><button class=sec>Restart</button></form></td></tr>"
                   % (E(u), "ok" if st == "active" else "warn", E(st), csrf_input(csrf), E(u)))
    out.append("</table><p>")
    for label, act, unit in (("Run a cycle now", "start", "paper-agent-cycle.service"), ("Check for updates now", "start", "paper-agent-update.service"), ("Back up now", "start", "paper-agent-backup.service"),
                             ("Refresh company list now", "start", "paper-agent-universe.service"), ("Restart guard + cycle + dashboard", "restart_all", "")):
        out.append("<form method=post action=/service style='display:inline'>%s<input type=hidden name=action value=%s><input type=hidden name=unit value='%s'><button class=sec>%s</button></form> " % (csrf_input(csrf), act, E(unit), label))
    res = ctx.get("control")
    out.append(("</p><p class=help>Last request: <b>%s</b> &mdash; %s &mdash; %s</p>" % (E(res.get("action")), "OK" if res.get("ok") else "FAILED", E(res.get("output")))) if res else "</p>")
    out.append("<p class=help>Restarts go through a small root helper that only accepts a fixed list of paper-agent actions. Jobs (update, backup, company list) are started on demand and are refused when the Pi is short of memory.</p></div>")
    return "".join(out)


def _checks_html(csrf: str, ctx: dict) -> str:
    out = ["<div class=card2 style='margin-top:0'><p>"]
    for label, path in (("Pre-open checklist", "/test/preflight"), ("Run full health check", "/test/doctor")):
        out.append("<form method=post action='%s' style='display:inline'>%s<button class=sec>%s</button></form> " % (path, csrf_input(csrf), label))
    out.append("</p><p class=help>The health check contacts every feed and quote provider; it can take up to a minute.</p>")
    out.append("<p class=help>Retired language-model tests (they make one real, tiny API call, and only matter if you re-enable the old analyst):</p><p>")
    for label, path in (("Test LLM (cheap tier)", "/test/llm?tier=triage"), ("Test LLM (deep tier)", "/test/llm?tier=analyst")):
        out.append("<form method=post action='%s' style='display:inline'>%s<button class=sec>%s</button></form> " % (path, csrf_input(csrf), label))
    out.append("</p>")
    if ctx.get("test_output"):
        out.append("<pre>%s</pre>" % E(ctx["test_output"]))
    out.append("</div>")
    return "".join(out)


def _history_html(csrf: str, ctx: dict) -> str:
    return (changes_panel(ctx.get("meta") or {}) or "<p class=help>No setting has been changed from the dashboard yet.</p>") + \
        ("<h2>Reset</h2><div class=card2 style='margin-top:0'><form method=post action=/settings/reset>%s<label><input type=checkbox name=confirm value=1> I want to discard ALL dashboard-made setting changes (API keys are kept)</label> <button class=danger>Reset to defaults</button></form></div>" % csrf_input(csrf))


def _tile(label: str, value: str, sub: str, href: str, state: str = "") -> str:
    return '<a class=tile href="/?tab=settings&amp;s=%s"><span><i class="dot %s"></i>%s</span><b>%s</b><span>%s</span></a>' % (E(href), E(state), E(label), E(value), E(sub))


def _search(find: str, cfg: dict, base: dict, raw: Dict[str, str], meta, csrf: str, bad: set) -> str:
    terms = [t for t in find.lower().split() if t]
    out = ["<h2 class=stitle>Search: %s</h2>" % E(find)]
    if len(find.strip()) < 2:
        return out[0] + "<p class=sblurb>Type at least two letters.</p>"
    named, mentioned = [], []                                  # in the layout's own order, so each section appears once and rows read in a sensible order
    for sec in SECTION_META:
        for title, _note, paths in sec["subs"]:
            for p in paths:
                f = S.BY_PATH[p]
                name_hay = " ".join([f.label, f.path, sec["title"], title]).lower()
                if terms and all(t in name_hay for t in terms):
                    named.append(f)
                elif terms and all(t in name_hay + " " + (f.help or "").lower() for t in terms):
                    mentioned.append(f)
    total = len(named) + len(mentioned)
    if not total:
        return out[0] + "<p class=sblurb>No setting matches. Try fewer or different words, for example &ldquo;stop&rdquo;, &ldquo;cash&rdquo;, &ldquo;short&rdquo; or &ldquo;minutes&rdquo;.</p>"
    room = 60
    out.append("<p class=sblurb>%d setting(s) match%s. Each shows the section it lives in.</p>" % (total, "; showing the first 60, so narrow the search to see the rest" if total > room else ""))
    out.append(_form_open(csrf, "search", find))
    for heading, items in (("Named like that", named), ("Mentioned in the description", mentioned)):
        items = items[:room]
        room -= len(items)
        if not items:
            continue
        if named and mentioned:
            out.append("<h2>%s</h2>" % heading)
        cur, rows = None, []
        for f in items:
            sid = SECTION_OF.get(f.path, "")
            if sid != cur:
                if rows:
                    out.append("<div class=sub>%s</div>" % "".join(rows))
                cur, rows = sid, ["<h3><a href='/?tab=settings&amp;s=%s'>%s</a></h3>" % (E(sid), E(BY_SECTION[sid]["title"] if sid in BY_SECTION else ""))]
            rows.append(_row(f, cfg, base, raw, meta, f.label in bad))
        out.append("<div class=sub>%s</div>" % "".join(rows))
    out.append(_savebar("Save these settings"))
    return "".join(out)


def _settings_section(sec: dict, cfg: dict, base: dict, ctx: dict, raw: Dict[str, str], csrf: str, sources: List[dict], units: Dict[str, str], secret_status: Dict[str, str], health) -> str:
    bad = _bad_labels(ctx.get("errors"))
    head = "<h2 class=stitle>%s</h2><p class=%s>%s</p>" % (E(sec["title"]), "banner" if sec["id"] == "llm" else "sblurb", E(sec["blurb"]))
    k = sec["kind"]
    if k == "settings":
        body = _form_open(csrf, sec["id"]) + _subs_html(sec, cfg, base, raw, ctx.get("meta"), bad) + (_tiers_block(cfg, raw) if sec["id"] == "llm" else "") + _savebar()
    elif k == "feeds":
        dead = dead_feed_ids(sources, cfg, health or {})
        body = _form_open(csrf, "feeds") + _feeds_table(cfg, raw, sources, health) + _savebar("Save feed settings")
        if dead:
            body += "<div class=card2><form method=post action=/settings/disable-dead>%s<b>%d feed(s) are permanently gone</b> (HTTP 404/410 three or more times): %s. <button class=sec>Disable these %d feeds</button></form></div>" % (csrf_input(csrf), len(dead), E(", ".join(dead)), len(dead))
    elif k == "keys":
        body = _keys_html(csrf, ctx, secret_status)
    elif k == "services":
        body = _services_html(csrf, ctx, units)
    elif k == "checks":
        body = _checks_html(csrf, ctx)
    else:
        body = _history_html(csrf, ctx)
    return head + body


def _sec_head(s: dict, focus: bool = True) -> str:
    return "<h2 class=stitle id='sec-%s'>%s%s</h2><p class=%s>%s</p>" % (E(s["id"]), E(s["title"]), " <a class=focus href='/?tab=settings&amp;s=%s'>only this section</a>" % E(s["id"]) if focus else "",
                                                                     "banner" if s["id"] == "llm" else "sblurb", E(s["blurb"]))


def _nav(active: str, cfg: dict, base: dict, find: str, n_pending: int) -> str:
    onpage = active == "all"
    href = lambda sid: ("#sec-%s" % sid) if onpage else ("/?tab=settings&amp;s=%s" % sid)  # noqa: E731
    out = ['<nav class=snav aria-label="Settings sections"><form method=get action="/"><input type=hidden name=tab value=settings><input type=text name=find value="%s" placeholder="Search all settings" aria-label="Search settings"> <button class=sec>Search</button></form>' % E(find)]
    out.append('<a class="%s" href="/?tab=settings">All settings on one page%s</a>' % ("on" if onpage else "", ' <span class=nb title="release suggestions waiting">%d</span>' % n_pending if n_pending else ""))
    for g in NAV_GROUPS:
        out.append("<div class=sg>%s</div>" % E(g))
        for s in [x for x in SECTION_META if x["group"] == g]:
            n = _changed_in(s, cfg, base)
            out.append('<a class="%s" href="%s"%s>%s%s</a>' % ("on" if active == s["id"] else "", href(s["id"]), " aria-current=page" if active == s["id"] else "", E(s["title"]),
                                                              ' <span class=nb title="%d changed from the default">%d</span>' % (n, n) if n else ""))
    return "".join(out) + "</nav>"


def _overview(cfg: dict, base: dict, ctx: dict, sources: List[dict], secret_status: Dict[str, str]) -> str:
    """The strip at the top of the single settings page: the key switches in plain words, and everything changed from the defaults."""
    g = lambda p: S.get_path(cfg, p)  # noqa: E731
    onoff = lambda b: ("On", "on") if b else ("Off", "off")  # noqa: E731
    inf = g("intelligence.influence")
    so = cfg.get("source_overrides", {})
    n_on = sum(1 for s in sources if so.get(s["id"], {}).get("enabled", True))
    n_keys = sum(1 for v in secret_status.values() if v and not str(v).lower().startswith("not set"))
    tiles = [_tile("Reasoning engine", inf, INFLUENCE_TEXT.get(inf, "").split(": ", 1)[-1], "engine", "on" if inf in ("guard", "active") else "warn" if inf == "observe" else "off"),
             _tile("Trading style", "Swing" if g("strategy.swing_enabled") and not g("strategy.flatten_at_close") else "Day-trade" if g("strategy.flatten_at_close") else "Standard", "holds up to %s days" % g("strategy.max_hold_days"), "exits", "on"),
             _tile("Risk per trade", "%.2f%%" % (g("risk.risk_per_trade_pct") * 100), "largest position %.1f%%" % (g("risk.max_position_pct") * 100), "size", "on"),
             _tile("Cash kept in reserve", "%.0f%%" % (g("risk.min_cash_pct") * 100), "trading pauses at %.0f%% drawdown" % (g("risk.drawdown_pause_pct") * 100), "size", "on"),
             _tile("Short selling", onoff(g("instruments.shorts_enabled"))[0], "up to %.0f%% of the book" % (g("instruments.short_max_gross_pct") * 100), "shorts", onoff(g("instruments.shorts_enabled"))[1]),
             _tile("Options", onoff(g("instruments.options_enabled"))[0], "paper trading, %.1f%% premium cap" % (g("instruments.options_max_premium_pct") * 100), "options", onoff(g("instruments.options_enabled"))[1]),
             _tile("Related companies", onoff(g("graph.enabled"))[0], "events spread to suppliers, customers, rivals", "related", onoff(g("graph.enabled"))[1]),
             _tile("Runs every", "%s min" % g("schedule.cycle_minutes"), "exit guard every %s min" % g("schedule.guard_minutes"), "schedule", "on"),
             _tile("Settings password", "Required" if g("runtime.require_password") else "Not required", "anyone on your network can open Settings" if not g("runtime.require_password") else "Settings need a login", "security", "on" if g("runtime.require_password") else "warn"),
             _tile("News & data feeds", "%d of %d on" % (n_on, len(sources)), "%d API key(s) saved" % n_keys, "feeds", "on")]
    chg = [f for f in S.FIELDS if _is_changed(f, cfg, base)]
    meta = ctx.get("meta") or {}
    rows = "".join("<tr><td><a href='#s-%s'>%s</a><br><span class=help>%s</span></td><td style='white-space:nowrap'><b>%s</b></td><td style='white-space:nowrap'>%s</td><td>%s</td></tr>" % (E(f.path.replace(".", "-")), E(f.label), E(BY_SECTION[SECTION_OF[f.path]]["title"] if f.path in SECTION_OF else ""),
                   E(_fmt(f.path, S.get_path(cfg, f.path))), E(_default_text(f, base)), _when(meta.get(f.path)).replace(" &middot; ", "")) for f in chg[:80])
    changed = ("<details class=chgs%s><summary>Changed from the defaults (%d): click to show</summary><div class=wrap><table><tr><th>Setting</th><th>Now</th><th>Default</th><th>Changed</th></tr>%s</table></div>%s</details>"
               % (" open" if 0 < len(chg) <= 12 else "", len(chg), rows, "<p class=help>Showing the first 80.</p>" if len(chg) > 80 else "")) if chg else "<p class=help>Changed from the defaults: nothing. Every setting is at its default.</p>"
    return ("<h2 class=stitle id='top'>Settings</h2><p class=sblurb>Every setting is on this page. Use the menu to jump to a section, or search. A <span class=star>&#9733;</span> marks the settings most people adjust. "
            "The Save button at the bottom saves every setting on the page; the key, service and reset boxes at the very bottom save separately.</p><div class=tiles>%s</div>%s" % ("".join(tiles), changed))


def _all_settings(cfg: dict, base: dict, ctx: dict, raw: Dict[str, str], csrf: str, sources: List[dict], units: Dict[str, str], secret_status: Dict[str, str], health) -> str:
    bad = _bad_labels(ctx.get("errors"))
    out = [_overview(cfg, base, ctx, sources, secret_status), _form_open(csrf, "all")]
    for s in SECTION_META:
        if s["kind"] == "settings":
            out.append(_sec_head(s) + _subs_html(s, cfg, base, raw, ctx.get("meta"), bad) + (_tiers_block(cfg, raw) if s["id"] == "llm" else ""))
        elif s["kind"] == "feeds":
            out.append(_sec_head(s) + _feeds_table(cfg, raw, sources, health))
    out.append(_savebar("Save all settings"))
    dead = dead_feed_ids(sources, cfg, health or {})
    if dead:
        out.append("<div class=card2><form method=post action=/settings/disable-dead>%s<b>%d feed(s) are permanently gone</b> (HTTP 404/410 three or more times): %s. <button class=sec>Disable these %d feeds</button></form></div>" % (csrf_input(csrf), len(dead), E(", ".join(dead)), len(dead)))
    for s in SECTION_META:                                          # the boxes that are not part of the settings form (they each save on their own)
        if s["kind"] in ("keys", "services", "checks", "history"):
            out.append(_sec_head(s, focus=False) + {"keys": lambda: _keys_html(csrf, ctx, secret_status), "services": lambda: _services_html(csrf, ctx, units),
                                                    "checks": lambda: _checks_html(csrf, ctx), "history": lambda: _history_html(csrf, ctx)}[s["kind"]]())
    return "".join(out)


def infer_view(raw: Dict[str, str]) -> str:
    """Which page should re-show a posted form that carries no (valid) section marker: the one section its settings belong to, else the all-settings page."""
    secs = {SECTION_OF.get(k[2:]) for k in (raw or {}) if k.startswith("f.") and k[2:] in SECTION_OF}
    secs |= {"feeds"} if any(k.startswith("src.") for k in (raw or {})) else set()
    secs.discard(None)
    return secs.pop() if len(secs) == 1 else ("all" if secs else "")


def _qv(q, key: str) -> str:
    v = (q or {}).get(key, "")
    v = v[0] if isinstance(v, (list, tuple)) and v else v
    return str(v or "").strip()


def page_settings(cfg: dict, base: dict, ctx: dict, sources: List[dict], units: Dict[str, str], secret_status: Dict[str, str], health: Optional[Dict[str, dict]] = None) -> str:
    raw = ctx.get("raw") or {}
    csrf = ctx.get("csrf", "")
    q = ctx.get("query") or {}
    find = (ctx.get("find") or _qv(q, "find"))[:60]
    view = valid_view(ctx.get("section") or _qv(q, "s"))
    if find.strip() and view in ("", "search", "overview", "all"):
        view = "search"
    if not view and raw:
        view = infer_view(raw)
    if not view:
        view = "checks" if ctx.get("test_output") else "all"
    if view == "overview":
        view = "all"                                                         # the old overview address now shows the single page
    pending = ctx.get("pending") or []
    top = ["<style>%s</style>" % NEW_CSS, flash_html(ctx.get("flash"), ctx.get("errors"), ctx.get("warnings"))]
    if view == "all":
        top.append(pending_panel(pending, csrf))
    elif pending:
        top.append("<div class=banner>The latest release suggests %d setting change(s). <a href='/?tab=settings'>Review them on the main Settings page</a>.</div>" % len(pending))
    if view == "search":
        main = _search(find, cfg, base, raw, ctx.get("meta"), csrf, _bad_labels(ctx.get("errors")))
    elif view == "all":
        main = _all_settings(cfg, base, ctx, raw, csrf, sources, units, secret_status, health)
    else:
        main = _settings_section(BY_SECTION[view], cfg, base, ctx, raw, csrf, sources, units, secret_status, health)
    return "".join(top) + '<div class=setwrap>%s<div class=sbody>%s</div></div>' % (_nav(view, cfg, base, find if view == "search" else "", len(pending)), main)
