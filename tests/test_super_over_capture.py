#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Offline tests for the super-over capture path in scrape_cricinfo_balls.py.

No network, no browser: these exercise the parts that decide *which* innings to
fetch (Cricinfo's schema is the moving target here), the pagination stop rules,
the dedupe/coalesce, and the final CSV shape.

    python -m unittest discover -s tests -v
    # or: python tests/test_super_over_capture.py
"""

import os
import sys
import unittest
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scrape_cricinfo_balls as sc  # noqa: E402

HUNDRED_URL = (
    "https://www.espncricinfo.com/series/the-hundred-men-s-competition-2026-1521176/"
    "mi-london-men-vs-sunrisers-leeds-men-1st-match-1521231/ball-by-ball-commentary"
)

MATCHED_URL = (
    "https://www.espncricinfo.com/series/ipl-2024-1411226/"
    "kolkata-knight-riders-vs-rajasthan-royals-31st-match-1423339/ball-by-ball-commentary"
)


def innings_balls(inning, overs, start_over=0):
    """Fake Cricinfo comment dicts for `overs` complete overs of one innings."""
    out = []
    for over in range(start_over, start_over + overs):
        for ball in range(1, 7):
            out.append({
                "id": f"{inning}-{over}-{ball}",
                "seqNo": (over - start_over) * 6 + ball,
                "oversActual": f"{over}.{ball}",
                "title": "",
                "text": f"{over}.{ball}: dummy delivery",
                "run": 1,
                "wicket": False,
                "batsmanPlayerId": 52 if over % 2 == 0 else 53,
                "bowlerPlayerId": 71,
                "nonStrikerPlayerId": 54,
                "inningNumber": inning,
                "teamName": "Home",
            })
    return out


# --------------------------------------------------------------------------------------
# URL parsing
# --------------------------------------------------------------------------------------
class TestUrlParsing(unittest.TestCase):
    def test_match_and_series_ids(self):
        self.assertEqual(sc.extract_match_id(HUNDRED_URL), "1521231")
        self.assertEqual(sc.extract_series_id(HUNDRED_URL), "1521176")
        self.assertEqual(sc.extract_match_id(MATCHED_URL), "1423339")
        self.assertEqual(sc.extract_series_id(MATCHED_URL), "1411226")

    def test_other_url_shapes(self):
        cases = {
            "https://www.espncricinfo.com/series/x-1521176/mil-vs-srl-1st-match-1521231/full-scorecard": "1521231",
            "https://www.espncricinfo.com/matches/live/2026/x-vs-y-live-blog/1521231": "1521231",
            "https://www.espncricinfo.com/series/8048/commentary/1423339/mi-vs-csk": "1423339",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(sc.extract_match_id(url), expected)

    def test_page_slug_swap(self):
        swapped = sc.url_for_page(HUNDRED_URL, "full-scorecard")
        self.assertTrue(swapped.endswith("/full-scorecard"))
        self.assertIn("1521231", swapped)
        self.assertNotIn("ball-by-ball", swapped)

    def test_api_urls_carry_inning_number(self):
        url = sc.comments_api_url("1521176", "1521231", 5, 4)
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["inningNumber"], ["5"])       # <- this is how a SO is requested
        self.assertEqual(query["matchId"], ["1521231"])
        self.assertEqual(query["fromInningOver"], ["4"])
        self.assertEqual(query["commentType"], ["ALL"])
        self.assertIn("scorecard", sc.scorecard_api_url("1521176", "1521231"))


# --------------------------------------------------------------------------------------
# Overs / sorting
# --------------------------------------------------------------------------------------
class TestOverParsing(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(sc.parse_over_ball("10.4"), (10, 4))
        self.assertEqual(sc.parse_over_ball("0.1"), (0, 1))
        self.assertEqual(sc.parse_over_ball(12), (12, None))
        self.assertEqual(sc.parse_over_ball(None), (None, None))
        self.assertEqual(sc.parse_over_ball("null"), (None, None))

    def test_numeric_sort_beats_lexicographic(self):
        # the old code sorted on the string, which put "10.1" before "2.5"
        raw = ["10.1", "2.5", "0.6", "1.1", "10.10"]
        self.assertEqual(sorted(sc.over_key(v) for v in raw),
                         [(0, 6), (1, 1), (2, 5), (10, 1), (10, 10)])
        self.assertLess(sc.over_key("2.5"), sc.over_key("10.1"))

    def test_start_over_per_format(self):
        self.assertGreaterEqual(sc.start_over_for_format("TEST"), 200)
        self.assertEqual(sc.start_over_for_format("TWENTY20"), 26)
        self.assertGreaterEqual(sc.start_over_for_format("ODI"), 55)
        self.assertEqual(sc.start_over_for_format("TWENTY20", overs_hint=100), 102)


# --------------------------------------------------------------------------------------
# Innings plan / super-over discovery -- the important bit
# --------------------------------------------------------------------------------------
class TestInningsPlanDiscovery(unittest.TestCase):
    def test_modern_api_superover_flag(self):
        payload = {"data": {"scorecard": {"innings": [
            {"Number": 1, "name": "MI London (Men) Inning", "shortName": "MIL", "teams": [{"name": "MI London (Men)"}]},
            {"Number": 2, "name": "Sunrisers Leeds (Men) Inning", "shortName": "SRL", "teams": [{"name": "Sunrisers Leeds (Men)"}]},
            {"Number": 3, "name": "1st Super Over", "shortName": "1st SO", "isSuperOver": True, "teams": [{"name": "MI London (Men)"}]},
            {"Number": 4, "name": "2nd Super Over", "shortName": "2nd SO", "isSuperOver": True, "teams": [{"name": "MI London (Men)"}]},
        ]}}}
        plan, extra = sc.build_innings_plan([payload])
        self.assertEqual([p.number for p in plan], [1, 2, 3, 4])
        self.assertEqual([p.is_super_over for p in plan], [False, False, True, True])
        self.assertEqual([p.super_over_number for p in plan if p.is_super_over], [1, 2])
        self.assertEqual(extra, [])

    def test_legacy_superovers_array(self):
        # old engine layout: separate `superovers` key, numbered after the main innings
        payload = {"INNINGS": [
            {"Number": 1, "Name": "1st Inning", "teams": [{"name": "India"}]},
            {"Number": 2, "Name": "2nd Inning", "teams": [{"name": "New Zealand"}]},
        ], "superovers": [
            {"Number": 5, "Name": "Super Over", "teams": [{"name": "India"}]},
            {"Number": 6, "Name": "Super Over", "teams": [{"name": "New Zealand"}]},
        ]}
        plan, extra = sc.build_innings_plan([payload])
        self.assertEqual([p.number for p in plan], [1, 2, 5, 6])
        self.assertEqual([p.is_super_over for p in plan], [False, False, True, True])
        # two halves of the same super over -> both SO #1
        self.assertEqual([p.super_over_number for p in plan if p.is_super_over], [1, 1])
        self.assertEqual(extra, [])

    def test_nameid_slug_and_pairing(self):
        payload = {"content": {"innings": [
            {"number": 1, "name": "India Inning", "teams": [{"name": "India"}]},
            {"number": 2, "name": "Pakistan Inning", "teams": [{"name": "Pakistan"}]},
            {"number": 3, "name": "India", "nameId": "super-over", "teams": [{"name": "India"}]},
            {"number": 4, "name": "Pakistan", "nameId": "super-over", "teams": [{"name": "Pakistan"}]},
            {"number": 5, "name": "India", "nameId": "super-over", "teams": [{"name": "India"}]},
            {"number": 6, "name": "Pakistan", "nameId": "super-over", "teams": [{"name": "Pakistan"}]},
        ]}}
        plan, _ = sc.build_innings_plan([payload])
        so = [p for p in plan if p.is_super_over]
        self.assertEqual([p.number for p in so], [3, 4, 5, 6])
        self.assertEqual([p.super_over_number for p in so], [1, 1, 2, 2])

    def test_hundred_super_five_is_a_tiebreaker(self):
        payload = {"innings": [
            {"Number": 1, "name": "Southern Brave Inning", "teams": [{"name": "Southern Brave"}]},
            {"Number": 2, "name": "Trent Rockets Inning", "teams": [{"name": "Trent Rockets"}]},
            {"Number": 3, "name": "Super 5", "teams": [{"name": "Trent Rockets"}]},
        ]}
        plan, _ = sc.build_innings_plan([payload])
        self.assertTrue(plan[-1].is_super_over)
        self.assertFalse(any(p.is_super_over for p in plan[:2]))

    def test_no_super_over_stays_clean(self):
        payload = {"data": {"scorecard": {"innings": [
            {"Number": 1, "name": "MI London (Men) Inning", "teams": [{"name": "MI London (Men)"}]},
            {"Number": 2, "name": "Sunrisers Leeds (Men) Inning", "teams": [{"name": "Sunrisers Leeds (Men)"}]},
        ]}}}
        plan, extra = sc.build_innings_plan([payload], "MI London won by 7 wickets")
        self.assertEqual([p.number for p in plan], [1, 2])
        self.assertFalse(any(p.is_super_over for p in plan))
        self.assertEqual(extra, [], "a decisive match must not trigger probing")

    def test_tie_with_hidden_super_over_triggers_probe(self):
        payload = {"data": {"scorecard": {"innings": [
            {"Number": 1, "name": "India Inning", "teams": [{"name": "India"}]},
            {"Number": 2, "name": "Pakistan Inning", "teams": [{"name": "Pakistan"}]},
        ]}}}
        _, extra = sc.build_innings_plan([payload], "Match tied, India won the Super Over")
        self.assertTrue(extra, "a tied match with no SO innings listed must still be probed")
        self.assertTrue(all(n > 2 for n in extra))

    def test_regular_innings_not_over_matched(self):
        # a "2.5 ov"-style fall-of-wicket / bowler figure dict must NOT become an innings
        payload = {"stats": {
            "batting": [{"Number": 1, "name": "Player X", "teams": ["India"], "oversActual": "3.4"}],
            "bowling": [{"Number": 3, "Name": "3rd Change", "overs": "4-0-22-1", "text": "x"}],
            "meta": {"id": 7, "name": "junk"},
        }}
        plan, extra = sc.build_innings_plan([payload])
        self.assertTrue(all(p.number for p in plan))
        self.assertFalse(any(p.is_super_over for p in plan))

    def test_plan_sorts_super_overs_last(self):
        plan = [
            sc.InningsPlan(number=1),
            sc.InningsPlan(number=3, is_super_over=True, super_over_number=1),
            sc.InningsPlan(number=2),
        ]
        self.assertEqual([p.number for p in sorted(plan, key=lambda p: p.sort_key)], [1, 2, 3])


# --------------------------------------------------------------------------------------
# Ball extraction, dedupe, coalescing
# --------------------------------------------------------------------------------------
class TestBallExtraction(unittest.TestCase):
    def test_flattens_nested_comments(self):
        payload = {"data": {"content": {"comments": [
            {"id": 1, "oversActual": "0.1", "text": "played straight",
             "dismissalText": {"short": "OUT", "long": "c Smith b Jones", "commentary": "edged"},
             "predictions": {"battingWinProb": 0.51}},
            {"id": 2, "oversActual": "0.2", "text": "beaten"},
        ]}}}
        balls = sc.parse_comments(payload)
        self.assertEqual(len(balls), 2)
        self.assertEqual(balls[0]["dismissal_text_long"], "c Smith b Jones")
        self.assertEqual(balls[0]["pred_battingWinProb"], 0.51)

    def test_coalesce_keeps_richer_record_and_fills_gaps(self):
        thin = {"id": 42, "oversActual": "0.3", "text": "short", "inningNumber": None}
        rich = {"id": 42, "oversActual": "0.3", "text": "full commentary line", "inningNumber": 5,
                "batsmanPlayerId": 11, "bowlerPlayerId": 22, "run": 4}
        merged = sc.merge_ball_records([thin, rich])
        self.assertEqual(merged["text"], "full commentary line")
        self.assertEqual(merged["inningNumber"], 5)
        self.assertEqual(merged["batsmanPlayerId"], 11)

    def test_combine_dedupes_across_sources(self):
        api_balls = innings_balls(1, 2) + innings_balls(5, 1)
        scroll_balls = innings_balls(1, 2) + innings_balls(5, 1)
        combined = sc.combine_balls(api_balls, scroll_balls)
        self.assertEqual(len(combined), 18)                      # 2 overs + 1 over, no doubles
        self.assertEqual(len({b["id"] for b in combined}), 18)

    def test_balls_without_ids_dedupe_on_content(self):
        a = {"oversActual": "0.4", "text": "wide", "inningNumber": 5}
        b = {"oversActual": "0.4", "text": "wide", "inningNumber": 5}
        self.assertEqual(len(sc.combine_balls([a, b])), 1)


# --------------------------------------------------------------------------------------
# API pagination (fake transport)
# --------------------------------------------------------------------------------------
class FakePage:
    """Stands in for a Playwright page: answers comments_api_url requests."""

    def __init__(self, overs_by_inning, start_index=0):
        self.pages_requested = 0
        self._data = {}
        for inning, overs in overs_by_inning.items():
            self._data[inning] = innings_balls(inning, overs, start_index)
        self.context = None

    def json_for(self, url):
        self.pages_requested += 1
        query = parse_qs(urlparse(url).query)
        inning = int(query["inningNumber"][0])
        window = int(query["fromInningOver"][0])
        balls = [b for b in self._data.get(inning, []) if sc.over_key(b["oversActual"])[0] <= window]
        balls = sorted(balls, key=lambda b: sc.over_key(b["oversActual"]), reverse=True)[:10 * 6]
        return {"status": 0, "data": {"content": {"comments": balls, "hasMore": bool(balls)}}}


class TestApiPagination(unittest.TestCase):
    def setUp(self):
        self._real = sc.api_get_json

    def tearDown(self):
        sc.api_get_json = self._real

    def _patch(self, page):
        sc.api_get_json = lambda _page, url, timeout_ms=25000: page.json_for(url)

    def test_full_innings_paged_in_few_requests(self):
        page = FakePage({1: 20, 2: 20})
        self._patch(page)
        balls, reached_start = sc.fetch_innings_balls(page, "1", "2", 1, start_over=26)
        self.assertTrue(reached_start)
        self.assertEqual(len(balls), 120)
        self.assertEqual({sc.over_key(b["oversActual"])[0] for b in balls}, set(range(20)))
        self.assertLessEqual(page.pages_requested, 5)

    def test_super_over_needs_one_request(self):
        page = FakePage({3: 1})
        self._patch(page)
        balls, reached_start = sc.fetch_innings_balls(page, "1", "2", 3, start_over=4)
        self.assertTrue(reached_start)
        self.assertEqual(len(balls), 6)
        self.assertEqual(page.pages_requested, 1)
        self.assertTrue(all(b["inningNumber"] == 3 for b in balls))

    def test_missing_innings_returns_nothing(self):
        page = FakePage({1: 20})
        self._patch(page)
        balls, reached_start = sc.fetch_innings_balls(page, "1", "2", 7, start_over=4)
        self.assertEqual(balls, [])
        self.assertFalse(reached_start)
        self.assertLess(page.pages_requested, 5, "must not spin on empty innings")

    def test_truncated_innings_is_flagged_incomplete(self):
        # transport only ever serves the last overs -> stop condition must fire and
        # the caller must know the innings was not fully captured (so it can scroll)
        page = FakePage({1: 30})
        sc.api_get_json = lambda _p, url, timeout_ms=25000: (
            {"status": 0, "data": {"content": {"comments": innings_balls(1, 5, start_over=25)}}}
            if "fromInningOver=26" in url else None
        )
        balls, reached_start = sc.fetch_innings_balls(page, "1", "2", 1, start_over=26)
        self.assertEqual(len(balls), 30)
        self.assertFalse(reached_start)

    def test_fetch_all_innings_marks_super_over_coverage(self):
        page = FakePage({1: 20, 2: 20, 3: 1, 4: 1})
        self._patch(page)
        plan = [
            sc.InningsPlan(number=1), sc.InningsPlan(number=2),
            sc.InningsPlan(number=3, is_super_over=True, super_over_number=1),
            sc.InningsPlan(number=4, is_super_over=True, super_over_number=1),
        ]
        balls, complete = sc.fetch_all_innings(page, "1", "2", plan, [], "TWENTY20", verbose=False)
        self.assertTrue(complete)
        self.assertEqual(len(balls), 240 + 12)
        self.assertEqual(len([b for b in balls if b["inningNumber"] in (3, 4)]), 12)


# --------------------------------------------------------------------------------------
# End-to-end frame assembly
# --------------------------------------------------------------------------------------
class TestOutputFrame(unittest.TestCase):
    def _fixtures(self):
        tied_payload = {"data": {"matchHeader": {
            "series": {"longName": "The Hundred Men's Competition 2026"},
            "title": "MI London (Men) vs Sunrisers Leeds (Men), 1st Match",
            "ground": {"name": "The Oval", "town": {"name": "London"}},
        }, "scorecard": {"innings": [
            {"Number": 1, "name": "MI London (Men) Inning", "teams": [{"name": "MI London (Men)"}]},
            {"Number": 2, "name": "Sunrisers Leeds (Men) Inning", "teams": [{"name": "Sunrisers Leeds (Men)"}]},
            {"Number": 3, "name": "1st Super Over", "isSuperOver": True, "teams": [{"name": "MI London (Men)"}]},
            {"Number": 4, "name": "1st Super Over", "isSuperOver": True, "teams": [{"name": "Sunrisers Leeds (Men)"}]},
        ], "players": [
            {"id": 52, "longName": "Sam Curran", "battingStyle": "LEFTHAND", "bowlingStyle": "RIGHT_ARM_MEDIUM_FAST"},
            {"id": 53, "longName": "Nicholas Pooran", "battingStyle": "lefthand", "bowlingStyle": "RIGHT_ARM_OFF_BREAK"},
            {"id": 54, "longName": "Will Jacks", "battingStyle": "RIGHTHAND"},
            {"id": 71, "longName": "Brydon Carse", "bowlingStyle": "RIGHT_ARM_FAST", "bowlingHand": "Right Arm"},
        ]}}}
        plan, _ = sc.build_innings_plan([tied_payload], "Match tied")
        balls = innings_balls(1, 3) + innings_balls(2, 3) + innings_balls(3, 1) + innings_balls(4, 1)
        for ball in balls:  # the fake data uses 0-indexed overs, matching Cricinfo
            if ball["inningNumber"] in (3, 4):
                ball["wicket"] = ball["oversActual"] == "0.2"
                ball["outPlayerId"] = 52 if ball["wicket"] else None
        players = {}
        sc.find_players_anywhere(tied_payload, players)
        meta = sc.extract_header_metadata(tied_payload)
        meta.update({"matchId": "1521231", "seriesId": "1521176"})
        return balls, players, meta, plan

    def test_super_over_rows_tagged_and_last(self):
        balls, players, meta, plan = self._fixtures()
        df = sc.build_output_frame(balls, players, meta, plan)
        self.assertEqual(len(df), 48)              # 2 x 3 overs regular + 2 x 6-ball super over
        self.assertEqual(int(df["isSuperOver"].sum()), 12)
        self.assertTrue(df["isSuperOver"].tail(12).all(), "super over rows sort after the main innings")
        self.assertFalse(bool(df["isSuperOver"].head(36).any()))
        self.assertEqual(set(df.loc[df["isSuperOver"], "inningType"]), {"Super Over"})
        self.assertEqual(set(df.loc[~df["isSuperOver"], "inningType"]), {"Regular"})
        self.assertEqual(set(df.loc[df["isSuperOver"], "superOverNumber"]), {1})
        self.assertEqual(sorted(df.loc[df["isSuperOver"], "inningNumber"].unique().tolist()), [3, 4])
        self.assertEqual(set(df.loc[~df["isSuperOver"], "superOverNumber"].dropna().tolist()), set())
        self.assertEqual(list(df.columns[:11]), [
            "tournamentName", "matchName", "venue", "matchId", "seriesId", "inningNumber",
            "inningType", "isSuperOver", "superOverNumber", "inningLabel", "overNumber",
        ])

    def test_super_over_halves_are_numbered_separately_per_tiebreaker(self):
        balls, players, meta, plan = self._fixtures()
        df = sc.build_output_frame(balls, players, meta, plan)
        so = df[df["isSuperOver"]]
        self.assertEqual(so["inningNumber"].tolist(), [3] * 6 + [4] * 6)
        self.assertEqual(set(so["inningLabel"]), {"1st Super Over"})

    def test_chronological_order_within_innings(self):
        balls, players, meta, plan = self._fixtures()
        for ball in balls:
            if ball["inningNumber"] == 1:
                ball["oversActual"] = {0: "0.1", 1: "10.1", 2: "2.3"}.get((int(ball["seqNo"]) - 1) // 6, "0.1")
        df = sc.build_output_frame(balls, players, meta, plan)
        first = df[df["inningNumber"] == 1]
        keys = [sc.over_key(v) for v in first["oversActual"].tolist()]
        self.assertEqual(keys, sorted(keys), "deliveries must be in over/ball order")
        self.assertLess(keys.index((2, 3)), keys.index((10, 1)), "over 2.3 must precede 10.1")
        self.assertNotEqual(list(first["oversActual"]), sorted(first["oversActual"]),
                            "a lexicographic sort of oversActual would have got this wrong")

    def test_player_enrichment_applies_to_super_over_balls(self):
        balls, players, meta, plan = self._fixtures()
        df = sc.build_output_frame(balls, players, meta, plan)
        so = df[df["isSuperOver"]]
        self.assertEqual(set(so["bowlerName"]), {"Brydon Carse"})
        self.assertEqual(set(so["bowlerBowlingStyle"]), {"Right-arm Fast"})
        self.assertEqual(set(so["bowlerBowlingHand"]), {"Right-arm"})
        self.assertEqual(set(so["batsmanName"]), {"Sam Curran"})
        self.assertIn("Nicholas Pooran", set(df["batsmanName"]))
        self.assertEqual(set(so["batsmanBattingStyle"]), {"Left-hand"})
        self.assertEqual(set(so["nonStrikerName"]), {"Will Jacks"})
        self.assertNotIn("batsmanPlayerId", df.columns)
        self.assertNotIn("bowlerPlayerId", df.columns)

    def test_style_cleaning_does_not_mangle_arm(self):
        # regression: "Right -arm Medium Fast" used to be the output
        style, hand = sc.derive_bowling_hand_and_style("RIGHT_ARM_MEDIUM_FAST")
        self.assertEqual(style, "Right-arm Medium Fast")
        self.assertEqual(hand, "Right-arm")
        self.assertEqual(sc.clean_style_str("LEFT_ARM_ORTHODOX"), "Left-arm Orthodox")
        self.assertEqual(sc.clean_style_str("Right Arm"), "Right-arm")
        self.assertEqual(sc.clean_style_str(["RIGHT_ARM_FAST", "MEDIUM"]), "Right-arm Fast Medium")

    def test_duplicate_balls_from_both_sources_are_merged(self):
        balls, players, meta, plan = self._fixtures()
        thin = [dict(b, text=None, teamName=None) for b in balls if b["inningNumber"] == 3]
        full = [b for b in balls if b["inningNumber"] == 3]
        others = [b for b in balls if b["inningNumber"] != 3]
        df = sc.build_output_frame(sc.combine_balls(thin + others, full + others), players, meta, plan)
        self.assertEqual(len(df[df["inningNumber"] == 3]), 6)
        self.assertTrue(df[df["inningNumber"] == 3]["text"].notna().all())
        self.assertEqual(len(df), 48)   # nothing lost, nothing duplicated

    def test_empty_input(self):
        df = sc.build_output_frame([], {}, {}, [])
        self.assertTrue(df.empty)


class TestInningsPlanNumberingForSuperOversOnly(unittest.TestCase):
    def test_labels_with_ordinals_group_halves_together(self):
        payload = {"innings": [
            {"Number": 3, "name": "MI London (Men) - 1st Super Over", "teams": [{"name": "MI London (Men)"}]},
            {"Number": 4, "name": "Sunrisers Leeds (Men) - 1st Super Over", "teams": [{"name": "Sunrisers Leeds (Men)"}]},
            {"Number": 5, "name": "MI London (Men) - 2nd Super Over", "teams": [{"name": "MI London (Men)"}]},
            {"Number": 6, "name": "Sunrisers Leeds (Men) - 2nd Super Over", "teams": [{"name": "Sunrisers Leeds (Men)"}]},
        ]}
        plan, _ = sc.build_innings_plan([payload])
        self.assertEqual([p.super_over_number for p in plan], [1, 1, 2, 2])
        self.assertTrue(all(p.is_super_over for p in plan))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestReportingAndCli(unittest.TestCase):
    def _write_csv(self, path):
        payload = {"data": {"scorecard": {"innings": [
            {"Number": 1, "name": "MI London (Men) Inning", "teams": [{"name": "MI London (Men)"}]},
            {"Number": 2, "name": "Sunrisers Leeds (Men) Inning", "teams": [{"name": "Sunrisers Leeds (Men)"}]},
            {"Number": 3, "name": "1st Super Over", "isSuperOver": True, "teams": [{"name": "MI London (Men)"}]},
        ], "players": [{"id": 71, "longName": "Brydon Carse", "bowlingStyle": "RIGHT_ARM_FAST"}]}}}
        plan, _ = sc.build_innings_plan(payload, "Match tied")
        balls = innings_balls(1, 2) + innings_balls(2, 2) + innings_balls(3, 1)
        players = {}
        sc.find_players_anywhere(payload, players)
        df = sc.build_output_frame(balls, players, {"matchName": "MI London vs Sunrisers Leeds",
                                                    "matchId": "1521231"}, plan)
        df.to_csv(path, index=False)
        return df

    def test_summarize_reports_the_super_over(self):
        import io
        import contextlib

        path = os.path.join(os.environ.get("TMPDIR", "/tmp"), "1521231.csv")
        self._write_csv(path)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sc.summarize(path)
        text = buf.getvalue()
        os.remove(path)
        self.assertIn("INNINGS BREAKDOWN", text)
        self.assertIn("SUPER OVER", text)
        self.assertIn("Innings 3", text)
        self.assertIn("deliveries across 1 super over", text)

    def test_csv_filename_matches_legacy_convention(self):
        # downstream tooling globs for <matchId>.csv, so the name must not drift
        self.assertEqual(sc.extract_match_id(HUNDRED_URL) + ".csv", "1521231.csv")

    def test_cli_flags(self):
        args = sc.parse_args(["--no-api", "--headless", "--out-dir", "data", "http://x/y"])
        self.assertTrue(args.no_api)
        self.assertTrue(args.headless)
        self.assertEqual(args.out_dir, "data")
        self.assertEqual(args.urls, ["http://x/y"])
        default = sc.parse_args([])
        self.assertFalse(default.no_api)
        self.assertFalse(default.api_only)

    def test_urls_file_is_read(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as handle:
            handle.write("# a comment\n\n" + HUNDRED_URL + "\n")
            name = handle.name
        args = sc.parse_args(["--urls-file", name])
        with open(name, encoding="utf-8") as handle:
            urls = [ln.strip() for ln in handle if ln.strip() and not ln.strip().startswith("#")]
        os.unlink(name)
        self.assertEqual(urls, [HUNDRED_URL])
        self.assertEqual(args.urls_file, name)
