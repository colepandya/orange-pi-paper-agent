"""Dashboard-editable settings: schema, validation, layering, secrets.

* Editable values live in a small overrides file in the STATE dir (writable by the unprivileged dashboard) and are layered over
  defaults + /etc/paper-agent/config.json, so nothing here needs root and updates never clobber them.
* Only values that DIFFER from the base are stored, so changed defaults in future releases still flow through for untouched settings.
* Every value is type- and range-checked, then cross-checked (e.g. max position <= max issuer) before anything is written.
* API keys are stored separately (0600), shown masked, and never echoed back."""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from typing import Dict, List, Optional, Tuple

from . import config


class Field:
    def __init__(self, path, label, kind, group, lo=None, hi=None, help="", choices=None, restart=""):
        self.path, self.label, self.kind, self.group, self.lo, self.hi, self.help, self.choices, self.restart = path, label, kind, group, lo, hi, help, choices, restart
        self.name = "f." + path


F = Field
FIELDS: List[Field] = [
    # ---------------------------------------------------------------- risk
    F("risk.max_position_pct", "Max size of one new position", "pct", "Risk limits", 0.005, 0.5, "% of equity committed to a single new position."),
    F("risk.max_issuer_pct", "Max exposure to one company", "pct", "Risk limits", 0.01, 0.6, "Total % of equity in one company (all positions in it)."),
    F("risk.max_theme_pct", "Max exposure to one theme", "pct", "Risk limits", 0.05, 0.9, "Total % of equity tied to one investment thesis."),
    F("risk.max_gross_pct", "Max invested (gross) exposure", "pct", "Risk limits", 0.1, 1.0, "Total % of equity allowed in positions."),
    F("risk.min_cash_pct", "Minimum cash reserve", "pct", "Risk limits", 0.0, 0.9, "% of equity always kept in cash."),
    F("risk.risk_per_trade_pct", "Risk per trade (to the stop)", "pct", "Risk limits", 0.001, 0.05, "Equity lost if the stop is hit; caps position size."),
    F("risk.min_position_pct", "Smallest position worth opening", "pct", "Risk limits", 0.001, 0.1, "Orders smaller than this are skipped."),
    F("risk.max_stress_loss_pct", "Max worst-case stress loss", "pct", "Risk limits", 0.02, 0.6, "Worst of the six stress tests may not exceed this; sizes shrink to fit."),
    F("risk.ruin_loss_pct", "Ruin threshold (for survival probability)", "pct", "Risk limits", 0.05, 0.8, "A stress loss above this counts against survival probability."),
    F("risk.drawdown_pause_pct", "Pause new trades at drawdown of", "pct", "Risk limits", 0.02, 0.5, "No new entries once drawdown from peak reaches this."),
    F("risk.daily_loss_pause_pct", "Pause new trades after one-day loss of", "pct", "Risk limits", 0.005, 0.2, ""),
    F("risk.full_size_edge", "Edge needed for a FULL-size position (expected profit / typical move)", "float", "Risk limits", 0.1, 3.0, "Positions scale with the edge: at this value (0.5 = half a typical move) a position gets the maximum size; weaker ideas get proportionally less, never below 25% of the maximum."),
    F("risk.shock_min_sources", "Outlets needed to confirm a market-wide shock", "int", "Risk limits", 1, 5, "A crash / war / emergency-policy story must be reported by at least this many different sources before it freezes new BUYs."),
    F("risk.shock_market_drop_pct", "Market must also be down at least (fraction, 0 = headlines alone suffice)", "float", "Risk limits", 0.0, 0.2, "A confirmed crash / war / emergency story only freezes new BUYs if SPY is also down this much versus its previous close. 0.01 = 1%."),
    F("risk.allow_add_to_position", "Allow a second position in a stock already held", "bool", "Risk limits", help="Off: if you already hold a stock, further BUYs on it are skipped (they would stack the same bet). The best-EV idea is processed first."),
    F("risk.veto_adverse_news", "Re-check queued BUYs against news that arrived after they were decided", "bool", "Risk limits", help="A later bearish story about the same company cancels the BUY; a market-wide shock holds it."),
    F("risk.veto_news_direction", "Later news counts as adverse at direction <=", "float", "Risk limits", -1.0, -0.1, "-1 = very bearish, 0 = neutral."),
    F("risk.veto_news_magnitude", "...and importance >=", "float", "Risk limits", 0.1, 1.0, ""),
    F("risk.shock_direction", "Market-wide shock at direction <=", "float", "Risk limits", -1.0, -0.3, "War, crash, emergency policy..."),
    F("risk.shock_hold_hours", "Hold queued BUYs after a market shock for (hours, 0 = off)", "int", "Risk limits", 0, 48, "They are re-checked every cycle and released afterwards."),
    F("risk.max_gap_down_pct", "Cancel a BUY if the stock opens this far BELOW the previous close", "pct", "Risk limits", 0.005, 0.30, ""),
    F("risk.max_gap_up_pct", "Cancel a BUY if the stock opens this far ABOVE the previous close", "pct", "Risk limits", 0.01, 0.50, "The move has already happened; the edge is gone."),
    F("strategy.swing_enabled", "Swing mode (short holds, tight risk)", "bool", "Swing strategy", help="On: trades are sized, targeted and exited for ~1-3 sessions. Off: the older multi-week behaviour (stops 2-10%, holds up to 15 days)."),
    F("strategy.max_hold_days", "Maximum hold (trading days)", "int", "Swing strategy", 1, 15, "A position still open after this many sessions is sold (the time stop)."),
    F("strategy.target_min_pct", "Profit target: at least", "pct", "Swing strategy", 0.003, 0.05, "Half the position is sold when the price reaches the target; the rest runs with a protected stop."),
    F("strategy.target_max_pct", "Profit target: at most", "pct", "Swing strategy", 0.01, 0.15, ""),
    F("strategy.stop_vol_mult", "Stop distance = this x the stock's daily volatility", "float", "Swing strategy", 0.5, 3.0, "1.0 = one typical daily move."),
    F("strategy.stop_min_pct", "Stop loss: at least", "pct", "Swing strategy", 0.003, 0.05, "Floors the stop so ordinary noise does not stop you out."),
    F("strategy.stop_max_pct", "Stop loss: at most", "pct", "Swing strategy", 0.01, 0.10, ""),
    F("strategy.min_reward_risk", "Minimum reward : risk", "float", "Swing strategy", 0.5, 5.0, "A BUY whose target is smaller than this multiple of its stop is skipped."),
    F("strategy.min_daily_vol_pct", "Skip stocks that move less than this per day", "pct", "Swing strategy", 0.0, 0.05, "A stock that barely moves cannot deliver a 1-2% swing."),
    F("strategy.partial_exit_pct", "Share sold when the target is reached", "pct", "Swing strategy", 0.1, 1.0, "0.60 = bank 60% and let the rest run behind the protected stop. 1.00 = sell everything at the target."),
    F("strategy.runner_cap_pct", "Runner target cap", "pct", "Swing strategy", 0.02, 0.35, "Where the remaining half is sold if it keeps running."),
    F("strategy.breakeven_trigger_pct", "Lock in break-even once up", "pct", "Swing strategy", 0.002, 0.05, "After the price has been this far above entry, the stop moves up to break-even."),
    F("strategy.breakeven_lock_pct", "...and lock in this much profit", "pct", "Swing strategy", 0.0, 0.02, "Covers costs."),
    F("strategy.trail_pct", "Then trail the high by", "pct", "Swing strategy", 0.002, 0.05, "The stop follows the highest price reached, never moves down."),
    F("strategy.avoid_earnings", "Stay out of earnings reports", "bool", "Swing strategy", help="Do not open a trade whose holding window contains an earnings report, and sell before the close if one would land overnight. A 1-3% stop cannot protect against an earnings gap."),
    F("strategy.earnings_exit_minutes_before_close", "...sell this many minutes before the close", "int", "Swing strategy", 5, 120, ""),
    F("strategy.flatten_at_close", "Sell everything shortly before the close (day-trade mode)", "bool", "Swing strategy", help="Off: positions may be held overnight up to the maximum hold. On: nothing is held overnight."),
    F("strategy.flatten_minutes_before_close", "...this many minutes before the close", "int", "Swing strategy", 1, 60, ""),
    F("risk.min_ev_net", "Minimum expected value (net of costs)", "pct", "Risk limits", 0.0, 0.05, "Candidates below this expected return are not traded."),
    F("risk.min_confidence", "Minimum forecast confidence", "pct", "Risk limits", 0.0, 0.95, ""),
    F("risk.max_new_per_cycle", "Max new positions per cycle (0 = no limit)", "int", "Risk limits", 0, 200, "0 = open as many as the other limits allow. Cash reserve, max invested, per-company, per-theme and stress-loss limits always apply."),
    F("risk.min_adv_usd", "Minimum average daily dollar volume", "int", "Risk limits", 100_000, 1_000_000_000, "Illiquid stocks below this are rejected (USD)."),
    # ---------------------------------------------------------------- portfolio / execution
    F("portfolio.slippage_bps", "Simulated slippage", "int", "Execution", 0, 100, "Basis points added to buys / removed from sells."),
    F("portfolio.entry_score", "Minimum eligibility score to trade", "int", "Execution", 0, 100, "0-100 score from source quality, entity match, size and freshness."),
    F("quotes.min_providers", "Fresh quote sources required to trade", "int", "Execution", 1, 6, "1 = one fresh price is enough. With 2+, that many fresh providers must agree."),
    F("quotes.max_deviation", "Max price disagreement between providers", "pct", "Execution", 0.0005, 0.05, ""),
    F("quotes.max_age_seconds", "Max quote age for exits (seconds)", "int", "Execution", 30, 900, ""),
    F("quotes.entry_max_age_seconds", "Max quote age for entries (seconds)", "int", "Execution", 30, 600, ""),
    F("quotes.allow_single_source", "Allow trading on a single quote source", "bool", "Execution", help="On = one fresh source is enough regardless of the setting above."),
    F("quotes.probe_minutes", "Check quote providers every (minutes, 0 = off)", "int", "Execution", 0, 120, "A cheap SPY check of each provider, market open or closed, so the Quote providers table on the System tab is always current."),
    F("quotes.single_source_max_move_pct", "A single source must be within this of the last close", "pct", "Execution", 0.02, 1.0, "Sanity check for a lone price: further away than this needs a second source to confirm. 0.20 = 20%."),
    # ---------------------------------------------------------------- refresh times / intelligence
    F("schedule.cycle_minutes", "Intelligence cycle every (minutes)", "int", "Refresh times", 5, 240, "News, evidence, theses, entries, monitoring. Applied to the systemd timer.", restart="schedule"),
    F("schedule.guard_minutes", "Exit guard every (minutes)", "int", "Refresh times", 1, 15, "Fresh-quote stop/target checks during US market hours.", restart="schedule"),
    F("prices.refresh_closed_minutes", "Refresh prices while the market is CLOSED every (minutes, 0 = off)", "int", "Refresh times", 0, 240, "Pre-market and after-hours prices of what you hold, what is queued to buy and what was just decided on."),
    F("prices.refresh_open_minutes", "Refresh dashboard prices while the market is OPEN every (minutes, 0 = off)", "int", "Refresh times", 0, 60, "Exits are watched separately by the exit guard (above)."),
    F("prices.max_symbols", "Max stocks refreshed per round", "int", "Refresh times", 5, 100, "Yahoo allows about 30 requests a minute."),
    F("prices.extended_hours", "Include pre-market and after-hours trades", "bool", "Refresh times", help="Off = regular-session prices only."),
    F("schedule.update_hours", "Check GitHub for updates every (hours)", "int", "Refresh times", 1, 48, "", restart="schedule"),
    F("intelligence.regime_refresh_hours", "Market regime refresh (hours)", "int", "Refresh times", 1, 48, ""),
    F("intelligence.company_refresh_hours", "Company profile refresh (hours)", "int", "Refresh times", 6, 720, ""),
    F("intelligence.contradiction_hours", "Contradiction search per thesis (hours)", "int", "Refresh times", 1, 48, ""),
    F("intelligence.stale_event_hours", "News older than this is stale (hours)", "int", "Refresh times", 1, 720, ""),
    F("intelligence.trade_event_max_age_hours", "Trade only on news younger than (hours)", "int", "Refresh times", 1, 72, ""),
    F("intelligence.learning_min_samples", "Samples before the system self-adjusts", "int", "Refresh times", 5, 500, ""),
    F("intelligence.gdelt_enabled", "Use GDELT world-event queries", "bool", "Refresh times"),
    F("intelligence.allow_thesis_entries", "Allow thesis-driven entries (no news trigger)", "bool", "Refresh times"),
    F("intelligence.thesis_entry_min_probability", "Min thesis probability for thesis-driven entries", "pct", "Refresh times", 0.5, 0.95, ""),
    F("intelligence.finnhub_company_news_max", "Companies polled for Finnhub news per cycle", "int", "Refresh times", 0, 50, ""),
    F("intelligence.max_company_refresh_per_cycle", "Company profiles refreshed per cycle", "int", "Refresh times", 0, 20, ""),
    # ---------------------------------------------------------------- LLM
    F("intelligence.llm_enabled", "Enable the LLM analyst", "bool", "LLM analyst", help="Needs an API key below. Off = the deterministic engine only."),
    F("intelligence.llm_daily_budget_usd", "Daily spend cap (USD)", "float", "LLM analyst", 0.0, 50.0, "Hard stop: calls are skipped once reached."),
    F("intelligence.llm_monthly_budget_usd", "Monthly spend cap (USD)", "float", "LLM analyst", 0.0, 500.0, ""),
    F("intelligence.llm_max_per_cycle", "News items analysed per cycle", "int", "LLM analyst", 0, 30, ""),
    F("intelligence.llm_daily_max_calls.triage", "Max cheap (triage) calls per day", "int", "LLM analyst", 0, 2000, ""),
    F("intelligence.llm_daily_max_calls.analyst", "Max deep (analyst) calls per day", "int", "LLM analyst", 0, 500, ""),
    F("intelligence.redteam_enabled", "Red-team challenge before each entry", "bool", "LLM analyst"),
    F("intelligence.redteam_required", "Block entries if the red team is unavailable", "bool", "LLM analyst"),
    F("intelligence.redteam_min_conviction", "Minimum conviction after the red team", "pct", "LLM analyst", 0.0, 0.95, ""),
    F("intelligence.max_trade_order", "Highest causal order allowed to trigger a trade", "int", "LLM analyst", 1, 3, "1 = directly affected only (recommended)."),
    F("intelligence.chain_decay", "Confidence kept per causal hop", "float", "LLM analyst", 0.1, 0.9, ""),
    F("intelligence.max_knowledge_refresh_per_cycle", "Companies whose SEC filings are read per cycle", "int", "LLM analyst", 0, 10, ""),
    # ---------------------------------------------------------------- runtime
    F("runtime.require_password", "Require a password to use Settings", "bool", "System", help="Off (default): anyone on your network can open Settings. Turn on to be asked to create a password on your next visit."),
    F("runtime.allowed_hosts", "Allowed hostnames (only if you use a custom name or proxy)", "hostlist", "System", help="Comma-separated. Private IPs, localhost and names like orangepizero3 or pi.local are always allowed."),
    F("maintenance.weekly_cleanup", "Weekly database clean-up (Saturdays, US market closed)", "bool", "Database", help="Expires news, events and run logs that nothing still points at. Never touches positions, the ledger, forecasts, evidence or portfolio history."),
    F("maintenance.news_days", "Keep news and events for (days)", "int", "Database", 7, 365, "Anything a decision, forecast or piece of evidence refers to is kept regardless."),
    F("maintenance.log_days", "Keep run logs and narrative history for (days)", "int", "Database", 7, 365, "The newest state of every thesis is always kept."),
    F("notifications.ntfy_topic", "Phone push: ntfy.sh topic", "topic", "System", help="Optional. Install the free ntfy app, subscribe to a long, hard-to-guess topic name, and enter it here to get each open/close report on your phone. Empty = off."),
    F("runtime.contact_email", "Contact email (sent to SEC in the User-Agent)", "email", "System", help="SEC asks for a real contact."),
    F("runtime.cycle_budget_seconds", "Cycle time budget (seconds)", "int", "System", 120, 500, "Slow company/filing refreshes stop early after ~40-50% of this so decisions are never starved."),
    F("runtime.min_available_memory_mb", "Skip a cycle below this free memory (MB)", "int", "System", 50, 500, ""),
    F("runtime.max_load_per_core", "Skip a cycle above this load per core", "float", "System", 0.5, 4.0, ""),
    F("runtime.bind_port", "Dashboard port", "int", "System", 1024, 65535, "Takes effect after restarting the dashboard.", restart="dashboard"),
]
BY_NAME = {f.name: f for f in FIELDS}
GROUP_ORDER = ["Risk limits", "Swing strategy", "Execution", "Refresh times", "LLM analyst", "Database", "System"]
TIERS = ("triage", "analyst")
PROVIDERS = ("anthropic", "github")
MODEL_RX = re.compile(r"^[A-Za-z0-9._:/-]{3,80}$")
EMAIL_RX = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,120}\.[A-Za-z]{2,}$")

SECRETS = [
    ("ANTHROPIC_API_KEY", "Anthropic API key", "LLM analyst (Claude). console.anthropic.com -> API keys; also set a monthly spend limit there."),
    ("GITHUB_MODELS_TOKEN", "GitHub Models token", "Optional free-tier LLM provider (github.com/settings/personal-access-tokens, 'Models: read')."),
    ("TWELVE_DATA_API_KEY", "Twelve Data key", "Quote provider."), ("FINNHUB_API_KEY", "Finnhub key", "Quotes, company news, fundamentals, insiders (the 20-character API key, not the webhook secret)."),
    ("ALPACA_API_KEY", "Alpaca key ID", "Quote provider."), ("ALPACA_API_SECRET", "Alpaca secret", "Quote provider."),
    ("MASSIVE_API_KEY", "Massive key", "Quote provider."), ("ALPHA_VANTAGE_API_KEY", "Alpha Vantage key", "Valuation only (no timestamps)."),
    ("TIINGO_API_KEY", "Tiingo key", "Quote provider."), ("EODHD_API_KEY", "EODHD key", "Quote provider (delayed)."),
]
SECRET_NAMES = [k for k, _, _ in SECRETS]
SECRET_RX = re.compile(r"^[\x21-\x7e]{8,200}$")      # printable ASCII, no spaces/quotes issues checked below


# ------------------------------------------------------------------------------------------- dict helpers
def get_path(d: dict, path: str):
    for k in path.split("."):
        d = d[k]
    return d


def set_path(d: dict, path: str, value) -> None:
    ks = path.split(".")
    for k in ks[:-1]:
        d = d.setdefault(k, {})
    d[ks[-1]] = value


def diff(new, base):
    """Leaves of ``new`` that differ from ``base`` (nested dict result)."""
    if isinstance(new, dict):
        out = {}
        for k, v in new.items():
            b = base.get(k) if isinstance(base, dict) else None
            d = diff(v, b)
            if d not in ({}, None) or (not isinstance(v, dict) and (not isinstance(base, dict) or k not in base)):
                out[k] = d
        return out
    return new if new != base else None


def _diff_clean(new: dict, base: dict) -> dict:
    out: dict = {}
    for k, v in new.items():
        if isinstance(v, dict):
            sub = _diff_clean(v, base.get(k, {}) if isinstance(base.get(k), dict) else {})
            if sub:
                out[k] = sub
        elif k not in base or base[k] != v:
            out[k] = v
    return out


# ------------------------------------------------------------------------------------------- parsing
def display_value(f: Field, cfg: dict) -> str:
    v = get_path(cfg, f.path)
    if f.kind == "pct":
        return ("%.4f" % (v * 100)).rstrip("0").rstrip(".")
    if f.kind == "bool":
        return "1" if v else "0"
    if f.kind == "hostlist":
        return ", ".join(v)
    return str(v)


def _parse(f: Field, raw: str):
    raw = (raw or "").strip()
    if f.kind == "bool":
        return raw.lower() in ("1", "true", "on", "yes")
    if f.kind == "topic":
        if raw and not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", raw):
            raise ValueError("8-64 letters, digits, - or _ (or leave empty)")
        return raw
    if f.kind == "hostlist":
        names = [h.strip().lower() for h in re.split(r"[,\s]+", raw) if h.strip()]
        if len(names) > 10 or any(not re.fullmatch(r"[a-z0-9]([a-z0-9.-]{0,60}[a-z0-9])?", h) for h in names):
            raise ValueError("up to 10 hostnames (letters, digits, dots, hyphens), separated by commas")
        return names
    if raw == "":
        raise ValueError("a value is required")
    if f.kind == "email":
        if not EMAIL_RX.match(raw):
            raise ValueError("not a valid email address")
        return raw
    try:
        x = float(raw)
    except ValueError:
        raise ValueError("must be a number")
    if x != x or x in (float("inf"), float("-inf")):
        raise ValueError("must be a finite number")
    if f.kind == "pct":
        x = round(x / 100.0, 6)
    elif f.kind == "int":
        if x != int(x):
            raise ValueError("must be a whole number")
        x = int(x)
    else:
        x = round(x, 6)
    if f.lo is not None and x < f.lo - 1e-12 or f.hi is not None and x > f.hi + 1e-12:
        shown = (lambda v: ("%g%%" % (v * 100)) if f.kind == "pct" else ("%g" % v))
        raise ValueError("must be between %s and %s" % (shown(f.lo), shown(f.hi)))
    return x


def cross_check(new: dict, current: dict) -> Tuple[List[str], List[str]]:
    """Rules that span several settings. Used by dashboard saves AND release (deploy) entries, so neither can create an inconsistent config.
    Only problems the NEW config INTRODUCES are reported: a pre-existing inconsistency must not block an unrelated change."""
    e_new, w_new = _cross_rules(new, current)
    e_old, _ = _cross_rules(current, current)
    return [e for e in e_new if e not in e_old], w_new


def _cross_rules(new: dict, current: dict) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    r, q, i = new["risk"], new["quotes"], new["intelligence"]
    if r["max_position_pct"] > r["max_issuer_pct"]:
        errors.append("Max position size cannot exceed max exposure to one company")
    if r["min_position_pct"] > r["max_position_pct"]:
        errors.append("Smallest position cannot exceed the max position size")
    if r["max_gross_pct"] + r["min_cash_pct"] > 1.0 + 1e-9:
        errors.append("Max invested + minimum cash reserve cannot exceed 100%")
    if r["daily_loss_pause_pct"] > r["drawdown_pause_pct"]:
        errors.append("Daily-loss pause cannot exceed the drawdown pause")
    if q["entry_max_age_seconds"] > q["max_age_seconds"]:
        errors.append("Entry quote age cannot exceed the exit quote age")
    if i["llm_daily_budget_usd"] > i["llm_monthly_budget_usd"]:
        errors.append("Daily LLM budget cannot exceed the monthly budget")
    if q["allow_single_source"] and not current["quotes"]["allow_single_source"]:
        warnings.append("Single-source quotes enabled: trades will no longer be cross-checked between providers.")
    if r["max_position_pct"] > 0.25:
        warnings.append("Max position above 25% of equity is very concentrated.")
    return errors, warnings


def apply_form(form: Dict[str, str], current: dict, base: dict, source_defaults) -> Tuple[dict, List[str], List[str]]:
    """Returns (new_effective_cfg, errors, warnings). Nothing is written here.
    ``source_defaults``: {feed id: default poll interval seconds} (a plain list of ids is accepted too)."""
    source_defaults = source_defaults if isinstance(source_defaults, dict) else {sid: None for sid in source_defaults}
    new = copy.deepcopy(current)
    errors: List[str] = []
    warnings: List[str] = []
    for f in FIELDS:
        if f.name not in form:
            continue
        try:
            set_path(new, f.path, _parse(f, form[f.name]))
        except ValueError as e:
            errors.append("%s: %s" % (f.label, e))
    # LLM tiers: provider + model + prices (prices make the dollar budget meaningful for any model)
    for t in TIERS:
        pk, mk, ik, ok = ("tier.%s.%s" % (t, s) for s in ("provider", "model", "price_in", "price_out"))
        if pk in form or mk in form:
            prov, model = form.get(pk, new["intelligence"]["llm_tiers"][t]["provider"]).strip(), form.get(mk, "").strip() or new["intelligence"]["llm_tiers"][t]["model"]
            if prov not in PROVIDERS:
                errors.append("%s tier provider must be one of %s" % (t, ", ".join(PROVIDERS)))
                continue
            if not MODEL_RX.match(model):
                errors.append("%s tier model id looks invalid" % t)
                continue
            new["intelligence"]["llm_tiers"][t] = {"provider": prov, "model": model}
            try:
                pin, pout = float(form.get(ik, "") or "nan"), float(form.get(ok, "") or "nan")
            except ValueError:
                pin = pout = float("nan")
            known = new["intelligence"]["llm_prices"].get(model)
            if pin == pin and pout == pout and 0 <= pin <= 1000 and 0 <= pout <= 1000:
                new["intelligence"]["llm_prices"][model] = [pin, pout]
            elif known is None:
                errors.append("%s tier: enter the input/output price per million tokens for model %s (needed to enforce the spend cap; 0/0 for a free tier)" % (t, model))
    # per-feed overrides
    so = copy.deepcopy(current.get("source_overrides", {}))
    for sid, dflt in source_defaults.items():
        ek, ik = "src.%s.enabled" % sid, "src.%s.interval" % sid
        o = dict(so.get(sid, {}))
        if ek in form:
            en = form[ek].strip().lower() in ("1", "true", "on", "yes")
            if en:
                o.pop("enabled", None)          # enabled is the default: store only the exception
            else:
                o["enabled"] = False
        if ik in form and form[ik].strip():
            try:
                iv = int(float(form[ik]))
                if not 60 <= iv <= 86400:
                    raise ValueError
                if dflt is not None and iv == int(dflt):
                    o.pop("interval", None)     # back to the default: nothing to store
                else:
                    o["interval"] = iv
            except ValueError:
                errors.append("Feed %s: interval must be 60-86400 seconds" % sid)
        if o:
            so[sid] = o
        else:
            so.pop(sid, None)
    new["source_overrides"] = so
    e2, w2 = cross_check(new, current)
    errors += e2
    warnings += w2
    return new, errors, warnings


# ------------------------------------------------------------------------------------------- persistence
def _atomic_write(path: str, text: str, mode: int = 0o600) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def overrides_for(new_cfg: dict, base_cfg: dict) -> dict:
    """Only EDITABLE settings are ever persisted (schema fields, LLM tiers/prices, feed overrides). Anything else that happens to differ
    from the base config (runtime paths, hand-added keys) is deliberately not written."""
    ov: dict = {}
    for f in FIELDS:
        a, b = get_path(new_cfg, f.path), get_path(base_cfg, f.path)
        if a != b:
            set_path(ov, f.path, a)
    for key in ("llm_tiers", "llm_prices"):
        if new_cfg["intelligence"][key] != base_cfg["intelligence"][key]:
            ov.setdefault("intelligence", {})[key] = new_cfg["intelligence"][key]
    if new_cfg.get("source_overrides") != base_cfg.get("source_overrides", {}):
        if new_cfg.get("source_overrides"):
            ov["source_overrides"] = new_cfg["source_overrides"]
    return ov


def save_overrides(ov: dict, path: Optional[str] = None) -> None:
    _atomic_write(path or config.SETTINGS_PATH, json.dumps(ov, indent=1, sort_keys=True), 0o640)


def reset_overrides(path: Optional[str] = None) -> None:
    try:
        os.unlink(path or config.SETTINGS_PATH)
    except FileNotFoundError:
        pass


# ------------------------------------------------------------------------------------------- secrets
def read_secrets(path: Optional[str] = None) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        with open(path or config.SECRETS_PATH) as f:
            for line in f:
                if "=" in line and not line.startswith("#"):
                    k, v = line.rstrip("\n").split("=", 1)
                    if k in SECRET_NAMES and v:
                        out[k] = v
    except OSError:
        pass
    return out


def mask(v: Optional[str]) -> str:
    return "not set" if not v else "set (ends …%s)" % v[-4:]


def secret_status(path: Optional[str] = None) -> Dict[str, str]:
    stored = read_secrets(path)
    return {k: mask(stored.get(k) or os.getenv(k)) for k in SECRET_NAMES}


def apply_secret_form(form: Dict[str, str], path: Optional[str] = None, environ=None) -> Tuple[List[str], List[str]]:
    """Blank field = unchanged. 'clear.<KEY>' = remove the dashboard-stored value. Returns (changed_names, errors). Values are never returned."""
    environ = os.environ if environ is None else environ
    cur = read_secrets(path)
    changed, errors = [], []
    for k in SECRET_NAMES:
        if form.get("clear." + k):
            if k in cur:
                del cur[k]
                environ.pop(k, None)
                changed.append(k)
            continue
        v = (form.get("secret." + k) or "").strip()
        if not v:
            continue
        if not SECRET_RX.match(v) or any(c in v for c in "\"'\\"):
            errors.append("%s: must be 8-200 visible characters with no spaces or quotes" % k)
            continue
        cur[k] = v
        environ[k] = v
        changed.append(k)
    if changed and not errors:
        _atomic_write(path or config.SECRETS_PATH, "# Managed by the dashboard. Do not edit by hand.\n" + "".join("%s=%s\n" % (k, v) for k, v in sorted(cur.items())), 0o600)
    return changed, errors


# =============================================================================================================
# Modified-on tracking + release (deploy) entries.
#
# Rule of the road: a deployment NEVER silently overwrites something you changed.
#   * every change records (path -> modified timestamp, who) in settings.meta.json
#   * a release may ship entries in deploy-settings.json: {id, key, value, released, force, note}
#       - key never changed by you          -> applied (recorded as 'deploy')
#       - you changed it, entry not forced   -> NOT applied; becomes a suggestion you can Accept / Dismiss
#                                                (and if your change is NEWER than the release, it is dropped silently)
#       - force=true                         -> applied, exactly once per entry id (a later redeploy never re-forces it)
# =============================================================================================================
from .timeutil import iso as _iso, parse_ts as _parse_ts, utcnow as _utcnow  # noqa: E402


def _state_dir() -> str:
    return os.path.dirname(config.SETTINGS_PATH) or "."


def meta_path() -> str:
    return os.path.join(_state_dir(), "settings.meta.json")


def applied_path() -> str:
    return os.path.join(_state_dir(), "deploy-applied.json")


def pending_path() -> str:
    return os.path.join(_state_dir(), "deploy-pending.json")


def _load_json(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def flat(d, prefix: str = "") -> Dict[str, object]:
    """{'risk': {'max_position_pct': 0.08}} -> {'risk.max_position_pct': 0.08}"""
    out: Dict[str, object] = {}
    if isinstance(d, dict):
        for k, v in d.items():
            if isinstance(v, dict):
                out.update(flat(v, prefix + k + "."))
            else:
                out[prefix + k] = v
    return out


def _unset(d: dict, path: str) -> None:
    """Remove one dotted path from a nested dict and prune any dicts left empty."""
    ks = path.split(".")
    stack = [d]
    for k in ks[:-1]:
        nxt = stack[-1].get(k)
        if not isinstance(nxt, dict):
            return
        stack.append(nxt)
    stack[-1].pop(ks[-1], None)
    for i in range(len(ks) - 2, -1, -1):
        if not stack[i + 1]:
            stack[i].pop(ks[i], None)


def load_overrides(path: Optional[str] = None) -> dict:
    ov = _load_json(path or config.SETTINGS_PATH, {})
    return ov if isinstance(ov, dict) else {}


def load_meta() -> Dict[str, dict]:
    m = _load_json(meta_path(), {})
    return m if isinstance(m, dict) else {}


def record_changes(old_ov: dict, new_ov: dict, who: str = "user", note: str = "", now=None) -> List[str]:
    """Stamp every added / changed / removed setting with (modified time, who). Returns the changed paths."""
    o, n = flat(old_ov), flat(new_ov)
    changed = sorted(k for k in set(o) | set(n) if o.get(k) != n.get(k))
    if changed:
        meta = load_meta()
        for k in changed:
            meta[k] = {"modified": _iso(now), "by": who, **({"removed": True} if k not in n else {}), **({"note": note} if note else {})}
        _atomic_write(meta_path(), json.dumps(meta, indent=1, sort_keys=True), 0o640)
    return changed


def record_secret_change(names: List[str], now=None) -> None:
    if names:
        meta = load_meta()
        for k in names:
            meta["secret." + k] = {"modified": _iso(now), "by": "user"}
        _atomic_write(meta_path(), json.dumps(meta, indent=1, sort_keys=True), 0o640)


def save_overrides_tracked(new_ov: dict, who: str = "user", note: str = "", now=None) -> List[str]:
    old = load_overrides()
    save_overrides(new_ov)
    return record_changes(old, new_ov, who, note, now)


BY_PATH = {f.path: f for f in FIELDS}


def validate_native(f: Field, value):
    """Validate a value given in native units (percent settings are FRACTIONS here, as stored)."""
    if f.kind == "bool":
        if not isinstance(value, bool):
            raise ValueError("must be true/false")
        return value
    if f.kind == "hostlist":
        return _parse(f, ", ".join(value) if isinstance(value, list) else str(value))
    if f.kind in ("email", "topic"):
        return _parse(f, str(value))
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("must be a number")
    if f.kind == "int" and value != int(value):
        raise ValueError("must be a whole number")
    v = int(value) if f.kind == "int" else float(value)
    if (f.lo is not None and v < f.lo - 1e-12) or (f.hi is not None and v > f.hi + 1e-12):
        raise ValueError("outside the allowed range")
    return v


def _effective(ov: dict) -> dict:
    return config.deep_merge(config.load_config(with_overrides=False), ov)


def _apply_value(ov: dict, f: Field, value, who: str, note: str, now=None) -> Tuple[bool, str]:
    """Set one setting (validated + cross-checked). Returns (ok, message)."""
    cur_eff = _effective(ov)
    new_eff = copy.deepcopy(cur_eff)
    set_path(new_eff, f.path, value)
    errs, _ = cross_check(new_eff, cur_eff)
    if errs:
        return False, "; ".join(errs)
    base = config.load_config(with_overrides=False)
    new_ov = copy.deepcopy(ov)
    if get_path(base, f.path) == value:
        _unset(new_ov, f.path)             # back at the shipped default: drop the override instead of pinning the value
    else:
        set_path(new_ov, f.path, value)
    save_overrides_tracked(new_ov, who, note, now)
    return True, "ok"


def apply_deploy(entries: List[dict], now=None) -> List[dict]:
    """Process release entries under the rules above. Idempotent: every entry id is decided at most once."""
    now = now or _utcnow()
    applied = _load_json(applied_path(), {})
    pending = _load_json(pending_path(), [])
    report: List[dict] = []
    for e in entries or []:
        eid, key = str(e.get("id", "")), str(e.get("key", ""))
        rec = {"id": eid, "key": key}
        f = BY_PATH.get(key)
        if not eid or f is None or "value" not in e:
            report.append({**rec, "status": "rejected", "detail": "unknown setting or malformed entry"})
            continue
        if eid in applied or any(p["id"] == eid for p in pending):
            report.append({**rec, "status": "already_decided", "detail": applied.get(eid, {}).get("status", "pending")})
            continue
        try:
            value = validate_native(f, e["value"])
        except ValueError as ex:
            applied[eid] = {"status": "rejected", "at": _iso(now), "detail": str(ex)}
            report.append({**rec, "status": "rejected", "detail": str(ex)})
            continue
        meta = load_meta().get(key)
        user_mod = bool(meta and meta.get("by") == "user")
        force = bool(e.get("force"))
        if user_mod and not force:
            released, mod = _parse_ts(e.get("released")), _parse_ts(meta.get("modified"))
            if released and mod and mod >= released:
                applied[eid] = {"status": "superseded_by_user", "at": _iso(now)}
                report.append({**rec, "status": "superseded_by_user", "detail": "you changed it after this release"})
            else:
                pending.append({"id": eid, "key": key, "label": f.label, "value": value, "note": str(e.get("note", ""))[:300], "released": e.get("released"),
                                "your_value": get_path(_effective(load_overrides()), key), "your_change": meta.get("modified"), "suggested_at": _iso(now)})
                report.append({**rec, "status": "suggested", "detail": "you changed this setting; the release value is offered, not applied"})
            continue
        ok, msg = _apply_value(load_overrides(), f, value, "deploy", "release %s%s" % (eid, " (forced)" if force else ""), now)
        applied[eid] = {"status": "applied" if ok else "rejected", "at": _iso(now), "forced": force, **({} if ok else {"detail": msg})}
        report.append({**rec, "status": "applied" if ok else "rejected", "detail": ("forced" if force else "default for an untouched setting") if ok else msg})
    _atomic_write(applied_path(), json.dumps(applied, indent=1, sort_keys=True), 0o640)
    _atomic_write(pending_path(), json.dumps(pending, indent=1), 0o640)
    return report


def pending_suggestions() -> List[dict]:
    p = _load_json(pending_path(), [])
    return p if isinstance(p, list) else []


def resolve_suggestion(eid: str, accept: bool, now=None) -> Tuple[bool, str]:
    pending, applied = pending_suggestions(), _load_json(applied_path(), {})
    item = next((p for p in pending if p["id"] == eid), None)
    if not item:
        return False, "no such suggestion"
    msg = "dismissed"
    ok = True
    if accept:
        f = BY_PATH.get(item["key"])
        ok, msg = _apply_value(load_overrides(), f, item["value"], "user", "accepted release suggestion %s" % eid, now) if f else (False, "unknown setting")
    if ok:
        applied[eid] = {"status": "accepted" if accept else "dismissed", "at": _iso(now)}
        _atomic_write(applied_path(), json.dumps(applied, indent=1, sort_keys=True), 0o640)
        _atomic_write(pending_path(), json.dumps([p for p in pending if p["id"] != eid], indent=1), 0o640)
    return ok, msg
