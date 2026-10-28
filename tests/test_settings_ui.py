"""Settings area: schema/validation, layering, secrets, auth, CSRF, privileged control worker. Real HTTP server, no mocks of the handler."""
import http.client
import json
import os
import re
import stat
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
from paper_agent import auth, config, control, dashboard, db, pipeline, settings as S, sources  # noqa: E402

SECRET = "sk-ant-TESTSECRET-abcdef12345"
iso_now = "2026-10-28T00:00:00+00:00"


def read(path):
    with open(path) as f:
        return f.read()


def write(path, text):
    with open(path, "w") as f:
        f.write(text)


def jload(path):
    with open(path) as f:
        return json.load(f)


auth.ITERATIONS = 1000        # tests only; production uses 150,000


class FakeClock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class TestSettingsLogic(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.base = config.load_config("/nonexistent", with_overrides=False)

    def test_every_schema_path_exists_and_has_sane_bounds(self):
        for f in S.FIELDS:
            v = S.get_path(self.base, f.path)
            if f.lo is not None:
                self.assertGreaterEqual(v, f.lo - 1e-9, f.path)
                self.assertLessEqual(v, f.hi + 1e-9, f.path)

    def test_overrides_store_only_changes_and_layer_over_base(self):
        new, err, _ = S.apply_form({"f.risk.max_position_pct": "8", "f.schedule.cycle_minutes": "15"}, self.base, self.base, [])
        self.assertEqual(err, [])
        ov = S.overrides_for(new, self.base)
        self.assertEqual(ov, {"risk": {"max_position_pct": 0.08}, "schedule": {"cycle_minutes": 15}})
        p = os.path.join(self.d, "settings.json")
        S.save_overrides(ov, p)
        eff = config.load_config("/nonexistent", settings_path=p)
        self.assertEqual(eff["risk"]["max_position_pct"], 0.08)
        self.assertEqual(eff["risk"]["max_issuer_pct"], self.base["risk"]["max_issuer_pct"])     # untouched values still follow defaults
        S.reset_overrides(p)
        self.assertEqual(config.load_config("/nonexistent", settings_path=p)["risk"]["max_position_pct"], self.base["risk"]["max_position_pct"])

    def test_rejects_bad_types_ranges_and_cross_field_conflicts(self):
        for k, v in (("f.risk.max_position_pct", "abc"), ("f.risk.max_position_pct", "90"), ("f.risk.min_cash_pct", "-5"), ("f.risk.max_new_per_cycle", "2.5"),
                     ("f.schedule.cycle_minutes", "1"), ("f.runtime.contact_email", "not-an-email"), ("f.risk.max_position_pct", ""), ("f.risk.max_position_pct", "nan")):
            _, err, _ = S.apply_form({k: v}, self.base, self.base, [])
            self.assertTrue(err, (k, v))
        for form in ({"f.risk.max_position_pct": "20", "f.risk.max_issuer_pct": "10"}, {"f.risk.max_gross_pct": "90", "f.risk.min_cash_pct": "30"},
                     {"f.risk.daily_loss_pause_pct": "15", "f.risk.drawdown_pause_pct": "10"}, {"f.intelligence.llm_daily_budget_usd": "10", "f.intelligence.llm_monthly_budget_usd": "5"},
                     {"f.quotes.entry_max_age_seconds": "300", "f.quotes.max_age_seconds": "100"}):
            _, err, _ = S.apply_form(form, self.base, self.base, [])
            self.assertTrue(err, form)

    def test_unknown_model_requires_prices_so_the_budget_stays_meaningful(self):
        _, err, _ = S.apply_form({"tier.triage.provider": "github", "tier.triage.model": "openai/some-new-model"}, self.base, self.base, [])
        self.assertTrue(any("price" in e for e in err))
        new, err, _ = S.apply_form({"tier.triage.provider": "github", "tier.triage.model": "openai/some-new-model", "tier.triage.price_in": "0", "tier.triage.price_out": "0"}, self.base, self.base, [])
        self.assertEqual((err, new["intelligence"]["llm_tiers"]["triage"]["provider"]), ([], "github"))
        _, err, _ = S.apply_form({"tier.triage.provider": "evilcorp", "tier.triage.model": "x"}, self.base, self.base, [])
        self.assertTrue(err)

    def test_feed_overrides_reach_the_collector(self):
        defaults = {"bbc_world": 600, "cnbc_top": 600, "npr_news": 900}
        new, err, _ = S.apply_form({"src.bbc_world.enabled": "0", "src.bbc_world.interval": "1800", "src.cnbc_top.interval": "5"}, self.base, self.base, defaults)
        self.assertEqual(len([e for e in err if "cnbc_top" in e]), 1)       # 5 s is below the 60 s floor
        self.assertEqual(new["source_overrides"]["bbc_world"], {"enabled": False, "interval": 1800})
        srcs = pipeline.load_sources(new)
        bbc = next(s for s in srcs if s["id"] == "bbc_world") if any(s["id"] == "bbc_world" for s in srcs) else None
        if bbc:      # sources.json lives in /etc on the Pi; in the sandbox use the repo copy
            self.assertFalse(bbc["enabled"])

    def test_only_feeds_you_actually_changed_are_stored(self):
        defaults = {"bbc_world": 600, "cnbc_top": 600, "npr_news": 900}
        form = {}
        for sid, iv in defaults.items():                     # the browser submits EVERY feed, at its current value
            form["src.%s.enabled" % sid] = "1"
            form["src.%s.interval" % sid] = str(iv)
        new, err, _ = S.apply_form(form, self.base, self.base, defaults)
        self.assertEqual((err, new["source_overrides"], S.overrides_for(new, self.base)), ([], {}, {}))
        form["src.npr_news.interval"] = "3600"
        form["src.cnbc_top.enabled"] = "0"
        new, _, _ = S.apply_form(form, self.base, self.base, defaults)
        self.assertEqual(new["source_overrides"], {"npr_news": {"interval": 3600}, "cnbc_top": {"enabled": False}})
        form["src.cnbc_top.enabled"], form["src.npr_news.interval"] = "1", "900"       # change back to defaults -> override disappears
        again, _, _ = S.apply_form(form, new, self.base, defaults)
        self.assertEqual(again["source_overrides"], {})

    def test_secrets_masked_validated_and_stored_privately(self):
        p, env = os.path.join(self.d, "secrets.env"), {}
        changed, err = S.apply_secret_form({"secret.ANTHROPIC_API_KEY": SECRET}, p, env)
        self.assertEqual((changed, err), (["ANTHROPIC_API_KEY"], []))
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        self.assertEqual(env["ANTHROPIC_API_KEY"], SECRET)
        st = S.secret_status(p)["ANTHROPIC_API_KEY"]
        self.assertIn("2345", st)
        self.assertNotIn(SECRET, st)
        self.assertEqual(S.apply_secret_form({"secret.ANTHROPIC_API_KEY": ""}, p, env), ([], []))      # blank = unchanged
        self.assertEqual(S.read_secrets(p)["ANTHROPIC_API_KEY"], SECRET)
        for bad in ("short", "has space in it 12345", 'quote"inside12345', "x" * 300, "line\nbreak12345"):
            ch, er = S.apply_secret_form({"secret.FINNHUB_API_KEY": bad}, p, env)
            self.assertTrue(er and not ch, bad)
        self.assertNotIn("FINNHUB_API_KEY", S.read_secrets(p))
        S.apply_secret_form({"clear.ANTHROPIC_API_KEY": "1"}, p, env)
        self.assertNotIn("ANTHROPIC_API_KEY", S.read_secrets(p))
        self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_dashboard_keys_override_file_and_systemd_values(self):
        p = os.path.join(self.d, "secrets.env")
        S.apply_secret_form({"secret.FINNHUB_API_KEY": "fromdashboard12345"}, p, {})
        envf = os.path.join(self.d, "etc.env")
        write(envf, "FINNHUB_API_KEY=fromfile\n")
        os.environ.pop("FINNHUB_API_KEY", None)
        config.load_env_file(envf)
        self.assertEqual(os.environ["FINNHUB_API_KEY"], "fromfile")
        config.load_env_file(p, override=True)
        self.assertEqual(os.environ["FINNHUB_API_KEY"], "fromdashboard12345")
        os.environ.pop("FINNHUB_API_KEY", None)


class TestAuth(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.a = auth.Auth(os.path.join(tempfile.mkdtemp(), "admin.json"), clock=self.clock)

    def test_password_hashing_and_policy(self):
        self.assertFalse(self.a.configured())
        with self.assertRaises(ValueError):
            self.a.set_password("short")
        self.a.set_password("correct horse battery")
        self.assertTrue(self.a.configured())
        self.assertTrue(self.a.verify("correct horse battery"))
        self.assertFalse(self.a.verify("wrong"))
        raw = read(self.a.path)
        self.assertNotIn("correct horse", raw)
        self.assertEqual(stat.S_IMODE(os.stat(self.a.path).st_mode), 0o600)

    def test_lockout_then_recovery(self):
        self.a.set_password("correct horse battery")
        for _ in range(auth.MAX_FAILS):
            self.assertIsNone(self.a.login("10.0.0.9", "nope"))
        self.assertIsNone(self.a.login("10.0.0.9", "correct horse battery"))      # locked even with the right password
        self.assertIsNotNone(self.a.login("10.0.0.10", "correct horse battery"))  # other clients unaffected
        self.clock.t += auth.LOCK_SECONDS + 1
        self.assertIsNotNone(self.a.login("10.0.0.9", "correct horse battery"))

    def test_sessions_expire_and_csrf_is_per_session(self):
        tok, csrf = self.a.new_session("1.1.1.1")
        s = self.a.session(tok)
        self.assertTrue(auth.Auth.csrf_ok(s, csrf))
        self.assertFalse(auth.Auth.csrf_ok(s, "forged"))
        self.assertFalse(auth.Auth.csrf_ok(s, None))
        self.clock.t += auth.SESSION_SECONDS + 1
        self.assertIsNone(self.a.session(tok))
        self.assertIsNone(self.a.session("not-a-token"))


class TestControlWorker(unittest.TestCase):
    def setUp(self):
        self.d, self.etc = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.calls = []

        def run(cmd, **kw):
            self.calls.append(cmd)
            return mock.Mock(returncode=0, stdout="", stderr="")
        self.run = run

    def req(self, action, args=None):
        control.send_request(action, args, self.d)
        return control.run_once(self.d, self.run, self.etc)

    def test_whitelisted_restart_runs_exactly_one_systemctl_command(self):
        r = self.req("restart", {"unit": "paper-agent-cycle.timer"})
        self.assertTrue(r["ok"])
        self.assertEqual(self.calls, [["systemctl", "restart", "--no-block", "paper-agent-cycle.timer"]])
        self.assertEqual(control.last_result(self.d)["action"], "restart")
        self.assertFalse(os.path.exists(os.path.join(self.d, "request.json")))      # consumed

    def test_everything_outside_the_whitelist_is_refused_without_executing(self):
        for unit in ("ssh.service", "paper-agent-cycle.timer; rm -rf /", "../../etc/passwd", "paper-agent-cycle.service", "", None, 5, ["paper-agent-guard.timer"]):
            r = self.req("restart", {"unit": unit})
            self.assertFalse(r["ok"], unit)
        for action in ("reboot", "shell", "exec", "set_password", "", None):
            self.assertFalse(self.req(action, {"cmd": "id"})["ok"], action)
        self.assertEqual(self.calls, [])

    def test_start_whitelist_differs_from_restart_whitelist(self):
        self.assertTrue(self.req("start", {"unit": "paper-agent-cycle.service"})["ok"])
        self.assertFalse(self.req("start", {"unit": "paper-agent-dashboard.service"})["ok"])

    def test_schedule_writes_dropins_only_from_validated_integers(self):
        r = self.req("set_schedule", {"cycle_minutes": 15, "guard_minutes": 3, "update_hours": 6})
        self.assertTrue(r["ok"])
        self.assertEqual(read(os.path.join(self.etc, "paper-agent-cycle.timer.d", "schedule.conf")), "[Timer]\nOnUnitInactiveSec=\nOnUnitInactiveSec=15min\n")
        self.assertEqual(read(os.path.join(self.etc, "paper-agent-update.timer.d", "schedule.conf")), "[Timer]\nOnUnitActiveSec=\nOnUnitActiveSec=6h\n")
        self.assertIn(["systemctl", "daemon-reload"], self.calls)
        etc2 = tempfile.mkdtemp()
        for bad in ({"cycle_minutes": 1}, {"cycle_minutes": 9999}, {"cycle_minutes": "5\nExecStartPre=/bin/evil"}, {"guard_minutes": "x"}, {"update_hours": 0}, {"cycle_minutes": None}):
            control.send_request("set_schedule", bad, self.d)
            res = control.run_once(self.d, self.run, etc2)
            self.assertFalse(res["ok"], bad)
        self.assertEqual(os.listdir(etc2), [])        # nothing written for any rejected value

    def test_invalid_oversized_and_symlinked_requests_are_rejected(self):
        p = os.path.join(self.d, "request.json")
        write(p, "{not json")
        self.assertFalse(control.run_once(self.d, self.run, self.etc)["ok"])
        write(p, json.dumps({"action": "restart", "args": {"unit": "paper-agent-cycle.timer"}, "pad": "x" * 6000}))
        self.assertFalse(control.run_once(self.d, self.run, self.etc)["ok"])
        target = os.path.join(self.d, "elsewhere.json")
        write(target, json.dumps({"action": "restart_all", "args": {}}))
        os.symlink(target, p)
        self.assertFalse(control.run_once(self.d, self.run, self.etc)["ok"])
        self.assertEqual(self.calls, [])

    def test_result_file_symlink_is_replaced_never_followed(self):
        victim = os.path.join(self.d, "victim.txt")
        write(victim, "PRECIOUS")
        os.symlink(victim, os.path.join(self.d, "result.json"))
        self.req("restart", {"unit": "paper-agent-guard.timer"})
        self.assertEqual(read(victim), "PRECIOUS")
        self.assertFalse(os.path.islink(os.path.join(self.d, "result.json")))
        self.assertTrue(control.last_result(self.d)["ok"])

    def test_no_request_is_a_noop(self):
        self.assertIsNone(control.run_once(self.d, self.run, self.etc))


class HTTPHarness:
    """Real HTTP server per test class. HERMETIC: every config/state path points into a temp dir, so results never depend on the machine's /etc or /var/lib."""
    REQUIRE = None          # None = use the shipped default (no password); True/False forces runtime.require_password

    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.dbpath = os.path.join(self.d, "t.db")
        db.open_db(self.dbpath).close()
        self._old = (config.SETTINGS_PATH, config.SECRETS_PATH, config.CONTROL_DIR, config.ADMIN_PATH, config.CONFIG_DIR, pipeline.CONFIG_DIR)
        config.SETTINGS_PATH, config.SECRETS_PATH = os.path.join(self.d, "settings.json"), os.path.join(self.d, "secrets.env")
        config.CONTROL_DIR, config.ADMIN_PATH = os.path.join(self.d, "control"), os.path.join(self.d, "admin.json")
        config.CONFIG_DIR = pipeline.CONFIG_DIR = os.path.join(self.d, "etc")        # no /etc/paper-agent/config.json, whatever the host has
        self.pause = os.path.join(self.d, "PAUSE")
        self._runtime_patch = mock.patch.dict(config.DEFAULTS["runtime"], {"require_password": self.REQUIRE} if self.REQUIRE is not None else {})   # change the DEFAULT, like a different release would
        self._runtime_patch.start()

        def cfg_fn():
            c = config.load_config()                                                  # defaults + (temp) settings overrides only
            c["runtime"]["pause_file"] = self.pause
            return c
        dashboard.Handler.db_path, dashboard.Handler.auth = self.dbpath, auth.Auth(config.ADMIN_PATH)
        dashboard.Handler.control_dir, dashboard.Handler.get_cfg = config.CONTROL_DIR, staticmethod(cfg_fn)

    def tearDown(self):
        self._runtime_patch.stop()
        (config.SETTINGS_PATH, config.SECRETS_PATH, config.CONTROL_DIR, config.ADMIN_PATH, config.CONFIG_DIR, pipeline.CONFIG_DIR) = self._old
        for k in S.SECRET_NAMES:
            os.environ.pop(k, None)

    # ---- tiny browser
    def req(self, method, path, form=None, cookie=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = urllib.parse.urlencode(form or {}) if method == "POST" else None
        h = {"Content-Type": "application/x-www-form-urlencoded"} if body is not None else {}
        if cookie:
            h["Cookie"] = cookie
        h.update(headers or {})
        c.request(method, path, body, h)
        r = c.getresponse()
        data = r.read().decode()
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        c.close()
        return r.status, hdrs, data

    def setup_admin(self, pw="correct horse battery"):
        st, h, _ = self.req("POST", "/setup", {"password": pw, "confirm": pw})
        return h["set-cookie"].split(";")[0]

    def csrf(self, cookie=None):
        _, _, page = self.req("GET", "/?tab=settings", cookie=cookie)
        return re.search(r'name=csrf value="([^"]+)"', page).group(1), page


class TestDashboardWithPassword(HTTPHarness, unittest.TestCase):
    """runtime.require_password = true: login, lockout, per-session CSRF."""
    REQUIRE = True

    # ---- tests
    def test_settings_require_login_and_first_visit_goes_to_setup(self):
        st, h, _ = self.req("GET", "/?tab=settings")
        self.assertEqual((st, h["location"]), (303, "/setup"))
        self.setup_admin()
        st, h, _ = self.req("GET", "/?tab=settings")
        self.assertEqual((st, h["location"]), (303, "/login"))
        st, h, _ = self.req("GET", "/setup")
        self.assertEqual(h["location"], "/login")          # setup cannot be re-run to take over

    def test_setup_validation_and_login_flow(self):
        self.assertEqual(self.req("POST", "/setup", {"password": "abcdefghijk", "confirm": "different12345"})[0], 400)
        self.assertEqual(self.req("POST", "/setup", {"password": "short", "confirm": "short"})[0], 400)
        cookie = self.setup_admin()
        st, _, page = self.req("GET", "/?tab=settings", cookie=cookie)
        self.assertEqual(st, 200)
        self.assertIn("Risk limits", page)
        self.assertEqual(self.req("POST", "/login", {"password": "wrong password"})[0], 401)
        st, h, _ = self.req("POST", "/login", {"password": "correct horse battery"})
        self.assertEqual(st, 303)
        self.assertIn("HttpOnly", h["set-cookie"])
        self.assertIn("SameSite=Strict", h["set-cookie"])

    def test_brute_force_is_locked_out(self):
        self.setup_admin()
        for _ in range(auth.MAX_FAILS):
            self.req("POST", "/login", {"password": "guess"})
        st, _, page = self.req("POST", "/login", {"password": "correct horse battery"})
        self.assertEqual(st, 401)
        self.assertIn("Too many attempts", page)

    def test_state_changing_posts_need_session_and_csrf(self):
        cookie = self.setup_admin()
        tok, _ = self.csrf(cookie)
        for path in ("/settings/save", "/settings/secrets", "/settings/reset", "/service", "/control/pause", "/test/doctor", "/test/llm"):
            self.assertEqual(self.req("POST", path, {"f.risk.max_position_pct": "5"})[0], 303, path)                       # no session -> redirected to login, nothing run
            self.assertEqual(self.req("POST", path, {"csrf": "forged"}, cookie)[0], 403, path)                               # session but forged token
            self.assertEqual(self.req("POST", path, {}, cookie)[0], 403, path)                                               # session but no token
        self.assertFalse(os.path.exists(config.SETTINGS_PATH))
        self.assertFalse(os.path.exists(self.pause))
        self.assertFalse(os.path.exists(os.path.join(config.CONTROL_DIR, "request.json")))

    def test_unrelated_config_differences_are_never_persisted(self):
        base = config.load_config("/nonexistent", with_overrides=False)
        new = json.loads(json.dumps(base))
        new["runtime"]["pause_file"] = "/tmp/elsewhere"
        new["risk"]["max_position_pct"] = 0.07
        new["totally_new_key"] = {"x": 1}
        self.assertEqual(S.overrides_for(new, base), {"risk": {"max_position_pct": 0.07}})

    def test_save_valid_settings_writes_only_changes_and_requests_schedule(self):
        cookie = self.setup_admin()
        tok, _ = self.csrf(cookie)
        st, h, _ = self.req("POST", "/settings/save", {"csrf": tok, "f.risk.max_position_pct": "8", "f.schedule.cycle_minutes": "15", "f.intelligence.llm_enabled": "1"}, cookie)
        self.assertEqual(st, 303)
        self.assertEqual(jload(config.SETTINGS_PATH), {"intelligence": {"llm_enabled": True}, "risk": {"max_position_pct": 0.08}, "schedule": {"cycle_minutes": 15}})
        self.assertEqual(jload(os.path.join(config.CONTROL_DIR, "request.json"))["args"]["cycle_minutes"], 15)
        _, page = self.csrf(cookie)
        self.assertIn('value="8"', page)
        self.assertIn("changed (default 10 %)", page)

    def test_invalid_settings_are_rejected_with_reasons_and_nothing_is_written(self):
        cookie = self.setup_admin()
        tok, _ = self.csrf(cookie)
        st, _, page = self.req("POST", "/settings/save", {"csrf": tok, "f.risk.max_position_pct": "20", "f.risk.max_issuer_pct": "10", "f.schedule.cycle_minutes": "1"}, cookie)
        self.assertEqual(st, 400)
        self.assertIn("must be between", page)                # cycle every 1 minute is below the 5 minute floor
        self.assertIn("cannot exceed", page)                  # a 20% position cannot fit under a 10% per-company cap
        self.assertIn('value="20"', page)                     # what the user typed is kept so they can fix it
        st, _, page = self.req("POST", "/settings/save", {"csrf": tok, "f.risk.max_position_pct": "90"}, cookie)
        self.assertEqual(st, 400)
        self.assertIn("must be between", page)
        self.assertFalse(os.path.exists(config.SETTINGS_PATH))

    def test_api_keys_are_masked_everywhere_and_stored_owner_only(self):
        cookie = self.setup_admin()
        tok, _ = self.csrf(cookie)
        st, h, _ = self.req("POST", "/settings/secrets", {"csrf": tok, "secret.ANTHROPIC_API_KEY": SECRET}, cookie)
        self.assertEqual(st, 303)
        self.assertNotIn(SECRET, h["location"])
        self.assertEqual(stat.S_IMODE(os.stat(config.SECRETS_PATH).st_mode), 0o600)
        self.assertEqual(os.environ["ANTHROPIC_API_KEY"], SECRET)                 # live in the running dashboard immediately
        _, _, page = self.req("GET", "/?tab=settings", cookie=cookie)
        self.assertNotIn(SECRET, page)
        self.assertIn("2345", page)
        for tab in dashboard.TABS:
            if tab != "settings":
                self.assertNotIn(SECRET, self.req("GET", "/?tab=" + tab)[2])
        st, _, page = self.req("POST", "/settings/secrets", {"csrf": tok, "secret.FINNHUB_API_KEY": 'bad "quote" key 12345'}, cookie)
        self.assertEqual(st, 400)
        self.assertNotIn("quote\" key", S.read_secrets().get("FINNHUB_API_KEY", ""))

    def test_service_requests_are_whitelisted_end_to_end(self):
        cookie = self.setup_admin()
        tok, _ = self.csrf(cookie)
        reqf = os.path.join(config.CONTROL_DIR, "request.json")
        for unit in ("ssh.service", "paper-agent-cycle.timer; reboot", "../x"):
            self.req("POST", "/service", {"csrf": tok, "action": "restart", "unit": unit}, cookie)
            self.assertFalse(os.path.exists(reqf), unit)
        self.req("POST", "/service", {"csrf": tok, "action": "reboot", "unit": ""}, cookie)
        self.assertFalse(os.path.exists(reqf))
        st, h, _ = self.req("POST", "/service", {"csrf": tok, "action": "restart", "unit": "paper-agent-dashboard.service"}, cookie)
        self.assertEqual(jload(reqf)["args"], {"unit": "paper-agent-dashboard.service"})

    def test_reset_keeps_keys_and_needs_confirmation(self):
        cookie = self.setup_admin()
        tok, _ = self.csrf(cookie)
        self.req("POST", "/settings/save", {"csrf": tok, "f.risk.max_position_pct": "8"}, cookie)
        self.req("POST", "/settings/secrets", {"csrf": tok, "secret.ANTHROPIC_API_KEY": SECRET}, cookie)
        self.req("POST", "/settings/reset", {"csrf": tok}, cookie)
        self.assertTrue(os.path.exists(config.SETTINGS_PATH))              # not confirmed -> untouched
        self.req("POST", "/settings/reset", {"csrf": tok, "confirm": "1"}, cookie)
        self.assertFalse(os.path.exists(config.SETTINGS_PATH))
        self.assertEqual(S.read_secrets()["ANTHROPIC_API_KEY"], SECRET)

    def test_pause_and_resume_trading(self):
        cookie = self.setup_admin()
        tok, _ = self.csrf(cookie)
        self.req("POST", "/control/pause", {"csrf": tok}, cookie)
        self.assertTrue(os.path.exists(self.pause))
        self.assertIn("TRADING PAUSED", self.req("GET", "/")[2])
        self.req("POST", "/control/resume", {"csrf": tok}, cookie)
        self.assertFalse(os.path.exists(self.pause))

    def test_xss_in_flash_and_security_headers(self):
        cookie = self.setup_admin()
        _, h, page = self.req("GET", "/?tab=settings&flash=" + urllib.parse.quote("<script>alert(1)</script>"), cookie=cookie)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("default-src 'none'", h["content-security-policy"])
        self.assertEqual(h["x-frame-options"], "DENY")
        self.assertNotIn("onchange=", self.req("GET", "/?tab=decisions")[2])           # inline JS is blocked by the CSP

    def test_llm_test_button_explains_when_off_and_doctor_output_is_shown(self):
        cookie = self.setup_admin()
        tok, _ = self.csrf(cookie)
        st, _, page = self.req("POST", "/test/llm?tier=triage", {"csrf": tok}, cookie)
        self.assertIn("LLM is OFF", page)
        with mock.patch("paper_agent.doctor.run", side_effect=lambda *a, **k: k["out"]("=== DOCTOR STUB <b> ===")):
            st, _, page = self.req("POST", "/test/doctor", {"csrf": tok}, cookie)
        self.assertIn("DOCTOR STUB &lt;b&gt;", page)

    def test_public_pages_still_work_without_login_but_never_show_settings(self):
        for tab in ("overview", "theses", "positions", "dossiers", "decisions", "risk", "intelligence", "system"):
            self.assertEqual(self.req("GET", "/?tab=" + tab)[0], 200, tab)
        self.assertEqual(self.req("GET", "/health")[0], 200)


class TestDashboardOpenByDefault(HTTPHarness, unittest.TestCase):
    """The shipped default: NO password. Settings are open to the LAN, but forged cross-site requests are still refused."""

    def test_default_is_no_password(self):
        self.assertFalse(config.DEFAULTS["runtime"]["require_password"])

    def test_settings_open_with_no_login_and_no_login_buttons(self):
        st, _, page = self.req("GET", "/?tab=settings")
        self.assertEqual(st, 200)
        for needle in ("Risk limits", "API keys", "Services", "Pause trading"):
            self.assertIn(needle, page)
        self.assertNotIn("Log out", page)
        self.assertNotIn(">Log in<", page)

    def test_login_and_setup_pages_just_go_to_settings(self):
        for path in ("/login", "/setup"):
            st, h, _ = self.req("GET", path)
            self.assertEqual((st, h["location"]), (303, "/?tab=settings"))
        self.assertEqual(self.req("POST", "/login", {"password": "x"})[0], 303)

    def test_posts_still_need_the_csrf_token(self):
        tok, _ = self.csrf()
        self.assertEqual(self.req("POST", "/settings/save", {"f.risk.max_position_pct": "7"})[0], 403)
        self.assertEqual(self.req("POST", "/settings/save", {"csrf": "forged", "f.risk.max_position_pct": "7"})[0], 403)
        self.assertFalse(os.path.exists(config.SETTINGS_PATH))
        self.assertEqual(self.req("POST", "/settings/save", {"csrf": tok, "f.risk.max_position_pct": "7"})[0], 303)
        self.assertEqual(jload(config.SETTINGS_PATH), {"risk": {"max_position_pct": 0.07}})

    def test_cross_site_requests_are_refused_even_with_a_valid_token(self):
        """Without a login, this is what stops a malicious web page you visit from silently changing your settings."""
        tok, _ = self.csrf()
        for hdrs in ({"Origin": "http://evil.example"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}):
            st, _, _ = self.req("POST", "/settings/save", {"csrf": tok, "f.risk.max_position_pct": "9"}, headers=hdrs)
            self.assertEqual(st, 403, hdrs)
        self.assertFalse(os.path.exists(config.SETTINGS_PATH))
        host = "127.0.0.1:%d" % self.port
        self.assertEqual(self.req("POST", "/settings/save", {"csrf": tok, "f.risk.max_position_pct": "9"}, headers={"Origin": "http://" + host, "Sec-Fetch-Site": "same-origin"})[0], 303)

    def test_keys_still_masked_and_service_actions_still_whitelisted(self):
        tok, _ = self.csrf()
        self.req("POST", "/settings/secrets", {"csrf": tok, "secret.ANTHROPIC_API_KEY": SECRET})
        self.assertEqual(S.read_secrets()["ANTHROPIC_API_KEY"], SECRET)
        _, _, page = self.req("GET", "/?tab=settings")
        self.assertNotIn(SECRET, page)
        self.assertIn("2345", page)
        reqf = os.path.join(config.CONTROL_DIR, "request.json")
        self.req("POST", "/service", {"csrf": tok, "action": "restart", "unit": "ssh.service"})
        self.assertFalse(os.path.exists(reqf))
        self.req("POST", "/service", {"csrf": tok, "action": "restart", "unit": "paper-agent-cycle.timer"})
        self.assertEqual(jload(reqf)["args"], {"unit": "paper-agent-cycle.timer"})

    def test_pause_resume_work_without_login(self):
        tok, _ = self.csrf()
        self.req("POST", "/control/pause", {"csrf": tok})
        self.assertTrue(os.path.exists(self.pause))
        self.req("POST", "/control/resume", {"csrf": tok})
        self.assertFalse(os.path.exists(self.pause))

    def test_pages_do_not_ask_browsers_to_send_origin_null(self):
        """REGRESSION (found on the Pi): `Referrer-Policy: no-referrer` makes browsers send `Origin: null` on same-origin form posts,
        which our cross-site check then (correctly) refused - blocking every legitimate Save."""
        _, h, _ = self.req("GET", "/?tab=settings")
        self.assertEqual(h["referrer-policy"], "same-origin")
        _, h, _ = self.req("GET", "/")
        self.assertEqual(h["referrer-policy"], "same-origin")

    def test_a_real_browsers_same_origin_post_is_accepted(self):
        """Exactly what Chrome/Safari/Firefox send for a form on http://<pi>:8088 (no Sec-Fetch-* headers on plain HTTP)."""
        tok, _ = self.csrf()
        host = "127.0.0.1:%d" % self.port
        st, _, _ = self.req("POST", "/settings/save", {"csrf": tok, "f.risk.max_position_pct": "6"},
                            headers={"Origin": "http://" + host, "Referer": "http://%s/?tab=settings" % host, "Host": host, "Upgrade-Insecure-Requests": "1"})
        self.assertEqual(st, 303)
        self.assertEqual(jload(config.SETTINGS_PATH), {"risk": {"max_position_pct": 0.06}})

    def test_dns_rebinding_is_refused(self):
        """A hostile site can point its own domain at the Pi: Origin == Host == evil domain. The Host itself must look like a LAN."""
        tok, _ = self.csrf()
        for evil in ("evil.example.com", "attacker.net", "192.168.1.91.evil.example.org"):
            h = "%s:%d" % (evil, self.port)
            st, _, body = self.req("POST", "/settings/save", {"csrf": tok, "f.risk.max_position_pct": "9"}, headers={"Host": h, "Origin": "http://" + h})
            self.assertEqual(st, 403, evil)
            self.assertIn("does not look like your local network", body)
        self.assertFalse(os.path.exists(config.SETTINGS_PATH))

    def test_normal_lan_hostnames_work(self):
        tok, _ = self.csrf()
        for i, name in enumerate(("192.168.1.91", "10.0.0.5", "orangepizero3", "pi.local", "localhost", "pi.home.arpa", "[::1]")):
            h = "%s:%d" % (name, self.port)
            st, _, _ = self.req("POST", "/settings/save", {"csrf": tok, "f.risk.max_position_pct": str(5 + i)}, headers={"Host": h, "Origin": "http://" + h})
            self.assertEqual(st, 303, name)

    def test_custom_hostname_can_be_allowed_from_settings(self):
        tok, _ = self.csrf()
        h = "pi.example.com:%d" % self.port
        hdr = {"Host": h, "Origin": "http://" + h}
        self.assertEqual(self.req("POST", "/settings/save", {"csrf": tok, "f.risk.max_position_pct": "8"}, headers=hdr)[0], 403)
        self.assertEqual(self.req("POST", "/settings/save", {"csrf": tok, "f.runtime.allowed_hosts": "pi.example.com"})[0], 303)
        self.assertEqual(self.req("POST", "/settings/save", {"csrf": tok, "f.risk.max_position_pct": "8"}, headers=hdr)[0], 303)

    def test_disable_dead_feeds_button_disables_only_permanently_dead_ones(self):
        os.makedirs(config.CONFIG_DIR, exist_ok=True)
        write(os.path.join(config.CONFIG_DIR, "sources.json"), json.dumps({"sources": [
            {"id": "imf", "name": "IMF", "type": "rss", "url": "u", "interval": 600}, {"id": "state", "name": "State", "type": "rss", "url": "u", "interval": 600},
            {"id": "good", "name": "Good", "type": "rss", "url": "u", "interval": 600}]}))
        c = db.connect(self.dbpath)
        for sid, st, fails in (("imf", "404", 5), ("state", "403", 9), ("good", "200", 0)):
            c.execute("INSERT INTO source_health(source_id,name,last_status,failures,last_success) VALUES(?,?,?,?,?)", (sid, sid, st, fails, iso_now if fails == 0 else None))
        c.close()
        tok, page = self.csrf()
        self.assertIn("Disable these 1 feeds", page)
        self.assertEqual(self.req("POST", "/settings/disable-dead", {})[0], 403)                      # still needs the CSRF token
        self.assertEqual(self.req("POST", "/settings/disable-dead", {"csrf": tok})[0], 303)
        self.assertEqual(jload(config.SETTINGS_PATH), {"source_overrides": {"imf": {"enabled": False}}})   # state (403) and good feeds untouched
        self.assertNotIn("Disable these", self.req("GET", "/?tab=settings")[2])

    def test_a_password_can_be_switched_on_from_settings_later(self):
        tok, _ = self.csrf()
        self.assertEqual(self.req("POST", "/settings/save", {"csrf": tok, "f.runtime.require_password": "1"})[0], 303)
        st, h, _ = self.req("GET", "/?tab=settings")
        self.assertEqual((st, h["location"]), (303, "/setup"))            # now the first-visit password creation appears
        cookie = self.setup_admin()
        self.assertEqual(self.req("GET", "/?tab=settings", cookie=cookie)[0], 200)
        self.assertEqual(self.req("GET", "/?tab=settings")[0], 303)         # and without the cookie it is locked again


if __name__ == "__main__":
    unittest.main()
