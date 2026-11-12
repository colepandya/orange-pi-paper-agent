# Dashboard settings, API keys and service control

Open the dashboard -> **Settings**. There is **no password by default** (it is a paper bot on your home network); you can switch a password on later under Settings -> Security, alerts & storage -> "Require a password".

**Every setting is on one page**, in sections, with a single **Save** button for all of them. The left-hand menu jumps to a section of that page; the strip at the top shows the key switches in plain language and (collapsed) everything you have changed from the defaults; a search box finds a setting by any word in its name or description. "Only this section" opens one section by itself with its own Save button (saving a section never touches another). A star marks the settings most people adjust. The API-key, service, check and reset boxes at the bottom each save on their own. The page uses no JavaScript (the dashboard's security policy allows none).

| Group | Section | What you can do |
|---|---|---|
| Trading | Position sizing & risk | Risk per trade, position and concentration limits, cash reserve, stress limits, drawdown and daily-loss pauses |
| Trading | Entry rules | Quality bar (expected value, confidence, entry score, reward:risk, liquidity), timing, how ideas are judged, avoiding doubling up |
| Trading | Safety checks before buying | Re-check against later news, opening gaps, market-wide shocks |
| Trading | Stops, targets & exits | Swing or day-trade style, holding time, profit targets, stops, break-even lock and trailing stop, earnings |
| Instruments | Short selling / Options / Related companies | Each instrument's switch, size, eligibility rules, costs and price sources |
| Intelligence | Reasoning engine / Reading the news | What the engine may do, the red team, ideas without a news trigger, learning, missed-trade analysis, article reading, SEC facts |
| Data & system | Prices & quotes / Schedule & Pi limits / Security, alerts & storage | Quote quality and refresh, how often the bot runs, Pi protection limits, password and hostnames, phone alerts, database retention |
| Connections | News & data feeds / API keys | Enable/disable each feed and its polling interval; API keys (masked; paste to replace; tick to remove) |
| Operations | Services & timers / Checks & tests / Change history & reset | Restart services, run a cycle, update or backup now; health checks; what changed, when and by whom; reset to defaults |
| Retired | Language-model analyst | The old LLM settings, kept so nothing is lost; the bot no longer calls a language model, so they have no effect on trading |
| Intelligence | Company knowledge | The company knowledge base (Build 45/46): on/off, what each part may do (every part starts on **observe**: it records and measures and changes no trade), relationship reach, SEC refresh limits and pacing, how fresh is fresh, filing retention, weekly facts |

Every value is validated (type, range, and cross-rules such as *max position <= max per company* and *max invested + min cash <= 100%*) before it is saved.

## Where things live
Precedence: built-in defaults < `/etc/paper-agent/config.json` < **dashboard settings** (`/var/lib/paper-agent/settings.json`). Only values you changed
are stored, so untouched settings keep following the defaults of new releases. API keys saved in the dashboard live in `/var/lib/paper-agent/secrets.env`
(owner-only) and override keys from `/etc/paper-agent/market-data.env`. Updates never touch either file. "Reset to defaults" removes the settings file and keeps keys.

## Security model (read this)
* **Default: no password.** Anyone who can reach the dashboard on your network can open Settings, replace API keys and restart the bot's services. That is a reasonable trade-off for a paper bot
  on a home LAN. **Never expose port 8088 to the internet** (no port-forwarding); use a VPN for remote access.
* Even without a password: only private-network addresses are served POSTs, every form carries a CSRF token, cross-site requests (a malicious web page you visit trying to change
  your settings) are refused using the browser's Origin / Sec-Fetch-Site headers, and the request's Host must look like your LAN (private IP, `localhost`, a single-label name such as
  `orangepizero3`, `*.local`, `*.lan`, `*.home.arpa`...). That last rule defeats DNS-rebinding attacks. If you use some other hostname or a proxy, add it under Settings -> Security, alerts & storage -> Allowed hostnames.
* **Optional password:** Settings -> Security, alerts & storage -> "Require a password to use Settings". On the next visit you create it (min. 10 chars; salted PBKDF2 hash; 8-hour HttpOnly session cookie;
  5 wrong attempts lock that address for 2 minutes). Traffic is still plain HTTP, so do not reuse a password.
* The dashboard itself runs unprivileged. Restarts are done by a small root helper (`paper-agent-control.path`) that accepts only a fixed list of `paper-agent-*` units and integer timer
  intervals within safe ranges; nothing from a request is ever passed to a shell. If the helper is stopped, the Settings page says so.
* API keys are never rendered back (only "set, ends ...abcd"), never put in URLs, and never written to the audit log (only which keys changed).

## Recovery
* Forgot a password you switched on: `sudo rm /var/lib/paper-agent/admin.json` (then visit Settings and create a new one), or turn it off entirely with `sudo rm /var/lib/paper-agent/settings.json` (this resets all dashboard-made settings; keys are kept).


## Company knowledge (Build 45/46)
Every mode below starts on **observe**. Observe records and measures what a part WOULD do; only `kb.relationship_mode = active` lets first-hop relationship effects affect a trade (materiality, state and regime have no active mode until they have been measured).

| Setting | Default | Meaning |
|---|---|---|
| kb.enabled | on | Switch the whole knowledge layer off (the bot then behaves as without it) |
| kb.relationship_mode / expiry_mode / materiality_mode / state_mode / regime_mode | observe | off, observe, (active only where implemented) |
| kb.max_hops, kb.propagate_events_per_cycle | 3, 40 | How far relationship effects are worked out; events processed per cycle |
| kb.refresh_companies_per_cycle, kb.sec_requests_per_second, kb.max_network_seconds, kb.max_db_seconds | 12, 5, 20, 5 | The per-cycle SEC refresh budget; a cycle is never held up by it |
| kb.retry_delay_hours, kb.failure_backoff_max_hours | 6, 72 | Pause after a refusal (403/429) or a failure |
| kb.max_data_age_days, kb.financial_max_age_days | 90, 150 | After this, a fact is marked stale, never silently used as current |
| kb.filing_window_days, kb.filing_rows_per_company | 120, 60 | Filing events kept (material ones are never pruned) |
| kb.weekly_facts_companies | 60 | SEC financial facts fetched in the weekly job (0 = off) |

## Adding to a stock you already hold (Build 47)
| Setting | Default | Meaning |
|---|---|---|
| risk.allow_add_to_position | off | Off: a second position in a stock you hold is always skipped. On: allowed, subject to the two protections below. |
| risk.add_cooldown_hours | 6 | A second entry within this many hours of the first is skipped as "the same bet bought twice" (ideas queued overnight all fill at the open). 0 = off. |
| risk.add_only_when_winning | on | Never add to a position that is currently losing (a short loses when the price rises). |

## Shadow book (Build 49)
| Setting | Default | Meaning |
|---|---|---|
| shadow.enabled | on | Follow every idea to a costed outcome. Observation only. |
| shadow.cost_bps | 30 | Cost taken off every shadow outcome, round trip. |
| shadow.min_abs_direction | 0.15 | Ideas with a weaker direction carry no view and are not followed. |
| shadow.measure_per_cycle / shadow.max_seconds | 300 / 3 | Work a cycle may spend: both capture and measurement stop at the time limit. |

## Put trial and opening delay (Build 50)
| Setting | Default | Meaning |
|---|---|---|
| risk.open_delay_minutes | 30 | Wait this long after the open before entering (was 10). |
| instruments.options_trial_enabled | on | Smaller puts on ideas slightly below the normal option minimum, to gather evidence faster. |
| instruments.options_trial_min_ev | 2% | The weakest edge the trial accepts (and never more than two points below the normal minimum). |
| instruments.options_trial_per_trade_pct | 0.6% | Premium per trial trade, at most. |
| instruments.options_trial_puts_only | on | Keep the trial to puts. |
| instruments.options_trial_breaker_trades / _pct | 8 / 0.5% | The trial pauses when its last N closed trades lost more than this share of equity. |
| instruments.options_rescue_enabled | on | A bearish idea the stock rules turned down ONLY for stock-position reasons (swing reward:risk, an edge a little under the stock minimum) gets a second look as a put. |
| instruments.options_rescue_per_trade_pct | 0.6% | Premium per rescued put, at most. |
| instruments.options_min_option_ev | 10% | Trial and rescued puts: the contract itself must be expected to return at least this on its premium (after decay and the exit spread). |
| instruments.options_min_adv_usd | $50M | Options only on stocks trading at least this a day (was $100M). |
| instruments.options_reversal_enabled | OFF | A bullish idea whose forecast is a fall becomes a half-size PUT. Off until the Intelligence tab shows the model's big forecasts coming true over several weeks. |
| instruments.options_reversal_min_move | 8% | The forecast fall must be at least this, net of costs (the 4-8% band showed nothing in the first week). |
| instruments.options_reversal_haircut | 0.5 | Believe this share of the predicted move (the overshoot model is unvalidated). |
| instruments.options_reversal_per_trade_pct | 0.6% | Premium per reversal put, at most. |
| instruments.options_reversal_cooldown_hours | 24 | Wait this long after a reversal put closes on the same stock. |

## More trades, kept small (Build 53)
| Setting | Default | Meaning |
|---|---|---|
| risk.min_ev_net | 0 (was 0.6%) | Minimum expected value (net of costs) to trade at all. Negative-EV ideas are still rejected. |
| strategy.min_reward_risk | 0.4 (was 1.0) | Minimum target-to-stop ratio to trade at all. |
| risk.cash_hurdle_margin | 0.1% (was a fixed 0.2%) | Margin above cash an idea must beat, on top of the cash rate over its horizon. |
| risk.full_size_min_ev | 0.6% | An idea below this expected value is traded SMALL. |
| risk.full_size_min_rr | 1.0 | An idea below this reward:risk is traded SMALL. |
| risk.low_edge_max_pct | 0.5% | Size of a small (low-edge) trade, at most. 0 switches the cap off. |
| portfolio.daily_trade_target | 10 | Shown on the Overview. A target, not a quota. |

## Research lab (Build 54)
| Setting | Default | Meaning |
|---|---|---|
| history.enabled | on | Backfill long daily price history for the research lab. Observation only. |
| history.years | 5 | Years of history to keep (1-10). About 40 MB for 400 stocks at 5. |
| history.symbols_per_cycle | 3 | Stocks fetched per cycle. |
| history.max_seconds | 12 | Seconds a cycle may spend fetching history. |
| research.enabled | on | Run the studies weekly while the US market is closed. |
| research.rerun_days | 7 | Repeat the studies this often. |
| research.max_seconds | 60 | Seconds a study may take inside a cycle. |

## Risk engine and sell-the-news (Build 55)
| Setting | Default | Meaning |
|---|---|---|
| risk.position_slots | 20 | No new position may take more than the invested limit divided by this. 0 = off. |
| risk.min_position_pct | 0.5% (was 1%) | Smallest position worth opening. |
| instruments.options_reversal_enabled | on (was off) | Good news on a stock that already ran up, which the model expects to fall, becomes a half-size put. |
| instruments.options_reversal_min_move | 3% (was 8%) | The forecast fall must be at least this, net of costs. |
| instruments.options_reversal_haircut | 0.6 (was 0.5) | Share of the predicted move believed (realised/predicted was 0.63). |
| risk.position_slots | 40 (was 20) | No new position may take more than the invested limit divided by this. |
| portfolio.daily_trade_target | 30 (was 10) | Shown on the Overview. A target, not a quota. |
| risk.low_edge_breaker_trades / _pct | 30 / 0.5% | The small-trade tier pauses when its last N closed trades lost more than this share of equity. 0 = never pause. |
