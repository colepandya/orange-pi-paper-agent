"""Configuration: defaults < /etc/paper-agent/config.json < environment. Secrets live only in env files."""
from __future__ import annotations

import copy
import json
import os
from typing import Any, Dict

CONFIG_DIR = os.getenv("PAPER_AGENT_CONFIG_DIR", "/etc/paper-agent")
STATE_DIR = os.getenv("PAPER_AGENT_STATE_DIR", "/var/lib/paper-agent")
DB_PATH = os.getenv("PAPER_AGENT_DB", os.path.join(STATE_DIR, "paper-agent.db"))

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
                "pause_file": os.path.join(STATE_DIR, "PAUSE_TRADES"), "lock_file": os.path.join(STATE_DIR, "cycle.lock")},
    "quotes": {"min_providers": 2, "max_deviation": 0.005, "max_age_seconds": 180, "entry_max_age_seconds": 90,
               "allow_single_source": False,
               "limits": {"TWELVE_DATA": [7, 60], "FINNHUB": [50, 60], "ALPACA": [100, 60], "MASSIVE": [4, 60],
                          "ALPHA_VANTAGE": [20, 86400], "TIINGO": [40, 3600], "EODHD": [18, 86400], "YAHOO": [30, 60]}},
    "intelligence": {
        "stale_event_hours": 72, "trade_event_max_age_hours": 24, "thesis_entry_min_probability": 0.70,
        "allow_thesis_entries": True, "contradiction_hours": 6, "company_refresh_hours": 72,
        "regime_refresh_hours": 6, "learning_min_samples": 20, "llm_enabled": False,
        "llm_model": "claude-haiku-5-5", "llm_max_per_cycle": 5, "gdelt_enabled": True,
        "finnhub_company_news_max": 15, "max_company_refresh_per_cycle": 6,
    },
}


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_env_file(path: str) -> int:
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
                if k and v and k not in os.environ:
                    os.environ[k] = v
                    n += 1
    except OSError:
        pass
    return n


def load_config(path: str = "") -> dict:
    path = path or os.path.join(CONFIG_DIR, "config.json")
    user: dict = {}
    try:
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
    except (OSError, ValueError):
        pass
    cfg = deep_merge(DEFAULTS, user)
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
