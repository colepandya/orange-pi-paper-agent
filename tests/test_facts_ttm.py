"""The SEC reader's period arithmetic, on the awkward shapes real filings have: non-calendar fiscal years, 52/53-week years, a missing prior-year figure, amendments, duplicate contexts, a change of unit, discontinued
operations and a change of fiscal year-end. The rule under test: a twelve-month figure is built ONLY when a reliable construction exists; otherwise the metric is UNAVAILABLE with a reason. An old annual figure is never
quietly used in place of a recent period."""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paper_agent import facts as F  # noqa: E402


def now(s):
    return dt.datetime.fromisoformat(s + "T12:00:00+00:00")


def r(start, end, val, filed, form="10-Q", accn=""):
    d = {"end": end, "val": val, "filed": filed, "form": form, "accn": accn}
    if start:
        d["start"] = start
    return d


def clean(rows, unit="USD"):
    return F._points({"units": {unit: rows}})


def calendar_company(extra=()):
    """December year-end, as of mid-August 2026."""
    return [r("2025-01-01", "2025-12-31", 400, "2026-02-10", "10-K"), r("2026-01-01", "2026-06-30", 220, "2026-08-01"), r("2025-01-01", "2025-06-30", 185, "2026-08-01")] + list(extra)


class TestTheReliableConstructions(unittest.TestCase):
    def test_a_calendar_year(self):
        d = F.ttm_detail(clean(calendar_company()), now("2026-08-15"))
        self.assertEqual((d["value"], d["period_end"], d["age_days"]), (435, "2026-06-30", 46))
        self.assertEqual(d["basis"], "annual + year-to-date - prior year-to-date")

    def test_a_june_fiscal_year(self):
        rows = [r("2024-07-01", "2025-06-30", 800, "2025-08-15", "10-K"), r("2025-07-01", "2025-12-31", 430, "2026-02-05"), r("2024-07-01", "2024-12-31", 380, "2025-02-05")]
        d = F.ttm_detail(clean(rows), now("2026-02-20"))
        self.assertEqual((d["value"], d["period_end"]), (850, "2025-12-31"))

    def test_a_september_52_week_year_whose_periods_do_not_line_up_to_the_day(self):
        rows = [r("2024-09-29", "2025-09-27", 1000, "2025-10-31", "10-K"), r("2025-09-28", "2026-06-27", 800, "2026-08-01"), r("2024-09-29", "2025-06-28", 730, "2025-08-01")]    # 363-day year, 272-day periods, 364 days apart
        d = F.ttm_detail(clean(rows), now("2026-08-20"))
        self.assertEqual((d["value"], d["period_end"]), (1070, "2026-06-27"))

    def test_a_53_week_year_with_year_to_date_periods_of_different_lengths(self):
        rows = [r("2023-12-31", "2025-01-04", 900, "2025-02-20", "10-K"), r("2025-01-05", "2025-06-28", 450, "2025-08-01"), r("2023-12-31", "2024-06-29", 430, "2024-08-01")]       # 371-day year; 174 vs 181 days
        d = F.ttm_detail(clean(rows), now("2025-08-15"))
        self.assertEqual(d["value"], 900 + 450 - 430)

    def test_four_contiguous_quarters_when_there_is_no_prior_year_to_date(self):
        rows = [r("2025-01-01", "2025-12-31", 400, "2026-02-10", "10-K"), r("2025-01-01", "2025-03-31", 90, "2025-05-01"), r("2025-04-01", "2025-06-30", 95, "2025-08-01"), r("2025-07-01", "2025-09-30", 100, "2025-11-01"),
                r("2026-01-01", "2026-03-31", 105, "2026-05-01"), r("2026-04-01", "2026-06-30", 115, "2026-08-01"), r("2026-01-01", "2026-06-30", 220, "2026-08-01")]             # no prior-year six-month figure
        pts = clean(rows)
        self.assertEqual([p["val"] for p in F.quarters(pts) if p.get("derived")], [115])                  # the fourth quarter a 10-K never reports
        d = F.ttm_detail(pts, now("2026-08-15"))
        self.assertEqual((d["value"], d["basis"]), (100 + 115 + 105 + 115, "last four contiguous quarters"))

    def test_a_recent_annual_alone_is_a_valid_twelve_months_but_only_while_it_is_recent(self):
        pts = clean([r("2025-01-01", "2025-12-31", 400, "2026-02-10", "10-K")])
        d = F.ttm_detail(pts, now("2026-03-15"))
        self.assertEqual((d["value"], d["basis"]), (400, "latest annual (recent, nothing newer)"))
        self.assertIn("unavailable", F.ttm_detail(pts, now("2026-08-15")))                                 # 227 days later it is NOT a trailing figure any more


class TestWhenAReliableFigureCannotBeBuilt(unittest.TestCase):
    def assert_unavailable(self, d, *words):
        self.assertNotIn("value", d)
        for w in words:
            self.assertIn(w, d["unavailable"])

    def test_a_missing_prior_year_to_date_gives_unavailable_and_never_the_stale_annual(self):
        d = F.ttm_detail(clean([r("2025-01-01", "2025-12-31", 400, "2026-02-10", "10-K"), r("2026-01-01", "2026-06-30", 220, "2026-08-01")]), now("2026-08-15"))
        self.assert_unavailable(d, "prior-year year-to-date", "NOT used in its place")

    def test_a_gap_in_the_quarters(self):
        rows = [r("2025-01-01", "2025-03-31", 90, "2025-05-01"), r("2025-04-01", "2025-06-30", 95, "2025-08-01"), r("2025-10-01", "2025-12-31", 110, "2026-02-01"), r("2026-01-01", "2026-03-31", 105, "2026-05-01"), r("2026-04-01", "2026-06-30", 115, "2026-08-01")]
        d = F.ttm_detail(clean(rows), now("2026-08-15"))
        self.assert_unavailable(d)

    def test_a_period_that_ended_too_long_ago(self):
        d = F.ttm_detail(clean(calendar_company()), now("2027-05-01"))
        self.assert_unavailable(d, "ended 305 days ago")

    def test_a_change_of_fiscal_year_end_is_not_papered_over(self):
        rows = [r("2023-04-01", "2024-03-31", 600, "2024-05-20", "10-K"), r("2024-04-01", "2024-12-31", 480, "2025-02-15", "10-KT"), r("2025-01-01", "2025-06-30", 350, "2025-08-01")]   # March year-end moved to December
        d = F.ttm_detail(clean(rows), now("2025-08-15"))
        self.assert_unavailable(d, "NOT used")
        recovered = F.ttm_detail(clean(rows + [r("2025-01-01", "2025-12-31", 700, "2026-02-10", "10-K")]), now("2026-03-01"))
        self.assertEqual(recovered["value"], 700)                                                          # once a full year under the new year-end exists, it is a valid recent annual

    def test_a_change_of_reporting_currency(self):
        usd_now_eur_before = F._points({"units": {"EUR": [r("2025-01-01", "2025-12-31", 400, "2026-02-10", "10-K"), r("2025-01-01", "2025-06-30", 185, "2025-08-01")], "USD": [r("2026-01-01", "2026-06-30", 240, "2026-08-01")]}})
        self.assertTrue(all(p["unit"] == "USD" for p in usd_now_eur_before))                                # only the unit that still has the newest data
        self.assertTrue(all(p.get("multi_unit") == ["EUR"] for p in usd_now_eur_before))
        self.assert_unavailable(F.ttm_detail(usd_now_eur_before, now("2026-08-15")))
        mixed = F._points({"units": {"USD": calendar_company(), "EUR": [r("2026-01-01", "2026-03-31", 5, "2026-05-01")]}})
        d = F.ttm_detail(mixed, now("2026-08-15"))
        self.assert_unavailable(d, "unit or currency changed")                                              # both units alive in the same window: ambiguous, so unavailable

    def test_a_currency_that_stopped_long_ago_is_irrelevant(self):
        old_eur = [r("2019-01-01", "2019-12-31", 300, "2020-02-10", "10-K")]
        pts = F._points({"units": {"USD": calendar_company(), "EUR": old_eur}})
        self.assertFalse(any(p.get("multi_unit") for p in pts))
        self.assertEqual(F.ttm_detail(pts, now("2026-08-15"))["value"], 435)
        eur_prior = F._points({"units": {"USD": [x for x in calendar_company() if x["val"] != 185], "EUR": [r("2025-01-01", "2025-06-30", 185, "2025-08-01")]}})
        self.assert_unavailable(F.ttm_detail(eur_prior, now("2026-08-15")), "reporting currency may have changed")

    def test_conflicting_values_for_the_same_period(self):
        d = F.ttm_detail(clean(calendar_company([r("2026-01-01", "2026-06-30", 225, "2026-08-01")])), now("2026-08-15"))
        self.assert_unavailable(d, "conflicting")


class TestEachAlignmentRuleOnItsOwn(unittest.TestCase):
    """Each rule isolated from the others, so that removing any one of them fails a test (a bad case that another rule happens to catch does not count)."""

    def test_a_year_to_date_must_start_where_the_annual_ended(self):
        rows = [r("2025-01-01", "2025-12-31", 400, "2026-02-10", "10-K"), r("2026-03-01", "2026-06-30", 150, "2026-08-01"), r("2025-03-01", "2025-06-30", 120, "2025-08-01")]          # a four-month period starting in March, not January
        d = F.ttm_detail(clean(rows), now("2026-08-15"))
        self.assertNotIn("value", d)
        self.assertIn("starts where the last annual ended", d["unavailable"])

    def test_the_prior_figure_must_be_the_same_period_one_year_earlier(self):
        rows = [r("2025-01-01", "2025-12-31", 400, "2026-02-10", "10-K"), r("2026-01-01", "2026-06-30", 220, "2026-08-01"), r("2024-01-01", "2024-06-30", 150, "2025-08-01")]            # two years earlier, not one
        d = F.ttm_detail(clean(rows), now("2026-08-15"))
        self.assertNotIn("value", d)
        self.assertIn("prior-year year-to-date", d["unavailable"])

    def test_four_quarters_must_follow_each_other_without_gaps_even_when_they_span_a_year(self):
        qs = [r("2025-07-01", "2025-09-23", 100, "2025-11-01"), r("2025-10-04", "2025-12-26", 100, "2026-02-01"), r("2026-01-06", "2026-03-30", 100, "2026-05-01"), r("2026-04-10", "2026-07-02", 100, "2026-08-01")]      # 83-day quarters with 10-day holes, together spanning a year
        d = F.ttm_detail(clean(qs), now("2026-08-15"))
        self.assertNotIn("value", d)
        self.assertIn("not contiguous", d["unavailable"])

    def test_a_fourth_quarter_is_derived_only_from_three_quarters_that_leave_no_hole(self):
        holey = [r("2025-01-01", "2025-12-31", 400, "2026-02-10", "10-K"), r("2025-01-01", "2025-03-31", 90, "2025-05-01"), r("2025-04-11", "2025-07-01", 95, "2025-08-01"), r("2025-07-02", "2025-09-30", 100, "2025-11-01")]       # April 1-10 is in no quarter
        self.assertEqual([p for p in F.quarters(clean(holey)) if p.get("derived")], [])                       # deriving would wrongly push those ten days into the fourth quarter

    def test_an_old_balance_sheet_figure_is_not_used(self):
        old = clean([r(None, "2025-06-30", 90, "2025-08-01")])
        d = F.stock_detail(old, now("2026-08-15"))
        self.assertIn("days old", d["unavailable"])
        f = F.summarize({"revenue": calendar_company(), "cash": [r(None, "2025-06-30", 90, "2025-08-01")]}, now("2026-08-15"))
        self.assertNotIn("cash", f["metrics"])
        self.assertIn("cash", f["unavailable"])


class TestAmendmentsDuplicatesAndRecasts(unittest.TestCase):
    def test_an_amendment_replaces_the_original(self):
        d = F.ttm_detail(clean(calendar_company([r("2025-01-01", "2025-12-31", 380, "2026-04-01", "10-K/A")])), now("2026-08-15"))
        self.assertEqual(d["value"], 380 + 220 - 185)

    def test_duplicate_contexts_with_the_same_value_are_one_figure_not_a_conflict(self):
        dup = [r("2026-01-01", "2026-06-30", 220, "2026-08-01", accn="b"), r("2026-01-01", "2026-06-30", 220, "2026-08-01", accn="c"), r("2025-01-01", "2025-12-31", 400, "2026-02-10", "10-K", accn="d")]
        d = F.ttm_detail(clean(calendar_company(dup)), now("2026-08-15"))
        self.assertEqual(d["value"], 435)

    def test_recast_comparatives_are_used_not_the_original_prior_year(self):
        original = r("2025-01-01", "2025-06-30", 205, "2025-08-01")                                          # as first reported
        recast = r("2025-01-01", "2025-06-30", 185, "2026-08-01")                                            # recast for a disposal, filed with this year's 10-Q
        d = F.ttm_detail(clean([r("2025-01-01", "2025-12-31", 400, "2026-02-10", "10-K"), r("2026-01-01", "2026-06-30", 220, "2026-08-01"), original, recast]), now("2026-08-15"))
        self.assertEqual(d["value"], 400 + 220 - 185)

    def test_discontinued_operations_do_not_make_a_continuing_business_look_loss_making(self):
        ann = lambda v: [r("2025-01-01", "2025-12-31", v, "2026-02-10", "10-K")]  # noqa: E731
        raw = {"revenue": ann(900), "net_income": ann(-50), "net_income_cont": ann(30), "disc_ops": ann(-80)}
        f = F.summarize(raw, now("2026-03-01"))
        self.assertEqual(f["net_income_ttm"], -50)
        self.assertEqual(f["net_income_continuing_ttm"], 30)
        self.assertNotIn("loss_making", [x["flag"] for x in f["flags"]])
        self.assertIn("discontinued operations", f["discontinued_note"])


class TestGrowthIsOnTheSameFooting(unittest.TestCase):
    def test_growth_from_the_year_to_date_pair_then_the_quarter_then_a_recent_annual_else_unavailable(self):
        self.assertAlmostEqual(F.growth_detail(clean(calendar_company()), now("2026-08-15"))["value"], 220 / 185.0 - 1, places=4)
        q = clean([r("2025-04-01", "2025-06-30", 95, "2025-08-01"), r("2026-04-01", "2026-06-30", 115, "2026-08-01")])
        d = F.growth_detail(q, now("2026-08-15"))
        self.assertAlmostEqual(d["value"], 115 / 95.0 - 1, places=4)
        self.assertIn("quarter", d["basis"])
        ann = clean([r("2024-01-01", "2024-12-31", 350, "2025-02-10", "10-K"), r("2025-01-01", "2025-12-31", 400, "2026-02-10", "10-K")])
        self.assertAlmostEqual(F.growth_detail(ann, now("2026-03-15"))["value"], 400 / 350.0 - 1, places=4)
        self.assertIn("unavailable", F.growth_detail(ann, now("2026-08-15")))                              # an old annual comparison is not "growth now"


if __name__ == "__main__":
    unittest.main()
