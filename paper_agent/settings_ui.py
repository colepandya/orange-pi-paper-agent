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


def _input(f: S.Field, cfg: dict, base: dict, raw: Dict[str, str]) -> str:
    cur = S.display_value(f, cfg)
    val = raw.get(f.name, cur)
    try:
        dflt = S.display_value(f, base)
    except KeyError:
        dflt = ""
    if f.kind == "bool":
        chk = " checked" if val in ("1", "true", "on") else ""
        return '<input type=hidden name="%s" value=0><label><input type=checkbox name="%s" value=1%s> enabled</label>' % (E(f.name), E(f.name), chk)
    unit = " %" if f.kind == "pct" else ""
    cls = "wide" if f.kind in ("email", "hostlist", "topic") else "num"
    return '<input type=text class=%s name="%s" value="%s" inputmode=decimal>%s' % (cls, E(f.name), E(val), unit)


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


def _row(f: S.Field, cfg: dict, base: dict, raw: Dict[str, str], meta: Optional[Dict[str, dict]] = None) -> str:
    cur, dflt = S.display_value(f, cfg), S.display_value(f, base)
    chg = '<span class=chg> changed (default %s%s)</span>' % (E(dflt if f.kind != "bool" else ("on" if dflt == "1" else "off")), " %" if f.kind == "pct" else "") if cur != dflt else ""
    rng = ""
    if f.lo is not None and f.kind in ("int", "float", "pct"):
        sc = 100 if f.kind == "pct" else 1
        rng = " range %g&ndash;%g%s" % (f.lo * sc, f.hi * sc, "%" if f.kind == "pct" else "")
    note = " <b>(restart needed)</b>" if f.restart == "dashboard" else ""
    return "<tr><td>%s<br><span class=help>%s%s%s</span>%s</td><td>%s%s</td></tr>" % (E(f.label), E(f.help), rng, note, _when((meta or {}).get(f.path)), _input(f, cfg, base, raw), chg)


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


def page_settings(cfg: dict, base: dict, ctx: dict, sources: List[dict], units: Dict[str, str], secret_status: Dict[str, str], health: Optional[Dict[str, dict]] = None) -> str:
    raw = ctx.get("raw") or {}
    csrf = ctx.get("csrf", "")
    out = [flash_html(ctx.get("flash"), ctx.get("errors"), ctx.get("warnings")), pending_panel(ctx.get("pending") or [], csrf)]
    out.append("<form method=post action=/settings/save>%s" % csrf_input(csrf))
    for g in S.GROUP_ORDER:
        out.append("<h2>%s</h2><div class=wrap><table>" % E(g))
        for f in [x for x in S.FIELDS if x.group == g]:
            out.append(_row(f, cfg, base, raw, ctx.get("meta")))
        if g == "LLM analyst":
            out.append(_tiers(cfg, raw))
        out.append("</table></div>")
    so = cfg.get("source_overrides", {})
    out.append("<h2>News & data feeds</h2><p class=help>Disable a feed or change how often it is polled (seconds, 60-86400). Failing feeds already back off automatically.</p><div class=wrap><table><tr><th>Feed</th><th>Type</th><th>Status</th><th>Enabled</th><th>Poll every (s)</th></tr>")
    for s in sources:
        o = so.get(s["id"], {})
        en = o.get("enabled", True)
        iv = raw.get("src.%s.interval" % s["id"], o.get("interval", s.get("interval", 600)))
        out.append("<tr><td>%s</td><td>%s</td><td>%s</td><td><input type=hidden name='src.%s.enabled' value=0><input type=checkbox name='src.%s.enabled' value=1%s></td><td><input type=text class=num name='src.%s.interval' value='%s'></td></tr>"
                   % (E(s["name"]), E(s["type"]), _feed_status((health or {}).get(s["id"])), E(s["id"]), E(s["id"]), " checked" if en else "", E(s["id"]), E(iv)))
    out.append('</table></div><div class=savebar><button class=primary>Save settings</button> <span class=help>Most changes apply on the next cycle; schedule changes are applied to the system timers automatically.</span></div></form>')
    dead = dead_feed_ids(sources, cfg, health or {})
    if dead:
        out.append("<div class=card2><form method=post action=/settings/disable-dead>%s<b>%d feed(s) are permanently gone</b> (HTTP 404/410 three or more times): %s. <button class=sec>Disable these %d feeds</button></form></div>" % (csrf_input(csrf), len(dead), E(", ".join(dead)), len(dead)))
    # ---- API keys
    out.append("<h2>API keys</h2><form method=post action=/settings/secrets>%s<div class=card2 style='margin-top:0'><p class=help>Keys are stored on the Pi (owner-only file) and are never shown again, only whether they are set. Leave a box empty to keep the current key.</p><table><tr><th>Key</th><th>Status</th><th>Replace with</th><th>Remove</th></tr>" % csrf_input(csrf))
    for name, label, help_ in S.SECRETS:
        updated = _when((ctx.get("meta") or {}).get("secret." + name)).replace("changed ", "updated ")
        out.append("<tr><td>%s<br><span class=help>%s</span></td><td>%s%s</td><td><input type=password class=wide name='secret.%s' autocomplete=off placeholder='paste new key'></td><td><input type=checkbox name='clear.%s' value=1></td></tr>"
                   % (E(label), E(help_), E(secret_status.get(name, "not set")), updated, E(name), E(name)))
    out.append("</table><p><button class=primary>Save keys</button></p></div></form>")
    # ---- services
    out.append("<h2>Services</h2>")
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
        verb = "restart"
        out.append("<tr><td>%s</td><td class=%s>%s</td><td><form method=post action=/service style='display:inline'>%s<input type=hidden name=action value=%s><input type=hidden name=unit value='%s'><button class=sec>Restart</button></form></td></tr>"
                   % (E(u), "ok" if st == "active" else "warn", E(st), csrf_input(csrf), verb, E(u)))
    out.append("</table><p>")
    for label, act, unit in (("Run a cycle now", "start", "paper-agent-cycle.service"), ("Check for updates now", "start", "paper-agent-update.service"), ("Back up now", "start", "paper-agent-backup.service"),
                             ("Refresh company list now", "start", "paper-agent-universe.service"), ("Restart guard + cycle + dashboard", "restart_all", "")):
        out.append("<form method=post action=/service style='display:inline'>%s<input type=hidden name=action value=%s><input type=hidden name=unit value='%s'><button class=sec>%s</button></form> " % (csrf_input(csrf), act, E(unit), label))
    res = ctx.get("control")
    if res:
        out.append("</p><p class=help>Last request: <b>%s</b> &mdash; %s &mdash; %s</p>" % (E(res.get("action")), "OK" if res.get("ok") else "FAILED", E(res.get("output"))))
    else:
        out.append("</p>")
    out.append("<p class=help>Restarts go through a small root helper that only accepts a fixed list of paper-agent actions. Jobs (update, backup, company list) are started on demand and are refused when the Pi is short of memory.</p></div>")
    # ---- tests
    out.append("<h2>Test connections</h2><div class=card2 style='margin-top:0'><p>")
    for label, path in (("Test LLM (cheap tier)", "/test/llm?tier=triage"), ("Test LLM (deep tier)", "/test/llm?tier=analyst"), ("Pre-open checklist", "/test/preflight"), ("Run full health check", "/test/doctor")):
        out.append("<form method=post action='%s' style='display:inline'>%s<button class=sec>%s</button></form> " % (path, csrf_input(csrf), label))
    out.append("</p><p class=help>The health check contacts every feed and quote provider; it can take up to a minute. An LLM test makes one real (tiny) API call.</p>")
    if ctx.get("test_output"):
        out.append("<pre>%s</pre>" % E(ctx["test_output"]))
    out.append("</div>")
    out.append(changes_panel(ctx.get("meta") or {}))
    out.append("<h2>Reset</h2><div class=card2 style='margin-top:0'><form method=post action=/settings/reset>%s<label><input type=checkbox name=confirm value=1> I want to discard ALL dashboard-made setting changes (API keys are kept)</label> <button class=danger>Reset to defaults</button></form></div>" % csrf_input(csrf))
    return "".join(out)
