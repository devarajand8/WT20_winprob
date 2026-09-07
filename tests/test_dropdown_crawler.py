#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for the browser fallback (dropdown crawler) without a browser.

A stand-in page mimics the Cricinfo commentary widget: one dropdown entry per
view, each view's feed delivered in reverse-chronological chunks, and ball records
appended to the shared list as if intercepted from `page.on("response")`.

The behaviours pinned down here are the ones that decide whether a Super Over is
captured in --no-api mode:

  * every dropdown entry is visited *by position*, so two identically-labelled
    super-over halves are not silently merged into one;
  * non-innings entries ("Match Feedback") are skipped but do not shift positions;
  * scrolling stops as soon as the innings' opening ball is seen, so a 6-ball
    super over costs one page-down instead of a dozen.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scrape_cricinfo_balls as sc  # noqa: E402


def feed(inning, overs):
    """Chronological ball dicts for an innings (Cricinfo numbers overs from 0)."""
    out = []
    for over in range(overs):
        for ball in range(1, 7):
            out.append({"id": f"{inning}-{over}-{ball}", "oversActual": f"{over}.{ball}",
                        "text": f"d{over}.{ball}", "inningNumber": inning})
    return out


class FakeOption:
    def __init__(self, site, position, label):
        self.site, self.position, self.label = site, position, label

    def inner_text(self):
        return f"{self.label}\nsubtitle row"

    def click(self, force=False, timeout=None):
        self.site.selected.append(self.position)
        self.site.current = self.position
        self.site.open = False
        self.site.chunks_served = 0


class FakeButton:
    def __init__(self, site):
        self.site = site

    def count(self):
        return 1

    def inner_text(self):
        return self.site.labels[self.site.current] + "\n"

    def click(self, force=False, timeout=None):
        self.site.open = True


class FakeLocator:
    def __init__(self, site, selector):
        self.site, self.selector = site, selector

    @property
    def first(self):
        return FakeButton(self.site) if self.selector == sc.DROPDOWN_BTN_SELECTOR else self

    def count(self):
        return 1 if self.selector == sc.DROPDOWN_BTN_SELECTOR else len(self.all())

    def all(self):
        if self.selector != sc.OPTION_SELECTORS or not self.site.open:
            return []
        return [FakeOption(self.site, i, lab) for i, lab in enumerate(self.site.raw_labels)]

    def inner_text(self):
        return self.first.inner_text()


class FakeKeyboard:
    def __init__(self, site):
        self.site = site

    def press(self, key):
        if key == "PageDown":
            self.site.page_down()


class FakeMouse:
    def __init__(self, site):
        self.site = site

    def click(self, x, y):
        self.site.open = False


class FakePage:
    """Only the handful of Playwright calls the crawler makes."""

    def __init__(self, labels, feeds, chunk=3):
        # labels may contain non-innings junk; feeds aligns with the *innings* labels
        self.raw_labels = labels
        self.labels = labels
        self.feeds = feeds                    # position -> chronological ball list
        self.chunk = chunk
        self.open = False
        self.current = 0
        self.selected = []
        self.page_downs = 0
        self.sink = None                      # set by the test = the intercepted list
        self.keyboard, self.mouse = FakeKeyboard(self), FakeMouse(self)
        self.context = None

    def locator(self, selector):
        return FakeLocator(self, selector)

    def evaluate(self, script, arg=None):
        return None

    def wait_for_timeout(self, ms):
        return None

    def page_down(self):
        self.page_downs += 1
        balls = self.feeds.get(self.current, [])
        if not balls or self.sink is None:
            return
        # the widget renders the *end* of the innings first; scrolling loads older chunks
        served = getattr(self, "_served", {})
        idx = served.get(self.current, 0)
        served[self.current] = idx + 1
        self._served = served
        remaining = len(balls) - idx * self.chunk
        if remaining <= 0:
            return
        chunk = balls[max(0, remaining - self.chunk):remaining]
        self.sink.extend(chunk)


class TestDropdownCrawler(unittest.TestCase):
    def setUp(self):
        self.stdout = sys.stdout
        sys.stdout = open(os.devnull, "w")

    def tearDown(self):
        sys.stdout.close()
        sys.stdout = self.stdout

    def test_all_entries_listed_with_dom_positions(self):
        labels = ["MI London (Men), 1st Inning", "Sunrisers Leeds (Men), 2nd Inning",
                  "1st Super Over", "2nd Super Over", "Match Feedback"]
        page = FakePage(labels, {})
        entries = sc.read_dropdown_options(page)
        self.assertEqual([pos for pos, _ in entries], [0, 1, 2, 3])
        self.assertEqual([lab for _, lab in entries], labels[:4])
        self.assertTrue(all("Feedback" not in lab for _, lab in entries))

    def test_duplicate_super_over_labels_are_both_kept(self):
        labels = ["1st Inning", "2nd Inning", "Super Over", "Super Over"]
        page = FakePage(labels, {})
        self.assertEqual([pos for pos, _ in sc.read_dropdown_options(page)], [0, 1, 2, 3])

    def test_every_view_visited_by_position(self):
        labels = ["1st Inning", "2nd Inning", "1st Super Over"]
        page = FakePage(labels, {})
        for _, position in [(0, 0), (1, 1), (2, 2)]:
            self.assertTrue(sc.select_dropdown_option(page, position))
        self.assertEqual(page.selected, [0, 1, 2])

    def test_out_of_range_position_reports_failure(self):
        page = FakePage(["1st Inning"], {})
        self.assertFalse(sc.select_dropdown_option(page, 9))

    def test_scroll_stops_at_opening_ball(self):
        balls_innings = feed(1, 3)                    # 18 balls, chunk 3 -> 6 page-downs
        page = FakePage(["1st Inning"], {0: balls_innings}, chunk=3)
        sink = []
        page.sink = sink
        got = sc.scroll_active_feed(page, "1st Inning", sink, max_scrolls=140)
        self.assertEqual(got, 18)
        self.assertEqual(len(sink), 18)
        self.assertEqual(sink[-3]["oversActual"], "0.1")   # final chunk served = start of innings
        self.assertEqual(len({b["id"] for b in sink}), 18)
        self.assertLess(page.page_downs, 8, "should stop as soon as 0.1 is captured")

    def test_super_over_costs_one_page_down(self):
        super_over = feed(3, 1)                        # 6 balls in one chunk
        page = FakePage(["Super Over"], {0: super_over}, chunk=6)
        sink = []
        page.sink = sink
        got = sc.scroll_active_feed(page, "Super Over", sink)
        self.assertEqual(got, 6)
        self.assertEqual(page.page_downs, 1)

    def test_empty_view_stops_after_stagnation(self):
        page = FakePage(["Nothing loaded"], {0: []}, chunk=6)
        sink = []
        page.sink = sink
        got = sc.scroll_active_feed(page, "Nothing loaded", sink, max_scrolls=140)
        self.assertEqual(got, 0)
        self.assertEqual(page.page_downs, 6, "must give up rather than scroll 140 times")


class TestCrawlDecision(unittest.TestCase):
    def test_complete_api_skips_crawl(self):
        plan = [sc.InningsPlan(number=1), sc.InningsPlan(number=2),
                sc.InningsPlan(number=3, is_super_over=True, super_over_number=1)]
        balls = feed(1, 2) + feed(2, 2) + feed(3, 1)
        need, reasons = sc.crawl_needed(plan, balls, [], True)
        self.assertFalse(need)
        self.assertEqual(reasons, [])

    def test_missing_super_over_forces_crawl_even_with_plenty_of_balls(self):
        plan = [sc.InningsPlan(number=1), sc.InningsPlan(number=2),
                sc.InningsPlan(number=3, is_super_over=True, super_over_number=1)]
        balls = feed(1, 20) + feed(2, 20)          # looks like a great haul...
        need, reasons = sc.crawl_needed(plan, balls, [], True)
        self.assertTrue(need)
        self.assertIn("SUPER OVER", reasons[0])

    def test_intercepted_balls_count_towards_coverage(self):
        plan = [sc.InningsPlan(number=1), sc.InningsPlan(number=3, is_super_over=True)]
        need, _ = sc.crawl_needed(plan, feed(1, 2), feed(3, 1), True)
        self.assertFalse(need)

    def test_incomplete_api_always_crawls(self):
        plan = [sc.InningsPlan(number=1)]
        need, _ = sc.crawl_needed(plan, feed(1, 1), [], False)
        self.assertTrue(need)

    def test_no_balls_at_all_crawls(self):
        need, _ = sc.crawl_needed([sc.InningsPlan(number=1)], [], [], True)
        self.assertTrue(need)


class TestSuperOverInFullPipeline(unittest.TestCase):
    """The tie-breaker must survive from raw payload to CSV row."""

    def setUp(self):
        self.stdout = sys.stdout
        sys.stdout = open(os.devnull, "w")

    def tearDown(self):
        sys.stdout.close()
        sys.stdout = self.stdout

    def test_payload_to_rows(self):
        payload = {"data": {"scorecard": {"innings": [
            {"Number": 1, "name": "India Inning", "teams": [{"name": "India"}]},
            {"Number": 2, "name": "New Zealand Inning", "teams": [{"name": "New Zealand"}]},
            {"Number": 3, "name": "Super Over", "isSuperOver": True, "teams": [{"name": "New Zealand"}]},
            {"Number": 4, "name": "Super Over", "isSuperOver": True, "teams": [{"name": "India"}]},
        ], "players": [
            {"id": 277916, "longName": "Tim Southee", "bowlingStyle": "RIGHT_ARM_MEDIUM_FAST"},
        ]}}}
        plan, extra = sc.build_innings_plan([payload], "Match tied, New Zealand won the Super Over")
        self.assertEqual(extra, [], "super overs are already in the plan, no probing needed")
        self.assertEqual([p.number for p in plan], [1, 2, 3, 4])
        self.assertEqual([p.super_over_number for p in plan][2:], [1, 1])

        balls = sc.parse_comments({"content": {"comments": [
            {"id": 9001, "oversActual": "0.1", "text": "six!", "inningNumber": 3, "bowlerPlayerId": 277916},
            {"id": 9002, "oversActual": "0.2", "text": "dot", "inningNumber": 3, "bowlerPlayerId": 277916},
            {"id": 9003, "oversActual": "0.1", "text": "four", "inningNumber": 4, "bowlerPlayerId": 277916},
        ]}})
        players = {}
        sc.find_players_anywhere(payload, players)
        df = sc.build_output_frame(balls, players, {"venue": "Wankhede", "matchId": "1"}, plan)
        self.assertEqual(len(df), 3)
        self.assertTrue(bool(df["isSuperOver"].all()))
        self.assertEqual(set(df["inningType"]), {"Super Over"})
        self.assertEqual(df["bowlerName"].tolist(), ["Tim Southee"] * 3)
        self.assertEqual(df["bowlerBowlingStyle"].tolist(), ["Right-arm Medium Fast"] * 3)
        # the two halves of the super over stay in fetch order, not by over number
        self.assertEqual(df["inningNumber"].tolist(), [3, 3, 4])


if __name__ == "__main__":
    unittest.main(verbosity=2)
