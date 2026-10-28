import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
from paper_agent import config, keys, net  # noqa: E402

FIRST, SECOND = "dbfvmrpr01qiahuc7f7g", "dbfvmrpr01qiahuc7f80"
BOTH = FIRST + SECOND


class TestFinnhubKeyResolution(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self._old = config.SETTINGS_PATH
        config.SETTINGS_PATH = os.path.join(self.d, "settings.json")
        self.calls = []
        self.t = 1000.0

    def tearDown(self):
        config.SETTINGS_PATH = self._old

    def fetch(self, good):
        def f(url, **k):
            tok = url.rsplit("token=", 1)[1]
            self.calls.append(tok)
            if tok == good:
                return {"c": 270.5, "t": 1}, 200
            raise net.FetchError("HTTP 401", 401)
        return f

    def test_the_working_half_of_a_pasted_pair_is_found_and_used(self):
        env = {"FINNHUB_API_KEY": BOTH}
        self.assertEqual(keys.resolve_finnhub(self.fetch(SECOND), lambda: self.t, env), SECOND)
        self.assertEqual(env["FINNHUB_API_KEY"], SECOND)
        self.assertEqual(self.calls, [FIRST, SECOND])                               # tried in order; 401 on the wrong one

    def test_the_answer_is_remembered_so_there_is_no_network_afterwards(self):
        keys.resolve_finnhub(self.fetch(FIRST), lambda: self.t, {"FINNHUB_API_KEY": BOTH})
        self.calls.clear()
        env = {"FINNHUB_API_KEY": BOTH}
        self.assertEqual(keys.resolve_finnhub(self.fetch(FIRST), lambda: self.t, env), FIRST)
        self.assertEqual((self.calls, env["FINNHUB_API_KEY"]), ([], FIRST))
        self.assertEqual(oct(os.stat(keys._cache_path()).st_mode & 0o777), "0o600")

    def test_failures_are_not_hammered_but_retried_after_an_hour(self):
        env = {"FINNHUB_API_KEY": BOTH}
        keys.resolve_finnhub(self.fetch("neither"), lambda: self.t, env)
        self.assertEqual(env["FINNHUB_API_KEY"], BOTH)                              # unchanged: nothing worked
        n = len(self.calls)
        keys.resolve_finnhub(self.fetch(SECOND), lambda: self.t + 600, env)         # 10 min later: skipped
        self.assertEqual(len(self.calls), n)
        self.assertEqual(keys.resolve_finnhub(self.fetch(SECOND), lambda: self.t + 3700, env), SECOND)   # an hour later: tries again

    def test_network_errors_never_raise_or_change_the_key(self):
        def down(url, **k):
            raise OSError("no route")
        env = {"FINNHUB_API_KEY": BOTH}
        self.assertEqual(keys.resolve_finnhub(down, lambda: self.t, env), BOTH)
        self.assertEqual(env["FINNHUB_API_KEY"], BOTH)

    def test_a_normal_20_character_key_or_no_key_is_left_alone_without_any_call(self):
        env = {"FINNHUB_API_KEY": FIRST}
        self.assertEqual(keys.resolve_finnhub(self.fetch("x"), lambda: self.t, env), FIRST)
        self.assertEqual(keys.resolve_finnhub(self.fetch("x"), lambda: self.t, {}), None)
        self.assertEqual(self.calls, [])

    def test_a_changed_pasted_value_is_resolved_afresh(self):
        keys.resolve_finnhub(self.fetch(FIRST), lambda: self.t, {"FINNHUB_API_KEY": BOTH})
        other = "a" * 20 + "b" * 20
        self.calls.clear()
        keys.resolve_finnhub(self.fetch("b" * 20), lambda: self.t, {"FINNHUB_API_KEY": other})
        self.assertEqual(self.calls, ["a" * 20, "b" * 20])


if __name__ == "__main__":
    unittest.main()
