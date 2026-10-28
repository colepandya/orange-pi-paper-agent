# Dashboard settings, API keys and service control

Open the dashboard -> **Settings**. There is **no password by default** (it is a paper bot on your home network); you can switch a password on later under Settings -> System -> "Require a password".

| Section | What you can do |
|---|---|
| Risk limits / Execution | Position, company, theme and gross limits, cash reserve, risk per trade, stress-loss cap, drawdown and daily-loss pauses, quote quorum and freshness, slippage, entry score |
| Refresh times | Cycle, guard and update intervals (applied to the systemd timers for you), regime / company / contradiction refresh, staleness windows |
| LLM analyst | On/off, daily and monthly dollar caps, call caps, provider + model + price per tier (Anthropic or any OpenAI-compatible provider), red team, causal-order limit |
| News & data feeds | Enable/disable each feed and its polling interval |
| API keys | Anthropic, GitHub Models, Twelve Data, Finnhub, Alpaca, Massive, Alpha Vantage, Tiingo, EODHD (masked; paste to replace; tick to remove) |
| Services | Restart each service, run a cycle / update / backup now, restart everything |
| Test connections | One-click LLM test (cheap or deep tier) and a full health check |

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
  `orangepizero3`, `*.local`, `*.lan`, `*.home.arpa`...). That last rule defeats DNS-rebinding attacks. If you use some other hostname or a proxy, add it under Settings -> System -> Allowed hostnames.
* **Optional password:** Settings -> System -> "Require a password to use Settings". On the next visit you create it (min. 10 chars; salted PBKDF2 hash; 8-hour HttpOnly session cookie;
  5 wrong attempts lock that address for 2 minutes). Traffic is still plain HTTP, so do not reuse a password.
* The dashboard itself runs unprivileged. Restarts are done by a small root helper (`paper-agent-control.path`) that accepts only a fixed list of `paper-agent-*` units and integer timer
  intervals within safe ranges; nothing from a request is ever passed to a shell. If the helper is stopped, the Settings page says so.
* API keys are never rendered back (only "set, ends ...abcd"), never put in URLs, and never written to the audit log (only which keys changed).

## Recovery
* Forgot a password you switched on: `sudo rm /var/lib/paper-agent/admin.json` (then visit Settings and create a new one), or turn it off entirely with `sudo rm /var/lib/paper-agent/settings.json` (this resets all dashboard-made settings; keys are kept).
