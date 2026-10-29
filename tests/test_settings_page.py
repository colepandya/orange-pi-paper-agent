"""REGRESSION: the 18 Swing Strategy settings were defined and validated but NEVER SHOWN: the Settings page loops over a fixed list of groups and the new group was not in it.
Every setting must be on the page, and the page must round-trip."""
import html as htmllib
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paper_agent import config, settings as S, settings_ui as U  # noqa: E402


def page(cfg):
    return U.page_settings(cfg, cfg, {}, [], {}, {}, None)


def posted(html):
    """What a browser would POST for this page: every named input -> its displayed value (unticked checkboxes are absent)."""
    form = {}
    for m in re.finditer(r"<input[^>]*>", html):
        tag = m.group(0)
        n, v, t = re.search(r"name=['\"]?([\w.]+)", tag), re.search(r"value=['\"]?([^'\">]*)", tag), re.search(r"type=['\"]?(\w+)", tag)
        if n and n.group(1).startswith("f."):
            if t and t.group(1) == "checkbox":
                if "checked" in tag:
                    form[n.group(1)] = "on"
            else:
                form[n.group(1)] = htmllib.unescape(v.group(1)) if v else ""
    return form


class TestEverySettingIsOnThePage(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load_config("/nonexistent", with_overrides=False)
        self.html = page(self.cfg)

    def test_every_group_is_listed_and_every_setting_is_displayed(self):
        self.assertLessEqual({f.group for f in S.FIELDS}, set(S.GROUP_ORDER), "a settings group is defined but would never be shown")
        missing = [f.path for f in S.FIELDS if f.name not in self.html]
        self.assertEqual(missing, [])
        for g in S.GROUP_ORDER:
            self.assertIn("<h2>%s</h2>" % g, self.html)

    def test_the_swing_group_is_there_with_all_its_controls(self):
        sw = [f for f in S.FIELDS if f.group == "Swing strategy"]
        self.assertEqual(len(sw), len(self.cfg["strategy"]))                        # one control per strategy key
        self.assertLess(self.html.index("<h2>Swing strategy</h2>"), self.html.index("<h2>Execution</h2>"))

    def test_saving_the_untouched_page_is_clean(self):
        _new, errors, _w = S.apply_form(posted(self.html), self.cfg, self.cfg, {})
        self.assertFalse(errors, errors)

    def test_an_edit_in_the_swing_group_is_stored_in_the_right_units(self):
        form = posted(self.html)
        form["f.strategy.target_min_pct"] = "1.5"                                  # the page shows percentages
        form["f.strategy.max_hold_days"] = "2"
        new, errors, _w = S.apply_form(form, self.cfg, self.cfg, {})
        self.assertFalse(errors, errors)
        self.assertEqual((new["strategy"]["target_min_pct"], new["strategy"]["max_hold_days"]), (0.015, 2))

    def test_nonsense_in_the_swing_group_is_refused(self):
        for k, bad in (("f.strategy.max_hold_days", "99"), ("f.strategy.stop_max_pct", "50"), ("f.strategy.min_reward_risk", "0"), ("f.strategy.trail_pct", "-1")):
            form = posted(self.html)
            form[k] = bad
            self.assertTrue(S.apply_form(form, self.cfg, self.cfg, {})[1], k)

    def test_the_page_shows_defaults_as_the_owner_would_expect(self):
        form = posted(self.html)
        self.assertEqual((form["f.strategy.target_min_pct"], form["f.strategy.partial_exit_pct"], form["f.strategy.max_hold_days"]), ("1.2", "60", "3"))


if __name__ == "__main__":
    unittest.main()
