#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
End-to-end offline run of `scrape_match` against a fake browser.

Two scenarios that matter for super-over capture, neither of which needs the network:

  A. the JSON API answers -> every planned innings (super overs included) is paged
     directly and the dropdown crawler is *not* touched;
  B. the API is unavailable -> the dropdown crawler runs, visits the super-over
     views, and the balls delivered live into the shared interception list still
     reach the CSV (that copy-and-overwrite bug is what these tests guard).
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scrape_cricinfo_balls as sc  # noqa: E402

MATCH_URL = (
    "https://www.espncricinfo.com/series/the-hundred-men-s-competition-2026-1521176/"
    "mi-london-men-vs-sunrisers-leeds-men-1st-match-1521231/full-scorecard"
)

TIED_SCORECARD = {"data": {"matchHeader": {
    "series": {"longName": "The Hundred Men's Competition 2026"},
    "title": "MI London (Men) vs Sunrisers Leeds (Men), 1st Match",
    "ground": {"name": "The Oval", "town": {"name": "London"}},
}, "scorecard": {"innings": [
    {"Number": 1, "name": "MI London (Men) Inning", "teams": [{"name": "MI London (Men)"}]},
    {"Number": 2, "name": "Sunrisers Leeds (Men) Inning", "teams": [{"name": "Sunrisers Leeds (Men)"}]},
    {"Number": 3, "name": "1st Super Over", "isSuperOver": True, "teams": [{"name": "MI London (Men)"}]},
    {"Number": 4, "name": "1st Super Over", "isSuperOver": True, "teams": [{"name": "Sunrisers Leeds (Men)"}]},
], "players": [
    {"id": 52, "longName": "Sam Curran", "battingStyle": "LEFTHAND"},
    {"id": 71, "longName": "Brydon Carse", "bowlingStyle": "RIGHT_ARM_FAST"},
]}}}

STATUS = {"data": {"matchInfo": {"status": "MI London (Men) won (1st Super Over)"}}}


def balls(inning, overs, start=0):
    out = []
    for over in range(start, start + overs):
        for ball in range(1, 7):
            out.append({"id": f"{inning}-{over}-{ball}", "seqNo": (over - start) * 6 + ball,
                        "oversActual": f"{over}.{ball}", "text": f"{over}.{ball} delivery",
                        "inningNumber": inning, "batsmanPlayerId": 52, "bowlerPlayerId": 71,
                        "nonStrikerPlayerId": 52})
    return out


class Resp:
    def __init__(self, url, payload):
        self.url, self._payload = url, payload

    def json(self):
        return self._payload


class FakeSite:
    """Holds the shared state between the fake page, context and browser."""

    def __init__(self, views=None, feeds=None, initial_payload=None, chunk=6):
        self.views = views or []
        self.feeds = feeds or {}
        self.chunk = chunk
        self.next_data = json.dumps(initial_payload) if initial_payload is not None else None
        self.served = {}
        self.selected = []
        self.current = 0
        self.page = None
        self.api_calls = []

    def comments_for(self, url):
        query = url.split("?", 1)[1]
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        inning = int(params["inningNumber"])
        window = int(params["fromInningOver"])
        data = balls(inning, 3) if inning in (1, 2) else balls(inning, 1)
        kept = [b for b in data if sc.over_key(b["oversActual"])[0] <= window]
        kept = sorted(kept, key=lambda b: sc.over_key(b["oversActual"]), reverse=True)[: self.chunk * 2]
        return {"status": 0, "data": {"content": {"comments": kept}}}

    # --- browser-driven delivery for the crawler path ---
    def deliver(self):
        page = self.page
        if page is None or "response" not in page.handlers:
            return
        balls_in_view = self.feeds.get(self.current, [])
        idx = self.served.get(self.current, 0)
        self.served[self.current] = idx + 1
        remaining = len(balls_in_view) - idx * self.chunk
        if remaining <= 0:
            return
        chunk = balls_in_view[max(0, remaining - self.chunk): remaining]
        page.handlers["response"](Resp("https://hs-consumer-api.espncricinfo.com/v1/pages/match/comments?x",
                                       {"status": 0, "data": {"content": {"comments": chunk}}}))


class FakeOption:
    def __init__(self, site, position):
        self.site, self.position = site, position

    def inner_text(self):
        return f"{self.site.views[self.position]}\nsub"

    def click(self, force=False, timeout=None):
        self.site.selected.append(self.position)
        self.site.current = self.position
        self.site.deliver()


class FakeButton:
    def __init__(self, site, open_flag):
        self.site, self.open_flag = site, open_flag

    def count(self):
        return 1

    def inner_text(self):
        return self.site.views[self.site.current]

    def click(self, force=False, timeout=None):
        self.site.open = True


class FakeLocator:
    def __init__(self, site, selector):
        self.site, self.selector = site, selector

    @property
    def first(self):
        return FakeButton(self.site, None)

    def count(self):
        return 1 if self.selector == sc.DROPDOWN_BTN_SELECTOR else len(self.site.views)

    def all(self):
        if not self.site.open:
            return []
        return [FakeOption(self.site, i) for i in range(len(self.site.views))]


class FakePage:
    def __init__(self, site=None):
        self.site = site
        self.handlers = {}
        self.keyboard = type("K", (), {"press": lambda _self, key: (site.deliver() if key == "PageDown" and site else None)})()
        self.mouse = type("M", (), {"click": lambda _self, x, y: setattr(site, "open", False) if site else None})()
        self.context = None

    def route(self, pattern, handler):
        pass

    def on(self, event, handler):
        self.handlers[event] = handler

    def goto(self, url, **kwargs):
        return None

    def wait_for_timeout(self, ms):
        return None

    def evaluate(self, script, arg=None):
        if isinstance(script, str) and "__NEXT_DATA__" in script and self.site and self.site.next_data:
            data, self.site.next_data = self.site.next_data, None
            return data
        return None

    def locator(self, selector):
        return FakeLocator(self.site, selector)

    def close(self):
        return None


@contextlib.contextmanager
def installed(site, api_json):
    """Swap Playwright + the JSON transport for fakes."""
    real_pw = sc._require_playwright
    real_api = sc.api_get_json

    class Ctx:
        def __init__(self, page):
            self.page, self.request = page, None

        def new_page(self):
            # one shared page: scrape_match registers page.on("response") on whatever
            # new_page() returns, and the fake site delivers chunks through it
            return self.page

    class Browser:
        def __init__(self, page):
            self.page, self.closed = page, False

        def new_context(self, **kwargs):
            return Ctx(self.page)

        def close(self):
            self.closed = True

    page = FakePage(site)
    site.page = page
    browser = Browser(page)

    def sync_playwright():
        pw = type("PW", (), {"chromium": type("C", (), {"launch": staticmethod(lambda **kw: browser)})()})()

        @contextlib.contextmanager
        def cm():
            yield pw
        return cm()

    sc._require_playwright = lambda: sync_playwright
    sc.api_get_json = lambda _page, url, timeout_ms=25000: api_json(url, site)
    try:
        yield page
    finally:
        sc._require_playwright = real_pw
        sc.api_get_json = real_api


def api_serves_match_data(url, site):
    site.api_calls.append(url)
    if "scorecard" in url:
        return TIED_SCORECARD
    if "home" in url:
        return STATUS
    if "comments" in url:
        return site.comments_for(url)
    return None


def api_down(url, site):
    return None


class TestEndToEndOffline(unittest.TestCase):
    def setUp(self):
        self.out_dir = tempfile.mkdtemp(prefix="so_test_")

    def tearDown(self):
        for name in os.listdir(self.out_dir):
            os.remove(os.path.join(self.out_dir, name))
        os.rmdir(self.out_dir)

    # ---- A: the API path covers the super overs, so no scrolling happens ----------
    def test_api_path_captures_super_overs_without_crawling(self):
        site = FakeSite(views=["should not be used"], feeds={}, initial_payload=TIED_SCORECARD)
        out = os.path.join(self.out_dir, "1521231.csv")
        buf = io.StringIO()
        with installed(site, api_serves_match_data):
            with contextlib.redirect_stdout(buf):
                result = sc.scrape_match(MATCH_URL, out, headless=True, api_only=True)
        log = buf.getvalue()
        self.assertTrue(os.path.exists(out), "CSV not written")
        # 18 + 18 regular balls + 6 + 6 super-over balls
        self.assertEqual(result["rows"], 48)
        self.assertIn("Super Over", log)
        with open(out, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        header = lines[0].split(",")
        for col in ("inningType", "isSuperOver", "superOverNumber", "inningLabel"):
            self.assertIn(col, header)
        self.assertTrue(any("1st Super Over" in line for line in lines[1:]))
        self.assertIn("1st Super Over", lines[-1], "super-over rows should be the tail of the file")

    def test_api_only_still_tags_half_the_super_over_rows(self):
        site = FakeSite(views=["unused"], feeds={}, initial_payload=TIED_SCORECARD)
        out = os.path.join(self.out_dir, "1521231.csv")
        with installed(site, api_serves_match_data):
            with contextlib.redirect_stdout(io.StringIO()):
                sc.scrape_match(MATCH_URL, out, headless=True, api_only=True)
        import pandas as pd

        df = pd.read_csv(out)
        so = df[df["isSuperOver"]]
        self.assertEqual(sorted(so["inningNumber"].unique().tolist()), [3, 4])
        self.assertEqual(set(so["superOverNumber"]), {1})
        self.assertEqual(so["bowlerName"].dropna().unique().tolist(), ["Brydon Carse"])
        self.assertEqual(so["batsmanBattingStyle"].dropna().unique().tolist(), ["Left-hand"])
        self.assertEqual(len(df[df["inningNumber"] == 3]), 6)
        self.assertEqual(df["venue"].dropna().unique().tolist(), ["The Oval, London"])

    # ---- B: no API -> the crawler must find the super-over views ------------------
    def test_crawler_visits_super_over_views_when_api_is_down(self):
        views = ["MI London (Men), 1st Inning", "Sunrisers Leeds (Men), 2nd Inning",
                 "1st Super Over", "2nd Super Over", "Match Feedback"]
        feeds = {0: balls(1, 3), 1: balls(2, 3), 2: balls(3, 1), 3: balls(4, 1)}
        site = FakeSite(views=views, feeds=feeds, initial_payload=TIED_SCORECARD, chunk=6)
        out = os.path.join(self.out_dir, "1521231.csv")
        buf = io.StringIO()
        with installed(site, api_down):
            with contextlib.redirect_stdout(buf):
                result = sc.scrape_match(MATCH_URL, out, headless=True, use_api=True)
        log = buf.getvalue()
        self.assertIn("dropdown crawler", log)
        self.assertEqual(site.selected, [1, 2, 3], "every innings view after the default one must be visited")
        self.assertNotIn("Match Feedback", " ".join(str(v) for v in site.served), "non-innings entries are skipped")
        self.assertEqual(result["rows"], 48)
        import pandas as pd

        df = pd.read_csv(out)
        self.assertEqual(int(df["isSuperOver"].sum()), 12)
        self.assertEqual(df.loc[df["isSuperOver"], "inningType"].unique().tolist(), ["Super Over"])
        self.assertEqual(sorted(df.loc[df["isSuperOver"], "inningNumber"].unique().tolist()), [3, 4])

    def test_default_view_is_the_currently_rendered_innings(self):
        # position 0 is already on screen, so the crawler must not re-open it
        views = ["1st Inning", "2nd Inning", "Super Over"]
        feeds = {0: balls(1, 1), 1: balls(2, 1), 2: balls(3, 1)}
        site = FakeSite(views=views, feeds=feeds, initial_payload=TIED_SCORECARD, chunk=6)
        out = os.path.join(self.out_dir, "1521231.csv")
        with installed(site, api_down):
            with contextlib.redirect_stdout(io.StringIO()):
                sc.scrape_match(MATCH_URL, out, headless=True)
        self.assertEqual(site.selected, [1, 2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
