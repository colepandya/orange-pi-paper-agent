"""Banded ('zebra') table rows on every dashboard table (System and Settings included). Readability is guarded in BOTH themes: the shaded row must not make any text colour hard to read."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
from paper_agent import dashboard  # noqa: E402

LIGHT = dict(bg="#ffffff", card="#f5f7fa", fg="#14181f", mut="#5b6573", ok="#0a7d3b", bad="#b3261e", warn="#9a6700", acc="#1f5fbf")
DARK = dict(bg="#0e1116", card="#171c24", fg="#e6e9ee", mut="#98a2b3", ok="#4cc38a", bad="#ff7b72", warn="#e3b341", acc="#6ea8ff")


def lum(h):
    h = h.lstrip("#")
    r, g, b = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def ratio(a, b):
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


class TestZebraStripes(unittest.TestCase):
    def test_every_second_data_row_is_shaded_in_light_and_dark_mode(self):
        css = dashboard.CSS
        self.assertEqual(css.count("tr:nth-child(2n+3) td{background:var(--stripe)}"), 1)         # rows 3, 5, 7...: the header is row 1 and the first data row stays plain
        self.assertEqual(css.count("--stripe:"), 2)
        light, dark = css.split("@media(prefers-color-scheme:dark)")
        self.assertIn("--stripe:#f3f6fa", light)
        self.assertIn("--stripe:#1c222c", dark)

    def test_the_shading_never_makes_text_hard_to_read_and_is_still_visible(self):
        for name, theme, stripe, min_text, min_visible in (("light", LIGHT, "#f3f6fa", 4.4, 1.07), ("dark", DARK, "#1c222c", 6.0, 1.15)):
            for k in ("fg", "mut", "ok", "bad", "warn", "acc"):
                self.assertGreaterEqual(ratio(theme[k], stripe), min_text, "%s %s on the shaded row" % (name, k))
            self.assertGreaterEqual(ratio(stripe, theme["bg"]), min_visible, "%s: the band must be visible against the page" % name)

    def test_the_system_and_settings_tables_are_all_ordinary_rows_that_get_it(self):
        c, cfg = H.fresh_db(), H.cfg()
        system = dashboard.render(c, cfg, "system", {})
        settings = dashboard.render(c, cfg, "settings", {}, {"authed": True})
        for page in (system, settings):
            self.assertIn("tr:nth-child(2n+3) td{background:var(--stripe)}", page)
            self.assertGreater(page.count("<tr>"), 10)
        self.assertGreater(settings.count("<table"), 5)


if __name__ == "__main__":
    unittest.main()
