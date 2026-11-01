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
