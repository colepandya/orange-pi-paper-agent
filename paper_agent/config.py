"""Configuration: defaults < /etc/paper-agent/config.json < environment. Secrets live only in env files."""
from __future__ import annotations

import copy
import json
import os
from typing import Any, Dict

CONFIG_DIR = os.getenv("PAPER_AGENT_CONFIG_DIR", "/etc/paper-agent")
STATE_DIR = os.getenv("PAPER_AGENT_STATE_DIR", "/var/lib/paper-agent")
DB_PATH = os.getenv("PAPER_AGENT_DB", os.path.join(STATE_DIR, "paper-agent.db"))
SETTINGS_PATH = os.getenv("PAPER_AGENT_SETTINGS", os.path.join(STATE_DIR, "settings.json"))     # dashboard-managed overrides (written by the unprivileged dashboard)
SECRETS_PATH = os.getenv("PAPER_AGENT_SECRETS", os.path.join(STATE_DIR, "secrets.env"))         # dashboard-managed API keys (0600)
CONTROL_DIR = os.getenv("PAPER_AGENT_CONTROL", os.path.join(STATE_DIR, "control"))              # request/result files for the root control worker
ADMIN_PATH = os.getenv("PAPER_AGENT_ADMIN", os.path.join(STATE_DIR, "admin.json"))              # dashboard login (salted PBKDF2 hash)

DEFAULTS: Dict[str, Any] = {
    "portfolio": {"starting_cash": 1_000_000.0, "slippage_bps": 8, "entry_score": 65},
    # Safer than the legacy 50% / 80% / 90% limits. Survival > returns (OMEGA principle 6).
    "risk": {
        "max_position_pct": 0.10, "max_issuer_pct": 0.15, "max_theme_pct": 0.30, "max_gross_pct": 0.80,
        "min_cash_pct": 0.20, "risk_per_trade_pct": 0.010, "min_position_pct": 0.01,
        "drawdown_pause_pct": 0.12, "daily_loss_pause_pct": 0.03, "survival_floor": 0.80,
        "ruin_loss_pct": 0.20, "max_stress_loss_pct": 0.20, "min_ev_net": 0.006, "min_confidence": 0.45, "max_new_per_cycle": 3,
        "max_spread_pct": 0.02, "min_adv_usd": 2_000_000,
    },
    "runtime": {"min_available_memory_mb": 120, "max_load_per_core": 1.5, "pihole_health_required": False,
                "bind_host": "0.0.0.0", "bind_port": 8088, "contact_email": "paper-agent@example.invalid",
                "require_password": False, "pause_file": os.path.join(STATE_DIR, "PAUSE_TRADES"), "lock_file": os.path.join(STATE_DIR, "cycle.lock")},
    "quotes": {"min_providers": 2, "max_deviation": 0.005, "max_age_seconds": 180, "entry_max_age_seconds": 90,
               "allow_single_source": False,
               "limits": {"TWELVE_DATA": [7, 60], "FINNHUB": [50, 60], "ALPACA": [100, 60], "MASSIVE": [4, 60],
                          "ALPHA_VANTAGE": [20, 86400], "TIINGO": [40, 3600], "EODHD": [18, 86400], "YAHOO": [30, 60]}},
    "schedule": {"cycle_minutes": 10, "guard_minutes": 2, "update_hours": 3},      # applied to systemd timers by the control worker
    "source_overrides": {},                                                       # {"<source id>": {"enabled": bool, "interval": seconds}}
    "intelligence": {
        "stale_event_hours": 72, "trade_event_max_age_hours": 24, "thesis_entry_min_probability": 0.70,
        "allow_thesis_entries": True, "contradiction_hours": 6, "company_refresh_hours": 72,
        "regime_refresh_hours": 6, "learning_min_samples": 20, "llm_enabled": False,
        "llm_max_per_cycle": 6, "gdelt_enabled": True,
        "finnhub_company_news_max": 15, "max_company_refresh_per_cycle": 6,
        # --- LLM analyst (provider-agnostic). Off until llm_enabled=true AND a key for the tier's provider exists.
        "llm_providers": {
            "anthropic": {"kind": "anthropic", "key_env": "ANTHROPIC_API_KEY", "url": "https://api.anthropic.com/v1/messages"},
            "github": {"kind": "openai", "key_env": "GITHUB_MODELS_TOKEN", "url": "https://models.github.ai/inference/chat/completions"},
        },
        "llm_tiers": {"triage": {"provider": "anthropic", "model": "claude-haiku-5-5"}, "analyst": {"provider": "anthropic", "model": "claude-sonnet-5-5"}},
        "llm_prices": {"claude-haiku-5-5": [0.10, 0.50], "claude-sonnet-5-5": [2.0, 10.0], "claude-opus-5-5": [4.0, 20.0]},   # USD per million tokens in/out (verify on Anthropic's pricing page)
        "llm_daily_budget_usd": 0.75, "llm_monthly_budget_usd": 15.0, "llm_daily_max_calls": {"triage": 150, "analyst": 40},
        "max_trade_order": 1, "chain_decay": 0.55, "chain_order_prior": {"1": 0.60, "2": 0.45, "3": 0.30},
        "max_knowledge_refresh_per_cycle": 2, "redteam_enabled": True, "redteam_min_conviction": 0.45, "redteam_required": False,
    },
}


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_env_file(path: str, override: bool = False) -> int:
    """Load KEY=VALUE lines into os.environ without overriding values already set. Returns count loaded."""
    n = 0
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and v and (override or k not in os.environ):
                    os.environ[k] = v
                    n += 1
    except OSError:
        pass
    return n


def load_config(path: str = "", with_overrides: bool = True, settings_path: str = "") -> dict:
    """defaults < /etc/paper-agent/config.json < dashboard settings (state dir)."""
    path = path or os.path.join(CONFIG_DIR, "config.json")
    user: dict = {}
    try:
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
    except (OSError, ValueError):
        pass
    cfg = deep_merge(DEFAULTS, user)
    if with_overrides:
        try:
            with open(settings_path or SETTINGS_PATH, encoding="utf-8") as f:
                ov = json.load(f)
            if isinstance(ov, dict):
                cfg = deep_merge(cfg, ov)
        except (OSError, ValueError):
            pass
    # Legacy config used portfolio.* limits of 50/80/90%. They are intentionally NOT honoured: the new
    # `risk` section is authoritative. Starting cash / slippage / entry score are carried across.
    return cfg


def load_json(path: str, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def load_secrets() -> None:
    for name in ("market-data.env", "secrets.env"):
        load_env_file(os.path.join(CONFIG_DIR, name))
    load_env_file(SECRETS_PATH, override=True)      # keys entered in the dashboard win over file / systemd values
