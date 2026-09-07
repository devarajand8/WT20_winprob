#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Parity tests: the single-file notebook version must behave exactly like the tested
`scrape_cricinfo_balls` module.

The notebook file is deliberately self-contained (paste one cell, run it), which means
its logic is a copy -- so every super-over-sensitive function is asserted against the
module's result on identical fixtures. If someone edits one file and not the other, this
fails rather than shipping two subtly different scrapers.

`AUTO_RUN` is neutralised before exec so importing the notebook source never launches a
browser.
"""

import contextlib
import io
import os
import re
import sys
import tempfile
import threading
import types
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import scrape_cricinfo_balls as module_sc  # noqa: E402

NOTEBOOK_PATH = os.path.join(REPO, "cricinfo_scraper_notebook.py")


def load_notebook_module():
    with open(NOTEBOOK_PATH, encoding="utf-8") as handle:
        source = handle.read()
    source = source.replace("\nif AUTO_RUN:", "\nif False:")   # don't auto-run on exec
    name = "cricinfo_scraper_notebook_under_test"
    mod = types.ModuleType(name)
    mod.__dict__["__file__"] = NOTEBOOK_PATH
    # exec into the module's own namespace so the functions' __globals__ is that dict --
    # otherwise monkeypatching `nb.api_get_json` here would not be visible to them
    exec(compile(source, NOTEBOOK_PATH, "exec"), mod.__dict__)
    sys.modules[name] = mod
    return mod


nb = load_notebook_module()

PAYLOADS = {
    "flag_on_innings": ({"data": {"scorecard": {"innings": [
        {"Number": 1, "name": "India Inning", "teams": [{"name": "India"}]},
        {"Number": 2, "name": "NZ Inning", "teams": [{"name": "New Zealand"}]},
        {"Number": 5, "name": "1st Super Over", "isSuperOver": True, "teams": [{"name": "NZ"}]},
        {"Number": 6, "name": "2nd Super Over", "isSuperOver": True, "teams": [{"name": "NZ"}]},
    ]}}}, ""),
    "legacy_superovers_key": ({"INNINGS": [
        {"Number": 1, "Name": "1st Inning", "teams": [{"name": "India"}]},
        {"Number": 2, "Name": "2nd Inning", "teams": [{"name": "Pakistan"}]},
    ], "superovers": [
        {"Number": 3, "Name": "Super Over", "teams": [{"name": "Pakistan"}]},
        {"Number": 4, "Name": "Super Over", "teams": [{"name": "India"}]},
    ]}, "Match tied"),
    "no_tiebreaker": ({"data": {"scorecard": {"innings": [
        {"Number": 1, "name": "MI London Inning", "teams": [{"name": "MI London"}]},
        {"Number": 2, "name": "Sunrisers Leeds Inning", "teams": [{"name": "Sunrisers Leeds"}]},
    ]}}}, "MI London won by 7 wickets"),
    "hidden_tiebreaker": ({"data": {"scorecard": {"innings": [
        {"Number": 1, "name": "India Inning", "teams": [{"name": "India"}]},
        {"Number": 2, "name": "Pakistan Inning", "teams": [{"name": "Pakistan"}]},
    ]}}}, "Match tied, India won in the Super Over"),
    "hundred_super_five": ({"innings": [
        {"Number": 1, "name": "Southern Brave Inning", "teams": [{"name": "Southern Brave"}]},
        {"Number": 2, "name": "Trent Rockets Inning", "teams": [{"name": "Trent Rockets"}]},
        {"Number": 3, "name": "Super 5", "teams": [{"name": "Trent Rockets"}]},
    ]}, ""),
}

URLS = [
    "https://www.espncricinfo.com/series/the-hundred-men-s-competition-2026-1521176/"
    "mi-london-men-vs-sunrisers-leeds-men-1st-match-1521231/ball-by-ball-commentary",
    "https://www.espncricinfo.com/series/8048/commentary/1423339/mi-vs-csk",
    "https://www.espncricinfo.com/series/xyz-1521176/a-vs-b-2nd-match-1521240/full-scorecard",
]


def plan_shape(plan):
    return [(p.number, bool(p.is_super_over), p.super_over_number, p.label) for p in plan]


class TestNotebookParity(unittest.TestCase):
    def test_file_exists_and_is_self_contained(self):
        with open(NOTEBOOK_PATH, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("import subprocess", source, "must not shell out to a worker script")
        self.assertNotIn("import argparse", source, "no CLI args: it is meant to be run as a cell")
        self.assertNotIn("import scrape_cricinfo_balls", source,
                         "must not import the sibling module -- it has to survive on its own")
        self.assertIn("def scrape_match(", source)

    def test_match_ids_agree(self):
        for url in URLS:
            with self.subTest(url=url):
                self.assertEqual(nb.extract_match_id(url), module_sc.extract_match_id(url))
                self.assertEqual(nb.extract_series_id(url), module_sc.extract_series_id(url))

    def test_innings_plan_agrees(self):
        for name, (payload, status) in PAYLOADS.items():
            with self.subTest(payload=name):
                a_plan, a_extra = module_sc.build_innings_plan([payload], status)
                b_plan, b_extra = nb.build_innings_plan([payload], status)
                self.assertEqual(plan_shape(a_plan), plan_shape(b_plan))
                self.assertEqual(a_extra, b_extra)

    def test_super_over_flagging_matches_expectations(self):
        expected = {"flag_on_innings": [5, 6], "legacy_superovers_key": [3, 4],
                    "no_tiebreaker": [], "hidden_tiebreaker": [], "hundred_super_five": [3]}
        for name, (payload, status) in PAYLOADS.items():
            with self.subTest(payload=name):
                for mod in (module_sc, nb):
                    plan, extra = mod.build_innings_plan([payload], status)
                    self.assertEqual([p.number for p in plan if p.is_super_over], expected[name])
                    self.assertEqual(bool(extra), name == "hidden_tiebreaker",
                                     "only a hinted-but-missing tie-breaker should trigger probing")

    def test_field_helpers_agree(self):
        for value in ("RIGHT_ARM_MEDIUM_FAST", "LEFT_ARM_ORTHODOX", "Right Arm", "LEFTHAND",
                      "Right-hand Bat", ["RIGHT_ARM_FAST", "MEDIUM"], None, ""):
            with self.subTest(value=value):
                self.assertEqual(nb.clean_style_str(value), module_sc.clean_style_str(value))
        for text in ("10.1", "2.5", "0.6", None, 7, "null"):
            self.assertEqual(nb.over_key(text), module_sc.over_key(text))
        for fmt in ("TEST", "TWENTY20", "ODI", ""):
            self.assertEqual(nb.start_over_for_format(fmt), module_sc.start_over_for_format(fmt))

    def test_crawl_decision_agrees(self):
        plan = [module_sc.InningsPlan(number=1), module_sc.InningsPlan(number=2),
                module_sc.InningsPlan(number=3, is_super_over=True, super_over_number=1)]
        nb_plan = [nb.InningsPlan(1), nb.InningsPlan(2), nb.InningsPlan(3, is_super_over=True, super_over_number=1)]
        balls = [{"id": f"a{i}", "oversActual": f"{i // 6}.{i % 6 + 1}", "inningNumber": 1 if i < 12 else 2}
                 for i in range(24)]
        self.assertEqual(module_sc.crawl_needed(plan, balls, [], True),
                         nb.crawl_needed(nb_plan, balls, [], True))
        self.assertEqual(module_sc.crawl_needed(plan, balls, [], False),
                         nb.crawl_needed(nb_plan, balls, [], False))

    def test_output_frame_agrees(self):
        payload, _ = PAYLOADS["flag_on_innings"]
        balls = []
        for inning, overs in ((1, 2), (2, 2), (5, 1), (6, 1)):
            for over in range(overs):
                for ball in range(1, 7):
                    balls.append({"id": f"{inning}-{over}-{ball}", "seqNo": over * 6 + ball,
                                  "oversActual": f"{over}.{ball}", "text": "d", "run": 1,
                                  "batsmanPlayerId": 52, "bowlerPlayerId": 71, "inningNumber": inning})
        meta = {"tournamentName": "Series", "matchName": "A vs B", "venue": "Oval"}
        players_a, players_b = {}, {}
        module_sc.find_players_anywhere(payload, players_a)
        nb.find_players_anywhere(payload, players_b)
        self.assertEqual(players_a, players_b)

        df_a = module_sc.build_output_frame(balls, players_a, dict(meta),
                                            module_sc.build_innings_plan([payload])[0])
        df_b = nb.build_output_frame(balls, players_b, dict(meta), nb.build_innings_plan([payload])[0])
        self.assertEqual(list(df_a.columns), list(df_b.columns))
        self.assertEqual(df_a.shape, df_b.shape)
        self.assertEqual(df_a["isSuperOver"].tolist(), df_b["isSuperOver"].tolist())
        self.assertEqual(df_a["inningType"].tolist(), df_b["inningType"].tolist())
        self.assertEqual(df_a["oversActual"].tolist(), df_b["oversActual"].tolist())
        self.assertEqual(df_a["bowlerBowlingStyle"].dropna().unique().tolist(),
                         df_b["bowlerBowlingStyle"].dropna().unique().tolist())

    def test_pagination_agrees(self):
        def make_transport(mod):
            def fake(_page, url, timeout_ms=25000):
                params = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&") if "=" in p)
                inning, window = int(params["inningNumber"]), int(params["fromInningOver"])
                total = 3 if inning == 1 else 1
                out = [{"id": f"{inning}-{o}-{b}", "seqNo": o * 6 + b, "oversActual": f"{o}.{b}",
                        "text": "d", "inningNumber": inning}
                       for o in range(total) for b in range(1, 7)]
                kept = [x for x in out if mod.over_key(x["oversActual"])[0] <= window]
                kept = sorted(kept, key=lambda x: mod.over_key(x["oversActual"]), reverse=True)[:12]
                return {"data": {"content": {"comments": kept}}}
            return fake

        results = []
        for mod in (module_sc, nb):
            real = mod.api_get_json
            mod.api_get_json = make_transport(mod)
            try:
                results.append([sorted(b["id"] for b in mod.fetch_innings_balls(None, "1", "2", 1, 26)[0]),
                                sorted(b["id"] for b in mod.fetch_innings_balls(None, "1", "2", 3, 4)[0])])
            finally:
                mod.api_get_json = real
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(results[0][0]), 18)
        self.assertEqual(len(results[0][1]), 6)

    def test_importing_the_file_does_not_start_a_scrape(self):
        # `import cricinfo_scraper_notebook as cric` must only define things; only a
        # pasted cell / %run (__name__ == "__main__") should kick off the queue
        src = open(NOTEBOOK_PATH, encoding="utf-8").read()
        self.assertIn('if AUTO_RUN and __name__ == "__main__":', src)
        self.assertNotIn("if AUTO_RUN:\n", src)
        ran = []
        ns = {"__name__": "cricinfo_scraper_notebook_imported", "__file__": NOTEBOOK_PATH,
              "__builtins__": __builtins__}
        # a scrape would need Playwright; the guard means we never get that far
        exec(compile(re.sub(r"^from __future__ import annotations$", "", src, flags=re.M), NOTEBOOK_PATH, "exec"), ns)
        ns["AUTO_RUN"] = True
        ran.append(ns.get("combined_df", "not set -> correct"))
        self.assertEqual(ran[-1], "not set -> correct")

    def test_notebook_defaults_are_notebook_friendly(self):
        self.assertTrue(hasattr(nb, "MATCH_URLS") and isinstance(nb.MATCH_URLS, list))
        self.assertTrue(hasattr(nb, "scrape_match") and hasattr(nb, "scrape_many"))
        self.assertIsNone(nb.HEADLESS, "auto-detect display so headless servers work out of the box")
        self.assertFalse(nb.API_ONLY, "the dropdown crawler is the safety net for super overs")
        import inspect
        sig = inspect.signature(nb.scrape_match)
        self.assertEqual(sig.parameters["output_dir"].default, None)
        self.assertEqual(nb._run_off_loop(lambda: 42), 42, "sync helper must work in a plain thread")


if __name__ == "__main__":
    unittest.main(verbosity=2)


# --------------------------------------------------------------------------------------
# The notebook file, driven end to end with the same fake browser used by the CLI test
# --------------------------------------------------------------------------------------
from test_end_to_end_offline import (  # noqa: E402
    MATCH_URL, TIED_SCORECARD, FakePage, FakeSite, api_serves_match_data,
)


@contextlib.contextmanager
def installed_in_notebook(site, api_json):
    real_import, real_api = nb._import_playwright, nb.api_get_json
    page = FakePage(site)
    site.page = page
    browser = type("Browser", (), {
        "new_context": lambda self, **kw: type("Ctx", (), {
            "new_page": lambda s: s.page, "page": page, "request": None})(),
        "close": lambda self: None,
    })()

    def sync_playwright():
        pw = type("PW", (), {"chromium": type("C", (), {"launch": staticmethod(lambda **kw: browser)})()})()

        @contextlib.contextmanager
        def cm():
            yield pw
        return cm()

    nb._import_playwright = lambda: sync_playwright
    nb.api_get_json = lambda _p, url, timeout_ms=25000: api_json(url, site)
    try:
        yield page
    finally:
        nb._import_playwright, nb.api_get_json = real_import, real_api


class TestNotebookEndToEnd(unittest.TestCase):
    def test_scrape_match_returns_dataframe_with_super_overs(self):
        site = FakeSite(views=["unused"], feeds={}, initial_payload=TIED_SCORECARD)
        with tempfile.TemporaryDirectory() as tmp:
            with installed_in_notebook(site, api_serves_match_data):
                with contextlib.redirect_stdout(io.StringIO()):
                    df = nb.scrape_match(MATCH_URL, output_dir=tmp)
            self.assertEqual(os.listdir(tmp), ["1521231.csv"], "CSV name must follow <matchId>.csv")
            self.assertEqual(len(df), 48)
            self.assertEqual(int(df["isSuperOver"].sum()), 12)
            self.assertEqual(sorted(df.loc[df["isSuperOver"], "inningNumber"].unique().tolist()), [3, 4])
            self.assertEqual(set(df.loc[df["isSuperOver"], "inningType"]), {"Super Over"})
            self.assertEqual(set(df.loc[df["isSuperOver"], "superOverNumber"]), {1})
            self.assertEqual(df["bowlerName"].dropna().unique().tolist(), ["Brydon Carse"])
            self.assertEqual(df["venue"].dropna().unique().tolist(), ["The Oval, London"])
            # last statement of a notebook cell renders the frame; breakdown is printable text
            text = nb.innings_breakdown(df)
            self.assertIn("SUPER OVER", text)
            self.assertIn("12 of them in 1 super over(s)", text)

    def test_scrape_many_queues_matches(self):
        site = FakeSite(views=["unused"], feeds={}, initial_payload=TIED_SCORECARD)
        with tempfile.TemporaryDirectory():
            repo_before = set(os.listdir(REPO))
            with installed_in_notebook(site, api_serves_match_data):
                with contextlib.redirect_stdout(io.StringIO()):
                    # output_dir="" -> DataFrame only: a notebook run must not drop CSVs in cwd
                    combined = nb.scrape_many([MATCH_URL], output_dir="")
            self.assertEqual(len(combined), 48)
            self.assertEqual(len(combined.attrs["per_match"]), 1)
            self.assertEqual(set(os.listdir(REPO)), repo_before, "no stray files written to the repo")

    def test_output_dir_writes_next_to_the_notebook(self):
        site = FakeSite(views=["unused"], feeds={}, initial_payload=TIED_SCORECARD)
        with tempfile.TemporaryDirectory() as tmp:
            with installed_in_notebook(site, api_serves_match_data):
                with contextlib.redirect_stdout(io.StringIO()):
                    nb.OUTPUT_DIR = tmp          # same as editing OUTPUT_DIR in the config block
                    try:
                        nb.scrape_many([MATCH_URL])
                    finally:
                        nb.OUTPUT_DIR = None
            self.assertIn("1521231.csv", os.listdir(tmp))

    def test_headless_flag_defaults_to_auto(self):
        seen = {}
        real_impl = nb._scrape_match_impl
        nb._scrape_match_impl = lambda url, out, headless, *a, **k: seen.setdefault("headless", headless) or None
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                nb.scrape_match(MATCH_URL, output_dir=None, headless=False)
            self.assertIs(seen["headless"], False)
        finally:
            nb._scrape_match_impl = real_impl


class TestNotebookEventLoopGuard(unittest.TestCase):
    def test_hops_to_a_worker_thread_when_a_loop_is_running(self):
        """The whole point of `_run_off_loop`: Playwright's sync API refuses to start
        while this thread has a running asyncio loop, which some kernels do."""
        import asyncio

        async def probe():
            return nb._run_off_loop(lambda: threading.current_thread().name)

        thread_used = asyncio.run(probe())
        self.assertTrue(thread_used.startswith("playwright"),
                        f"expected a dedicated thread, ran on {thread_used!r}")
        self.assertEqual(nb._run_off_loop(lambda: threading.current_thread().name),
                         threading.current_thread().name, "no loop running -> stay on the main thread")

    def test_result_and_exceptions_pass_through(self):
        import asyncio

        async def boom():
            return nb._run_off_loop(self._raise)

        self.assertEqual(nb._run_off_loop(lambda: 7), 7)
        with self.assertRaises(RuntimeError):
            asyncio.run(boom())

    @staticmethod
    def _raise():
        raise RuntimeError("boom from the worker thread")
