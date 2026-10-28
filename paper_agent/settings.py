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
    F("risk.min_ev_net", "Minimum expected value (net of costs)", "pct", "Risk limits", 0.0, 0.05, "Candidates below this expected return are not traded."),
    F("risk.min_confidence", "Minimum forecast confidence", "pct", "Risk limits", 0.0, 0.95, ""),
    F("risk.max_new_per_cycle", "Max new positions per cycle", "int", "Risk limits", 0, 10, ""),
    F("risk.min_adv_usd", "Minimum average daily dollar volume", "int", "Risk limits", 100_000, 1_000_000_000, "Illiquid stocks below this are rejected (USD)."),
    # ---------------------------------------------------------------- portfolio / execution
    F("portfolio.slippage_bps", "Simulated slippage", "int", "Execution", 0, 100, "Basis points added to buys / removed from sells."),
    F("portfolio.entry_score", "Minimum eligibility score to trade", "int", "Execution", 0, 100, "0-100 score from source quality, entity match, size and freshness."),
    F("quotes.min_providers", "Quote providers that must agree", "int", "Execution", 1, 6, "Execution needs this many fresh, agreeing prices."),
    F("quotes.max_deviation", "Max price disagreement between providers", "pct", "Execution", 0.0005, 0.05, ""),
    F("quotes.max_age_seconds", "Max quote age for exits (seconds)", "int", "Execution", 30, 900, ""),
    F("quotes.entry_max_age_seconds", "Max quote age for entries (seconds)", "int", "Execution", 30, 600, ""),
    F("quotes.allow_single_source", "Allow trading on a single quote source", "bool", "Execution", help="Unsafe: removes the cross-check between providers."),
    # ---------------------------------------------------------------- refresh times / intelligence
    F("schedule.cycle_minutes", "Intelligence cycle every (minutes)", "int", "Refresh times", 5, 240, "News, evidence, theses, entries, monitoring. Applied to the systemd timer.", restart="schedule"),
    F("schedule.guard_minutes", "Exit guard every (minutes)", "int", "Refresh times", 1, 15, "Fresh-quote stop/target checks during US market hours.", restart="schedule"),
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
    F("runtime.contact_email", "Contact email (sent to SEC in the User-Agent)", "email", "System", help="SEC asks for a real contact."),
    F("runtime.min_available_memory_mb", "Skip a cycle below this free memory (MB)", "int", "System", 50, 500, ""),
    F("runtime.max_load_per_core", "Skip a cycle above this load per core", "float", "System", 0.5, 4.0, ""),
    F("runtime.bind_port", "Dashboard port", "int", "System", 1024, 65535, "Takes effect after restarting the dashboard.", restart="dashboard"),
]
BY_NAME = {f.name: f for f in FIELDS}
GROUP_ORDER = ["Risk limits", "Execution", "Refresh times", "LLM analyst", "System"]
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
    return str(v)


def _parse(f: Field, raw: str):
    raw = (raw or "").strip()
    if f.kind == "bool":
        return raw.lower() in ("1", "true", "on", "yes")
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
