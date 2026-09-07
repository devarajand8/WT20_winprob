#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ESPNcricinfo ball-by-ball scraper -- includes Super Over deliveries.

This is a rework of the old ``scroll_dropdown_fixed.py`` worker/controller pair.
Everything the old script produced is still produced (same CSV-per-match layout,
same player-style enrichment), plus:

  * Super Overs are captured.  They are *discovered*, not hard-coded: the scorecard
    payload is walked and any innings that Cricinfo flags as a super over /
    "Super 5" / "SO" is added to the fetch plan -- whether the API numbers it 3, 5
    or 47.  If the match was tied but no super-over innings was found in the
    payload, the next few innings numbers are probed anyway, so a schema change
    cannot silently drop a tie-breaker.
  * A direct API path (``/v1/pages/match/comments``) replaces "scroll until the
    feed stops growing" for the happy path, so a match costs seconds instead of a
    couple of minutes of scrolling.  The dropdown-scrolling crawler is kept as an
    automatic fallback (and as ``--no-api``), and it now iterates the innings
    dropdown by *position* instead of by label, so two or more consecutive super
    overs are all visited.
  * Every row is tagged: ``inningType`` ("Regular"/"Super Over"), ``isSuperOver``,
    ``superOverNumber`` (1, 2, ... when a tie needs more than one super over),
    ``inningLabel``, plus numeric ``overNumber``/``ballNumber`` columns.  Sorting is
    done on those (the old code sorted on the string ``oversActual``, which put
    "10.1" before "2.5").
  * Duplicate balls from the API and from the browser feed are coalesced
    field-by-field instead of dropping one copy, so a partially-populated record
    never wins over a complete one.

Usage
-----
    python scrape_cricinfo_balls.py                      # uses MATCH_URLS below
    python scrape_cricinfo_balls.py <commentary-or-scorecard-url> [...]
    python scrape_cricinfo_balls.py --urls-file matches.txt --out-dir data/
    python scrape_cricinfo_balls.py --no-api <url>       # browser-scroll crawler only
    python scrape_cricinfo_balls.py --api-only <url>     # never scroll

Output CSV name defaults to ``<matchId>.csv`` (e.g. ``1521231.csv``), matching the
old behaviour, so anything downstream that globs for ``*.csv`` keeps working.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# =============================================================================
# MATCH LIST CONFIGURATION (add your match commentary URLs here)
# =============================================================================
MATCH_URLS: List[str] = [
    "https://www.espncricinfo.com/series/the-hundred-men-s-competition-2026-1521176/"
    "mi-london-men-vs-sunrisers-leeds-men-1st-match-1521231/ball-by-ball-commentary",
]

API_BASE = "https://hs-consumer-api.espncricinfo.com/v1"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

BLOCKED_RESOURCE_HINTS = (
    "googlesyndication", "doubleclick", "amazon-adsystem", "outbrain",
    "criteo", "clevertap", "wzrk", "google-analytics", "googletagmanager",
)

# --- column order for the front of the CSV -----------------------------------
LEAD_COLUMNS = [
    "tournamentName", "matchName", "venue", "matchId", "seriesId",
    "inningNumber", "inningType", "isSuperOver", "superOverNumber", "inningLabel",
    "overNumber", "ballNumber", "oversActual",
]

# "Super Over", "1st Super Over", "Super Over - MI London", "2nd SO", "Super 5"
SUPER_OVER_TEXT_RE = re.compile(
    r"super[\s\-]*(?:over|[\-]?\d)|\bs[\s\-]?over\b|(?<![a-z])s\d?o(?![a-z])|super[\s\-]*5",
    re.IGNORECASE,
)
SUPER_OVER_SLUG_RE = re.compile(r"^super[\s\-_]?overs?$|^super[\s\-_]?5$", re.IGNORECASE)
SUPER_OVER_KEY_RE = re.compile(r"super[\s\-_]?overs?$", re.IGNORECASE)
ORDINAL_RE = re.compile(r"\b(1st|2nd|3rd|4th|5th|6th|first|second|third|fourth|fifth|sixth)\b", re.IGNORECASE)
ORDINALS = {
    "1st": 1, "first": 1, "2nd": 2, "second": 2, "3rd": 3, "third": 3,
    "4th": 4, "fourth": 4, "5th": 5, "fifth": 5, "6th": 6, "sixth": 6,
}

INN_NUMBER_KEYS = ("Number", "number", "inningsNumber", "inningNumber", "innNo", "inningsNo", "no")
INN_META_KEYS = (
    "name", "Name", "longName", "shortName", "title", "label",
    "battingTeam", "battingTeamId", "teams", "teamsList", "status",
    "inningsId", "id", "nameId", "matchId",
)
# A `Number` + metadata alone is not enough -- statsguru-ish payloads have plenty of
# those. At least one of these anchors (or an innings-sounding label) must be there.
INN_ANCHOR_KEYS = ("inningsId", "teams", "teamsList", "battingTeam", "battingTeamId", "matchId", "status", "nameId")
INNINGS_LABEL_RE = re.compile(r"innin|over|\d(?:st|nd|rd|th)\b", re.IGNORECASE)
BALL_MARKERS = ("oversActual", "seqNo", "ballResult", "comments", "text", "shortText")


# =============================================================================
# PURE HELPERS -- no browser, no pandas, unit-testable
# =============================================================================
def extract_match_id(url: str) -> str:
    """
    Match id, from either URL shape in use today:

        /series/<slug>-<seriesId>/<teams>-1st-match-<matchId>/<page>   (current)
        /series/<seriesId>/commentary/<matchId>/<teams>                (legacy)
    """
    path = re.split(r"[?#]", url, 1)[0].rstrip("/")
    segments = [s for s in path.split("/") if s]
    # The /series/<slug>-<seriesId>/ segment also ends in a long number -- never let it
    # masquerade as the match id.
    if "series" in segments:
        idx = len(segments) - 1 - segments[::-1].index("series")
        if idx + 1 < len(segments):
            segments.pop(idx + 1)

    # 1. A slug carrying the match id at its tail, e.g. "...-1st-match-1521231".
    for seg in reversed(segments):
        m = re.fullmatch(r"[a-z0-9.\-]*?-(\d{5,})", seg, re.IGNORECASE)
        if m and re.search(r"(match|game|-)\d*$", seg):
            return m.group(1)

    # 2. Otherwise the last purely-numeric path segment is the match id
    #    (the legacy layout puts the series id earlier in the path).
    for seg in reversed(segments):
        if re.fullmatch(r"\d{5,}", seg):
            return seg

    # 3. Last resort: the trailing number before a page slug, then any last number.
    for pattern in (
        r"/(\d+)(?=[-/](?:ball|live|full|score|commentary|result|match-scorecard))",
        r"/(?:id|matchId|match)[=/](\d+)",
    ):
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    numbers = re.findall(r"\d+", url)
    if numbers:
        return numbers[-1]
    raise ValueError(f"Could not extract match ID from URL: {url}")


def extract_series_id(url: str) -> Optional[str]:
    """Series id lives at the tail of the /series/<slug>-<id>/ segment."""
    m = re.search(r"/series/[^/]*?-(\d{4,})/", url)
    if m:
        return m.group(1)
    m = re.search(r"/series/(\d+)/", url)
    return m.group(1) if m else None


def url_for_page(match_url: str, page: str) -> str:
    """Swap the trailing page slug (ball-by-ball-commentary -> full-scorecard)."""
    base = re.sub(r"/(ball-by-ball-commentary|full-scorecard|live-blog/[^/]+|commentary/[^/]+|scorecard|live-match-details)\s*$", "", match_url.rstrip("/"), flags=re.I)
    return f"{base}/{page}" if base else match_url


def clean_style_str(val: Any) -> Optional[str]:
    """
    Normalise Cricinfo's style/hand values.

    The old worker did `val.replace('_', ' ').title().replace('Arm', '-arm')`, which
    turned RIGHT_ARM_MEDIUM_FAST into "Right -arm Medium Fast" (the space survives
    the replace). Splitting into words first avoids that.
    """
    if not val:
        return None
    if isinstance(val, (list, tuple)):
        val = ", ".join(str(x) for x in val if x)
    if not isinstance(val, str):
        val = str(val)
    val = val.strip()
    if not val:
        return None
    if "_" in val or val.isupper():
        words = re.findall(r"[A-Za-z0-9]+", re.sub(r"(?<=[a-z])(?=[A-Z])", " ", val))
        if words:
            val = " ".join(w.capitalize() for w in words)
    val = re.sub(r"\b(Right|Left)[\s\-]*Arm\b", r"\1-arm", val, flags=re.IGNORECASE)
    val = re.sub(r"\b(Right|Left)[\s\-]*Hand\b", r"\1-hand", val, flags=re.IGNORECASE)
    val = re.sub(r"\s+", " ", val).strip()
    return val or None


def derive_bowling_hand_and_style(style_val: Any, hand_val: Any = None) -> Tuple[Optional[str], Optional[str]]:
    style_clean = clean_style_str(style_val)
    hand_clean = clean_style_str(hand_val)

    if not hand_clean and style_clean:
        s_lower = style_clean.lower()
        if "right" in s_lower:
            hand_clean = "Right-arm"
        elif "left" in s_lower:
            hand_clean = "Left-arm"
    elif hand_clean:
        h_lower = hand_clean.lower()
        if "right" in h_lower:
            hand_clean = "Right-arm"
        elif "left" in h_lower:
            hand_clean = "Left-arm"
    return style_clean, hand_clean


def parse_over_ball(overs_actual: Any) -> Tuple[Optional[int], Optional[int]]:
    """'10.4' -> (10, 4); 10 -> (10, None); garbage -> (None, None)."""
    if overs_actual is None:
        return None, None
    if isinstance(overs_actual, (int, float)) and not isinstance(overs_actual, bool):
        return int(overs_actual), None
    text = str(overs_actual).strip()
    m = re.match(r"^(\d+)(?:[.](\d+))?$", text)
    if not m:
        return None, None
    return int(m.group(1)), (int(m.group(2)) if m.group(2) is not None else None)


def over_key(overs_actual: Any) -> Tuple[int, int]:
    """Sort key that doesn't do the 'lexicographic overs' thing."""
    over, ball = parse_over_ball(overs_actual)
    return (over if over is not None else -1, ball if ball is not None else -1)


@dataclass
class InningsPlan:
    """One innings of one match, super over or not."""

    number: int
    label: Optional[str] = None
    is_super_over: bool = False
    super_over_number: Optional[int] = None
    source: str = "api"
    team_names: List[str] = field(default_factory=list)

    @property
    def sort_key(self) -> Tuple[int, int]:
        return (1 if self.is_super_over else 0, self.number)


def _looks_like_ball_dict(obj: Dict[str, Any]) -> bool:
    return any(k in obj for k in BALL_MARKERS)


def _is_super_over_dict(obj: Dict[str, Any], inside_super_block: bool) -> bool:
    if inside_super_block:
        return True
    for key in ("isSuperOver", "superOver", "isSo", "soFlag"):
        if obj.get(key):
            return True
    for key in ("nameId", "typeId", "slug", "inningsType", "type", "statusId", "id"):
        val = obj.get(key)
        if isinstance(val, str) and SUPER_OVER_SLUG_RE.match(val.strip()):
            return True
    for key in ("name", "Name", "longName", "shortName", "title", "label", "inningsName", "nameId"):
        val = obj.get(key)
        if isinstance(val, str) and SUPER_OVER_TEXT_RE.search(val):
            return True
    return False


def _number_of(obj: Dict[str, Any]) -> Optional[int]:
    for key in INN_NUMBER_KEYS:
        if key in obj:
            try:
                num = int(str(obj[key]).strip())
            except (TypeError, ValueError):
                continue
            if 0 <= num <= 999:
                return num
    return None


def _teams_of(obj: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    teams = obj.get("teams") or obj.get("teamsList")
    if isinstance(teams, list):
        for team in teams:
            if isinstance(team, dict):
                nm = team.get("name") or team.get("shortName") or team.get("alternateName")
                if nm:
                    names.append(str(nm))
            elif isinstance(team, str):
                names.append(team)
    bt = obj.get("battingTeam")
    if isinstance(bt, dict):
        nm = bt.get("name") or bt.get("shortName")
        if nm:
            names.append(str(nm))
    elif isinstance(bt, str):
        names.append(bt)
    return names


def find_innings_candidates(payload: Any, inside_super_block: bool = False, depth: int = 0) -> List[Dict[str, Any]]:
    """
    Walk a Cricinfo JSON payload and collect every dict that looks like an innings,
    remembering whether it was nested under a `superOvers`-ish key.
    """
    found: List[Dict[str, Any]] = []
    if depth > 14:
        return found
    if isinstance(payload, dict):
        number = _number_of(payload)
        meta_hits = sum(1 for k in INN_META_KEYS if k in payload)
        if number is not None and meta_hits >= 2 and not _looks_like_ball_dict(payload):
            labelled = " ".join(
                str(payload[k]) for k in ("name", "Name", "longName", "shortName", "title", "label")
                if isinstance(payload.get(k), str)
            )
            if any(k in payload for k in INN_ANCHOR_KEYS) or INNINGS_LABEL_RE.search(labelled):
                found.append({"__innings__": payload, "__super__": inside_super_block})
        for key, val in payload.items():
            child_super = inside_super_block or bool(SUPER_OVER_KEY_RE.search(str(key)))
            if isinstance(val, (dict, list)):
                found.extend(find_innings_candidates(val, child_super, depth + 1))
    elif isinstance(payload, list):
        for item in payload:
            found.extend(find_innings_candidates(item, inside_super_block, depth + 1))
    return found


def _ordinal_in(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    m = ORDINAL_RE.search(text)
    return ORDINALS.get(m.group(1).lower()) if m else None


def build_innings_plan(payloads: Iterable[Any], status_text: str = "") -> Tuple[List[InningsPlan], List[int]]:
    """
    Return (plan, extra_innings_numbers_to_probe).

    `plan` is every innings discovered in the payloads -- regular ones first, then
    super overs -- each tagged with which super over it belongs to.  `extra` holds
    innings numbers we should probe even though nothing in the payload advertised
    them (a tied match whose scorecard didn't surface the super over innings).
    """
    if isinstance(payloads, (dict, list)):   # a bare payload is a common slip
        payloads = [payloads]
    candidates: List[Dict[str, Any]] = []
    seen: Dict[Tuple[int, str], None] = {}
    for payload in payloads:
        for item in find_innings_candidates(payload):
            obj = item["__innings__"]
            number = _number_of(obj)
            if number is None:
                continue
            label = None
            for key in ("name", "Name", "longName", "shortName", "title", "label"):
                val = obj.get(key)
                if isinstance(val, str) and val.strip():
                    label = val.strip()
                    break
            is_so = _is_super_over_dict(obj, bool(item["__super__"]))
            key = (number, label or "")
            if key in seen:
                continue
            seen[key] = None
            candidates.append({
                "number": number, "label": label, "is_super_over": is_so,
                "teams": _teams_of(obj),
            })

    regular = sorted({c["number"] for c in candidates if not c["is_super_over"]})
    super_overs = sorted({c["number"] for c in candidates if c["is_super_over"]})
    numbers = sorted(set(regular) | set(super_overs))

    plan: List[InningsPlan] = []
    for number in numbers:
        rows = [c for c in candidates if c["number"] == number]
        label = next((c["label"] for c in rows if c.get("label")), None)
        is_so = any(c["is_super_over"] for c in rows)
        teams: List[str] = []
        for row in rows:
            for team in row["teams"]:
                if team not in teams:
                    teams.append(team)
        plan.append(InningsPlan(number=number, label=label, is_super_over=is_so, team_names=teams))

    # Number the super overs: 1st, 2nd, ... (a tie can need several).
    so_entries = [p for p in plan if p.is_super_over]
    labelled = [p for p in so_entries if _ordinal_in(p.label)]
    if labelled:
        for entry in so_entries:
            entry.super_over_number = _ordinal_in(entry.label) or 1
    elif so_entries:
        # No ordinal in the label: each super over is normally bowled as two halves,
        # so group consecutive innings pairs (5,6 -> SO1, 7,8 -> SO2).
        for idx, entry in enumerate(so_entries):
            entry.super_over_number = idx // 2 + 1

    extra: List[int] = []
    mentioned_super_over = bool(status_text and SUPER_OVER_TEXT_RE.search(status_text))
    tied = bool(status_text and re.search(r"\btie\b|tied", status_text, re.IGNORECASE))
    if mentioned_super_over and not so_entries:
        start = (max(numbers) if numbers else len(regular) or 2) + 1
        extra = [n for n in range(start, start + 6) if n not in numbers]
    elif tied and not so_entries:
        # Tied but no super over anywhere: it may have been abandoned, or the
        # innings objects may be shaped differently than expected. Probe lightly.
        start = (max(numbers) if numbers else len(regular) or 2) + 1
        extra = [n for n in range(start, start + 4) if n not in numbers]

    plan.sort(key=lambda p: p.sort_key)
    return plan, extra


def collect_strings(payload: Any, keys: Sequence[str], depth: int = 0, out: Optional[List[str]] = None) -> List[str]:
    if out is None:
        out = []
    if depth > 12 or len(out) > 60:
        return out
    if isinstance(payload, dict):
        for key, val in payload.items():
            if isinstance(val, str) and any(k.lower() == str(key).lower() for k in keys):
                out.append(val)
            else:
                collect_strings(val, keys, depth + 1, out)
    elif isinstance(payload, list):
        for item in payload:
            collect_strings(item, keys, depth + 1, out)
    return out


def extract_status_text(payload: Any) -> str:
    return " | ".join(dict.fromkeys(collect_strings(payload, ("status", "statusText", "matchStatus", "result", "statusId"))))


def extract_header_metadata(payload: Any, depth: int = 0) -> Dict[str, Optional[str]]:
    """tournamentName / matchName / venue from a matchHeader-ish dict, if present."""
    empty = {"tournamentName": None, "matchName": None, "venue": None}
    if not isinstance(payload, dict) or depth > 4:
        return empty

    header = None
    for key in ("matchHeader", "matchInfo", "match"):
        val = payload.get(key)
        if isinstance(val, dict) and ("ground" in val or "series" in val or "matchType" in val):
            header = val
            break
    if header is None:
        for val in payload.values():
            if isinstance(val, dict):
                nested = extract_header_metadata(val, depth + 1)
                if any(nested.values()):
                    return nested
        return empty

    out = dict(empty)
    series = header.get("series")
    if isinstance(series, dict):
        val = series.get("longName") or series.get("name") or series.get("alternateName")
        if val:
            out["tournamentName"] = str(val).strip()
    title = header.get("title") or header.get("longName") or header.get("name")
    if title:
        out["matchName"] = str(title).strip()
    ground = header.get("ground") or header.get("venue")
    if isinstance(ground, dict):
        g_name = ground.get("longName") or ground.get("name") or ground.get("smallName")
        town = ground.get("town")
        town = town.get("name") if isinstance(town, dict) else town
        if g_name and town and str(town).lower() not in str(g_name).lower():
            out["venue"] = f"{g_name}, {town}".strip()
        elif g_name:
            out["venue"] = str(g_name).strip()
    elif isinstance(ground, str) and ground.strip():
        out["venue"] = ground.strip()
    return out


def parse_comments(obj: Any) -> List[Dict[str, Any]]:
    """Flatten every ball-comment dict out of a (nested) Cricinfo payload."""
    extracted: List[Dict[str, Any]] = []
    if isinstance(obj, dict):
        comments = obj.get("comments")
        if isinstance(comments, list):
            for comment in comments:
                if isinstance(comment, dict) and ("id" in comment or "oversActual" in comment):
                    flat = dict(comment)
                    if isinstance(flat.get("predictions"), dict):
                        for k, v in flat["predictions"].items():
                            flat.setdefault(f"pred_{k}", v)
                    if isinstance(flat.get("dismissalText"), dict):
                        dismissal = flat["dismissalText"]
                        flat["dismissal_text_short"] = dismissal.get("short")
                        flat["dismissal_text_long"] = dismissal.get("long")
                        flat["dismissal_text_commentary"] = dismissal.get("commentary")
                    extracted.append(flat)
        for val in obj.values():
            extracted.extend(parse_comments(val))
    elif isinstance(obj, list):
        for item in obj:
            extracted.extend(parse_comments(item))
    return extracted


PLAYER_NAME_KEYS = ("longName", "fullName", "name", "knownAs", "shortName")


def find_players_anywhere(obj: Any, player_map: Dict[int, Dict[str, Any]]) -> None:
    if isinstance(obj, dict):
        p_id = obj.get("id") or obj.get("objectId") or obj.get("playerId") or obj.get("player_id")
        has_player_keys = any(k in obj for k in (
            "longName", "fullName", "knownAs", "battingStyle", "bowlingStyle",
            "battingStyles", "bowlingStyles", "battingStyleType", "bowlingStyleType",
        ))
        if p_id and has_player_keys:
            try:
                p_id = int(p_id)
            except (TypeError, ValueError):
                p_id = None
            if p_id is not None:
                name = next((obj[k] for k in PLAYER_NAME_KEYS if isinstance(obj.get(k), str) and obj[k].strip()), None)
                bat_style = clean_style_str(
                    obj.get("battingStyle") or obj.get("battingStyles") or obj.get("battingStyleType") or obj.get("longBattingStyles")
                )
                bowl_style, bowl_hand = derive_bowling_hand_and_style(
                    obj.get("bowlingStyle") or obj.get("bowlingStyles") or obj.get("bowlingStyleType") or obj.get("longBowlingStyles"),
                    obj.get("bowlingHand") or obj.get("bowlingHandType") or obj.get("hand"),
                )
                slot = player_map.setdefault(p_id, {})
                for key, val in (("name", name), ("battingStyle", bat_style), ("bowlingStyle", bowl_style), ("bowlingHand", bowl_hand)):
                    if val and not slot.get(key):
                        slot[key] = val
        for val in obj.values():
            find_players_anywhere(val, player_map)
    elif isinstance(obj, list):
        for item in obj:
            find_players_anywhere(item, player_map)


def ball_dedupe_key(ball: Dict[str, Any], default_inning: Optional[int] = None) -> str:
    """Stable per-ball key; falls back to content so balls without an `id` still dedupe."""
    ball_id = ball.get("id")
    if ball_id not in (None, ""):
        return f"id:{ball_id}"
    inn = ball.get("inningNumber", ball.get("inningsNumber"))
    if inn is None:
        inn = default_inning
    return "k:{}|{}|{}|{}".format(inn, ball.get("seqNo"), ball.get("oversActual"), ball.get("title") or ball.get("text") or "")


def merge_ball_records(records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Coalesce the same ball seen from two sources. The richest record wins per
    field, but any non-empty value from the others still fills its gaps.
    """
    ordered = sorted(records, key=lambda r: -sum(1 for v in r.values() if v not in (None, "", [], {}, False)))
    merged: Dict[str, Any] = dict(ordered[0])
    for record in ordered[1:]:
        for key, val in record.items():
            cur = merged.get(key)
            if (cur is None or cur == "" or cur == [] or cur == {}) and val not in (None, "", [], {}):
                merged[key] = val
    return merged


def combine_balls(*ball_lists: Sequence[Dict[str, Any]], default_innings: Optional[int] = None) -> List[Dict[str, Any]]:
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []
    for balls in ball_lists:
        for ball in balls:
            if not isinstance(ball, dict):
                continue
            key = ball_dedupe_key(ball, default_innings)
            if key not in buckets:
                buckets[key] = []
                order.append(key)
            buckets[key].append(ball)
    return [merge_ball_records(buckets[k]) for k in order]


def plan_for_inning(plan: Sequence[InningsPlan], number: Optional[int]) -> Optional[InningsPlan]:
    if number is None:
        return None
    for entry in plan:
        if entry.number == number:
            return entry
    return None


def metadata_from_url(match_url: str) -> Dict[str, Optional[str]]:
    """Slug-derived fallbacks so a match still has a name when the API is shy."""
    out = {"tournamentName": None, "matchName": None, "venue": None}
    t_match = re.search(r"/series/([^/]+?)(?:-\d+)?/", match_url)
    if t_match:
        slug = re.sub(r"-\d+$", "", t_match.group(1))
        out["tournamentName"] = slug.replace("-", " ").title()
    m_match = re.search(r"/([^/]+?-\d+(?:st|nd|rd|th)?-match(?:-\d+)?|[^/]+?-vs-[^/]+?-\d+)(?:-\d+)?/", match_url)
    if m_match:
        slug = re.sub(r"-(\d+(?:st|nd|rd|th)-match)?(\d+)?$", "", m_match.group(1))
        out["matchName"] = slug.replace("-", " ").title().replace(" Vs ", " vs ")
    return out


def comments_api_url(series_id: Optional[str], match_id: str, inning_number: int, from_over: int, direction: str = "DESC") -> str:
    parts = [f"{API_BASE}/pages/match/comments?lang=en"]
    if series_id:
        parts.append(f"&seriesId={series_id}")
    parts.append(f"&matchId={match_id}")
    parts.append(f"&inningNumber={inning_number}")
    parts.append("&commentType=ALL")
    parts.append(f"&fromInningOver={from_over}")
    parts.append(f"&sortDirection={direction}")
    return "".join(parts)


def scorecard_api_url(series_id: Optional[str], match_id: str) -> str:
    return f"{API_BASE}/pages/match/scorecard?lang=en" + (f"&seriesId={series_id}" if series_id else "") + f"&matchId={match_id}"


def home_api_url(series_id: Optional[str], match_id: str) -> str:
    return f"{API_BASE}/pages/match/home?lang=en" + (f"&seriesId={series_id}" if series_id else "") + f"&matchId={match_id}"


def start_over_for_format(format_hint: str, overs_hint: Optional[float] = None) -> int:
    """
    Where to begin paging backwards from. Deliberately generous -- the pager stops
    as soon as the innings' first ball shows up, so over-estimating only costs one
    or two (empty) requests, while under-estimating would silently clip the innings.
    """
    fmt = (format_hint or "").upper()
    if "TEST" in fmt:
        default = 220
    elif any(k in fmt for k in ("T20", "TWENTY20", "HUNDRED")):
        default = 26
    elif any(k in fmt for k in ("ODI", "LIMITED", "50")):
        default = 56
    else:
        default = 60
    return max(default, int(overs_hint) + 2 if overs_hint else 0)


# =============================================================================
# DATAFRAME ASSEMBLY (needs pandas)
# =============================================================================
def build_output_frame(
    balls: Sequence[Dict[str, Any]],
    player_map: Dict[int, Dict[str, Any]],
    metadata: Dict[str, Optional[str]],
    plan: Sequence[InningsPlan],
) -> "Any":
    """
    Turn flattened ball records into the deliverable frame: metadata columns first,
    super-over tags, resolved player names/styles, deduped and chronologically sorted.
    """
    import pandas as pd  # local import: keeps this module importable without pandas

    if not balls:
        return pd.DataFrame()

    df = pd.DataFrame(list(balls))

    # Innings number: trust the payload, fall back to the number we paged by.
    if "inningNumber" not in df.columns and "inningsNumber" in df.columns:
        df["inningNumber"] = df["inningsNumber"]
    if "inningsNumber" in df.columns:
        df.drop(columns=["inningsNumber"], inplace=True)
    if "inningNumber" not in df.columns:
        df["inningNumber"] = None
    df["inningNumber"] = pd.to_numeric(df["inningNumber"], errors="coerce")

    # overNumber / ballNumber: prefer whatever the payload carries, else parse
    # `oversActual` ("12.4" -> 12, 4). `ballNumber` is the ball *within* its over.
    overs_actual = df["oversActual"].tolist() if "oversActual" in df.columns else [None] * len(df)
    parsed = [over_key(v) for v in overs_actual]
    for col, pos in (("overNumber", 0), ("ballNumber", 1)):
        if col not in df.columns:
            df[col] = [p[pos] if p[pos] != -1 else None for p in parsed]
        else:
            current = df[col].tolist()
            df[col] = [c if c is not None and c == c else (p[pos] if p[pos] != -1 else None)
                       for c, p in zip(current, parsed)]
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # Tag super overs.
    inning_types: List[str] = []
    super_flags: List[bool] = []
    so_numbers: List[Any] = []
    labels: List[Any] = []
    unknown = set()
    for row_inning in df["inningNumber"].tolist():
        key = None if row_inning is None or pd.isna(row_inning) else int(row_inning)
        entry = plan_for_inning(plan, key)
        if entry is None:
            unknown.add(key)
            inning_types.append("Regular")
            super_flags.append(False)
            so_numbers.append(pd.NA)
            labels.append(pd.NA)
            continue
        inning_types.append("Super Over" if entry.is_super_over else "Regular")
        super_flags.append(bool(entry.is_super_over))
        so_numbers.append(entry.super_over_number if entry.super_over_number is not None else pd.NA)
        labels.append(entry.label or pd.NA)
    if unknown:
        print(f"[!] Balls for innings not in the fetch plan: {sorted(x for x in unknown if x is not None)}")
        print("    Tagged as Regular -- widen find_innings_candidates() if these are tie-breakers.")
    df["inningType"] = inning_types
    df["isSuperOver"] = super_flags
    df["superOverNumber"] = pd.array(so_numbers, dtype="Int64")
    df["inningLabel"] = labels

    for col in ("inningNumber", "overNumber", "ballNumber"):
        df[col] = df[col].fillna(-1 if col != "inningNumber" else 0).astype("Int64")

    # Player enrichment (names + styles), as before, now including super-over balls.
    role_configs = [
        ("batsman", "batsmanPlayerId"),
        ("nonStriker", "nonStrikerPlayerId"),
        ("bowler", "bowlerPlayerId"),
        ("outPlayer", "outPlayerId"),
    ]
    for role, id_col in role_configs:
        if id_col not in df.columns:
            continue

        def lookup(x: Any, key: str, _map: Dict[int, Dict[str, Any]] = player_map) -> Optional[str]:
            try:
                k = int(x)
            except (TypeError, ValueError):
                return None
            return _map.get(k, {}).get(key)

        ids = pd.to_numeric(df[id_col], errors="coerce")
        df[f"{role}Name"] = ids.map(lambda x: lookup(x, "name"))
        if role in ("batsman", "nonStriker"):
            df[f"{role}BattingStyle"] = ids.map(lambda x: lookup(x, "battingStyle"))
        elif role == "bowler":
            df[f"{role}BowlingStyle"] = ids.map(lambda x: lookup(x, "bowlingStyle"))
            df[f"{role}BowlingHand"] = ids.map(lambda x: lookup(x, "bowlingHand"))
        df.drop(columns=[id_col], inplace=True, errors="ignore")

    # Dedupe, coalescing every copy of a ball instead of keeping one arbitrary row.
    keys = [ball_dedupe_key(rec) for rec in df.to_dict("records")]
    df["__key__"] = keys
    frames = []
    for _, group in df.groupby("__key__", sort=False):
        if len(group) == 1:
            frames.append(group)
            continue
        records = [{k: v for k, v in r.items() if k != "__key__"} for r in group.to_dict("records")]
        merged = merge_ball_records(records)
        merged["__key__"] = group.iloc[0]["__key__"]
        frames.append(pd.DataFrame([merged]))
    df = pd.concat(frames, ignore_index=True)
    df.drop(columns=["__key__"], inplace=True, errors="ignore")
    # Match-level metadata as leading columns.
    for col in ("tournamentName", "matchName", "venue", "matchId", "seriesId"):
        value = metadata.get(col)
        if value is not None:
            df[col] = value

    # Sort: regular innings, then super overs; chronological inside each.
    df = df.sort_values(["isSuperOver", "inningNumber", "overNumber", "ballNumber"], na_position="last")
    front = [c for c in LEAD_COLUMNS if c in df.columns]
    return df[front + [c for c in df.columns if c not in front]].reset_index(drop=True)


# =============================================================================
# NETWORKING (playwright only; imported lazily so the module stays testable)
# =============================================================================
def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: WPS433
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "Playwright is required for the browser path. Install with:\n"
            "    pip install playwright && playwright install chromium"
        ) from exc
    return sync_playwright


def api_get_json(page: Any, url: str, timeout_ms: int = 25000) -> Optional[Any]:
    """
    Fetch a Cricinfo JSON endpoint from inside the page (real cookies + TLS
    fingerprint, so Akamai treats it like the site's own XHR), falling back to
    Playwright's request context.
    """
    result = None
    try:
        result = page.evaluate(
            """async ({url, timeoutMs}) => {
                try {
                    const ctrl = new AbortController();
                    const t = setTimeout(() => ctrl.abort(), timeoutMs);
                    const resp = await fetch(url, {credentials: 'include', signal: ctrl.signal});
                    clearTimeout(t);
                    if (!resp.ok) return {__status: resp.status};
                    return {__status: resp.status, __json: await resp.json()};
                } catch (err) { return {__status: -1, __error: String(err)}; }
            }""",
            {"url": url, "timeoutMs": timeout_ms},
        )
    except Exception:  # noqa: BLE001 - detached frame, closed page, etc.
        result = None
    if isinstance(result, dict) and result.get("__status") == 200 and result.get("__json") is not None:
        return result["__json"]

    try:
        resp = page.context.request.get(
            url,
            timeout=timeout_ms,
            headers={"Accept": "application/json", "Referer": "https://www.espncricinfo.com/", "User-Agent": BROWSER_UA},
        )
        if resp.status == 200:
            return resp.json()
    except Exception:  # noqa: BLE001
        pass
    return None


def fetch_innings_balls(
    page: Any,
    series_id: Optional[str],
    match_id: str,
    inning_number: int,
    start_over: int,
    max_pages: int = 14,
) -> Tuple[List[Dict[str, Any]], bool]:
    """
    Page an innings' commentary backwards, over-block by over-block, until the
    opening ball is in hand. Returns (balls, reached_start_of_innings).
    """
    balls: List[Dict[str, Any]] = []
    seen = set()
    reached_start = False
    window = start_over
    empty_pages = 0
    for _ in range(max_pages):
        payload = api_get_json(page, comments_api_url(series_id, match_id, inning_number, window))
        if payload is None:
            break
        batch = parse_comments(payload)
        new = 0
        lowest: Optional[Tuple[int, int]] = None
        for ball in batch:
            if isinstance(ball, dict):
                ball.setdefault("inningNumber", inning_number)
            key = ball_dedupe_key(ball, inning_number)
            if key in seen:
                continue
            seen.add(key)
            balls.append(ball)
            new += 1
            over_key_val = over_key(ball.get("oversActual"))
            if over_key_val != (-1, -1) and (lowest is None or over_key_val < lowest):
                lowest = over_key_val
            if over_key_val == (0, 1):
                reached_start = True
        if new == 0:
            empty_pages += 1
            if not balls:
                # A real innings always yields something for a DESC request at or
                # past its last over: an empty first page means it doesn't exist
                # (we are probing a tie-breaker that wasn't played). Don't page down.
                break
            if empty_pages >= 2:
                break
            window -= 10
        else:
            empty_pages = 0
            if reached_start:
                break
            window = (lowest[0] - 1) if lowest else window - 10
        if window < 0:
            # Nothing older to ask for. If over 0 is in hand, the innings is complete.
            if lowest is not None and lowest[0] <= 0:
                reached_start = True
            break
    return balls, reached_start


# --- browser crawler (fallback path) -----------------------------------------
DROPDOWN_BTN_SELECTOR = "button:has(i.icon-caret_down), button:has(span.ds-text-button-3)"
OPTION_SELECTORS = (
    "div[data-floating-ui-portal] div.ds-cursor-pointer, div.ds-popper div.ds-cursor-pointer, "
    "[role='listbox'] [role='option'], [role='option'], ul[role='listbox'] li"
)
OVERLAY_JS = """() => {
    const btn = document.getElementById('onetrust-accept-btn-handler');
    if (btn) btn.click();
    const selectors = ['#onetrust-consent-sdk', '.onetrust-pc-dark-filter', '#wzrk_wrapper',
        '.wzrk-overlay', '.adSlot', '[id*="ad-overlay"]', 'iframe[id*="google_ads"]',
        'div[class*="video-dock"]', 'div[class*="ad-container"]'];
    selectors.forEach(sel => document.querySelectorAll(sel).forEach(el => el.remove()));
}"""


def remove_ad_and_cookie_overlays(page: Any) -> None:
    try:
        page.evaluate(OVERLAY_JS)
    except Exception:  # noqa: BLE001
        pass


def read_dropdown_options(page: Any) -> List[Tuple[int, str]]:
    """
    (position, label) for every entry in the innings dropdown.

    Positions are DOM indices and labels are deliberately *not* de-duplicated: a
    match with two super overs lists two entries, and in some layouts both halves
    of one super over carry the same label ("Super Over"). Clicking by position is
    the only way to be sure each view is actually visited.
    """
    remove_ad_and_cookie_overlays(page)
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(400)
    button = page.locator(DROPDOWN_BTN_SELECTOR).first
    if button.count() == 0:
        return []
    try:
        button.click(force=True, timeout=4000)
    except Exception:  # noqa: BLE001
        page.evaluate("() => { const b = document.querySelector('button:has(i.icon-caret_down)'); if (b) b.click(); }")
    page.wait_for_timeout(900)

    entries: List[Tuple[int, str]] = []
    for position, option in enumerate(page.locator(OPTION_SELECTORS).all()):
        try:
            text = option.inner_text().strip()
        except Exception:  # noqa: BLE001
            continue
        name = text.split("\n")[0].strip()
        if not name or "feedback" in name.lower():
            continue
        entries.append((position, name))
    try:
        page.mouse.click(10, 10)  # close the popup
    except Exception:  # noqa: BLE001
        pass
    page.wait_for_timeout(300)
    return entries


def select_dropdown_option(page: Any, position: int) -> bool:
    """Open the innings dropdown and click the entry at DOM index `position`."""
    remove_ad_and_cookie_overlays(page)
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(300)
    button = page.locator(DROPDOWN_BTN_SELECTOR).first
    if button.count() == 0:
        return False
    try:
        button.click(force=True, timeout=4000)
    except Exception:  # noqa: BLE001
        page.evaluate("() => { const b = document.querySelector('button:has(i.icon-caret_down)'); if (b) b.click(); }")
    page.wait_for_timeout(900)
    options = page.locator(OPTION_SELECTORS).all()
    if position >= len(options):
        try:
            page.mouse.click(10, 10)
        except Exception:  # noqa: BLE001
            pass
        return False
    try:
        options[position].click(force=True)
        page.wait_for_timeout(3500)
        return True
    except Exception:  # noqa: BLE001
        return False


def scroll_active_feed(page: Any, label: str, balls: List[Dict[str, Any]], max_scrolls: int = 140) -> int:
    """
    Scroll the visible feed to the top of the innings, collecting intercepted
    balls. Stops early once ball 0.1 of *this* innings has been captured, which is
    what makes a 6-ball super over cheap instead of 15 wasted seconds of scrolling.
    """
    print(f"[+] Scrolling '{label}' feed to ball 0.1 ...")
    start_index = len(balls)
    last_count = len(balls)
    stagnant = 0
    for idx in range(1, max_scrolls + 1):
        remove_ad_and_cookie_overlays(page)
        page.evaluate(
            """() => {
                window.scrollTo(0, document.body.scrollHeight);
                setTimeout(() => window.scrollBy(0, -400), 100);
                setTimeout(() => window.scrollTo(0, document.body.scrollHeight), 250);
            }"""
        )
        page.keyboard.press("PageDown")
        page.wait_for_timeout(950)

        current = len(balls)
        if current > last_count:
            if idx % 4 == 0 or current - last_count > 20:
                print(f"    [scroll {idx:3d}] {current} deliveries captured")
            last_count = current
            stagnant = 0
            if any(over_key(b.get("oversActual")) == (0, 1) for b in balls[start_index:]):
                print(f"    -> opening ball of '{label}' reached")
                return current - start_index
        else:
            stagnant += 1
        if stagnant >= 6:
            print(f"    -> feed stopped growing for '{label}'")
            break
    return len(balls) - start_index


def crawl_needed(
    plan: Sequence[InningsPlan],
    api_balls: Sequence[Dict[str, Any]],
    intercepted: Sequence[Dict[str, Any]],
    api_complete: bool,
) -> Tuple[bool, List[str]]:
    """
    Decide whether the browser crawler has to run.

    The API path is trusted only if it reported every planned innings as fully
    paged *and* each innings -- super overs especially -- actually has balls. A
    super over that came back empty is exactly the failure mode worth re-crawling
    for, and it is invisible to a "did we get lots of balls" check.
    """
    captured: Dict[int, int] = {}
    for ball in list(api_balls) + list(intercepted):
        inning = ball.get("inningNumber", ball.get("inningsNumber"))
        try:
            key = int(inning)
        except (TypeError, ValueError):
            continue
        captured[key] = captured.get(key, 0) + 1
    missing = [entry for entry in plan if not captured.get(entry.number)]
    reasons = [f"innings {e.number} ({e.label or 'unlabelled'})" + (" [SUPER OVER]" if e.is_super_over else "")
               for e in missing]
    if not api_complete:
        return True, reasons
    return (bool(missing) or not (api_balls or intercepted)), reasons


def scrape_match(
    match_url: str,
    output_csv: str,
    headless: bool = False,
    use_api: bool = True,
    api_only: bool = False,
    verbose: bool = True,
) -> Dict[str, Any]:
    """Scrape one match (all innings + super overs) and write `output_csv`."""
    sync_playwright = _require_playwright()
    match_id = extract_match_id(match_url)
    series_id = extract_series_id(match_url)
    commentary_url = url_for_page(match_url, "ball-by-ball-commentary")

    intercepted: List[Dict[str, Any]] = []
    payloads: List[Any] = []
    player_map: Dict[int, Dict[str, Any]] = {}
    metadata: Dict[str, Optional[str]] = {"tournamentName": None, "matchName": None, "venue": None}

    def absorb(payload: Any) -> None:
        if payload is None:
            return
        payloads.append(payload)
        parsed = parse_comments(payload)
        if parsed:
            intercepted.extend(parsed)
        find_players_anywhere(payload, player_map)
        for key, val in extract_header_metadata(payload).items():
            if val and not metadata.get(key):
                metadata[key] = val

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=["--start-minimized", "--disable-blink-features=AutomationControlled"] if not headless else ["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(user_agent=BROWSER_UA, viewport={"width": 1366, "height": 900})
        page = context.new_page()
        page.route(
            "**/*",
            lambda route: route.abort()
            if any(h in route.request.url.lower() for h in BLOCKED_RESOURCE_HINTS)
            else route.continue_(),
        )

        def on_response(response: Any) -> None:
            low = response.url.lower()
            if "comments" in low or "scorecard" in low or "match" in low:
                try:
                    absorb(response.json())
                except Exception:  # noqa: BLE001 - non-JSON or already-consumed body
                    pass

        page.on("response", on_response)

        print(f"[+] Loading {commentary_url}")
        page.goto(commentary_url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(3500)
        remove_ad_and_cookie_overlays(page)

        try:
            next_data = page.evaluate('() => document.getElementById("__NEXT_DATA__") ? document.getElementById("__NEXT_DATA__").textContent : null')
            if next_data:
                absorb(json.loads(next_data))
        except Exception:  # noqa: BLE001
            pass

        # The scorecard JSON is what advertises the super-over innings; the rendered
        # scorecard page is the belt-and-braces source for squads / venue.
        got = api_get_json(page, scorecard_api_url(series_id, match_id))
        if got is not None:
            absorb(got)
        else:
            print("[-] Scorecard API unavailable from the page -- relying on the rendered page and the live feed")
        sc_page = None
        try:
            sc_page = context.new_page()
            sc_page.goto(url_for_page(match_url, "full-scorecard"), wait_until="domcontentloaded", timeout=45000)
            sc_page.wait_for_timeout(2500)
            remove_ad_and_cookie_overlays(sc_page)
            raw = sc_page.evaluate(
                '() => document.getElementById("__NEXT_DATA__") ? document.getElementById("__NEXT_DATA__").textContent : null'
            )
            if raw:
                absorb(json.loads(raw))
            dom_meta = sc_page.evaluate(r"""() => {
                let venue = "", tournament = "";
                for (const row of document.querySelectorAll('tr, div.ds-grid')) {
                    const text = row.innerText || "";
                    if ((text.includes("Venue") || text.includes("Stadium")) && !venue) {
                        const el = row.querySelector('a[href*="/ground/"], a[href*="/cricket-grounds/"]') || row.lastElementChild;
                        if (el) venue = el.innerText.trim();
                    }
                    if (text.includes("Series") && !tournament) {
                        const el = row.querySelector('a[href*="/series/"]') || row.lastElementChild;
                        if (el) tournament = el.innerText.trim();
                    }
                }
                if (!venue) {
                    const g = document.querySelector('a[href*="/cricket-grounds/"], a[href*="/ground/"]');
                    if (g) venue = g.innerText.trim();
                }
                return { venue, tournament };
            }""")
            if isinstance(dom_meta, dict):
                if dom_meta.get("venue") and not metadata.get("venue"):
                    metadata["venue"] = dom_meta["venue"]
                if dom_meta.get("tournament") and not metadata.get("tournamentName"):
                    metadata["tournamentName"] = dom_meta["tournament"]
            if sc_page is not None:
                sc_page.close()
        except Exception as exc:  # noqa: BLE001
            if verbose:
                print(f"[-] Supplementary scorecard query skipped: {exc}")

        got = api_get_json(page, home_api_url(series_id, match_id))
        if got is not None:
            absorb(got)

        status_text = " ".join(extract_status_text(payload) for payload in payloads)[:4000]
        plan, probe_numbers = build_innings_plan(payloads, status_text)
        if not plan:
            # Nothing parsed out of the payloads: assume a normal 2-innings match so
            # the innings numbers we page for are at least sane.
            plan = [InningsPlan(number=n, label=f"Innings {n}") for n in (1, 2)]
            probe_numbers = []

        for key, val in metadata_from_url(match_url).items():
            if val and not metadata.get(key):
                metadata[key] = val
        metadata["matchId"] = match_id
        metadata["seriesId"] = series_id

        print("=" * 70)
        print(f"[+] Match {match_id} (series {series_id}) -- {metadata.get('matchName') or 'unknown'}")
        print(f"[+] Venue: {metadata.get('venue') or 'unknown'}   players seen: {len(player_map)}")
        print("[+] Innings plan:")
        for entry in plan:
            kind = f"Super Over #{entry.super_over_number}" if entry.is_super_over else "regular"
            print(f"      {entry.number:>3}  {kind:<16} {entry.label or ''}")
        if probe_numbers:
            print(f"[+] Tied/super-over hint in status, probing extra innings: {probe_numbers}")
        print("=" * 70)

        api_balls: List[Dict[str, Any]] = []
        api_complete = False
        if use_api:
            format_hint = " ".join(
                v for payload in payloads for v in collect_strings(payload, ("format", "matchType", "matchTypeCode"))
            )[:200]
            api_balls, api_complete = fetch_all_innings(page, series_id, match_id, plan, probe_numbers, format_hint, verbose=verbose)

        must_crawl, reasons = crawl_needed(plan, api_balls, intercepted, api_complete) if use_api else (True, [])
        if api_only:
            must_crawl = False
            if not api_complete and use_api and reasons:
                print(f"[!] --api-only: innings without data will be missing from the CSV: {'; '.join(reasons)}")
        if must_crawl:
            why = f" ({'; '.join(reasons)})" if reasons else ""
            if use_api:
                print(f"[+] API path incomplete{why} -- running the dropdown crawler")
            else:
                print("[+] Crawling the innings dropdown (API disabled) -- super overs included")
            # Crawl into the *shared* list: live interception keeps appending there,
            # and a copy would silently throw away anything that arrived mid-crawl.
            balls = intercepted
            labels = read_dropdown_options(page)
            if labels:
                print(f"[+] Dropdown offers {len(labels)} views: {[lab for _, lab in labels]}")
                for idx, (position, label) in enumerate(labels):
                    if idx > 0 and not select_dropdown_option(page, position):
                        print(f"[-] Could not switch to view {idx} ({label})")
                        continue
                    got_here = scroll_active_feed(page, label, balls)
                    if got_here == 0 and idx == 0:
                        print("[!] No balls intercepted on the default view; continuing")
                    elif got_here:
                        print(f"[+] View {idx + 1}/{len(labels)} '{label}': {got_here} deliveries")
            else:
                print("[!] No innings dropdown found; relying on the default view only")
                scroll_active_feed(page, "default view", balls)
        elif verbose:
            print("[+] API captured every planned innings; skipping the scroll crawler")

        all_balls = combine_balls(api_balls, intercepted)
        browser.close()

    df = build_output_frame(all_balls, player_map, metadata, plan)
    if df.empty:
        print("[!] No deliveries captured for this match.")
        return {"matchId": match_id, "rows": 0, "path": None}

    df.to_csv(output_csv, index=False)
    print(f"[SUCCESS] {len(df)} deliveries -> {output_csv}")
    return {"matchId": match_id, "rows": int(len(df)), "path": output_csv, "columns": list(df.columns), "frame": df}


def fetch_all_innings(
    page: Any,
    series_id: Optional[str],
    match_id: str,
    plan: Sequence[InningsPlan],
    probe_numbers: Sequence[int],
    format_hint: str,
    verbose: bool = True,
) -> Tuple[List[Dict[str, Any]], bool]:
    """Fetch every planned innings (super overs included) straight from the API."""
    all_balls: List[Dict[str, Any]] = []
    complete = True
    numbers = [entry.number for entry in plan] + [n for n in probe_numbers if n not in {e.number for e in plan}]
    for number in numbers:
        entry = plan_for_inning(plan, number)
        start_over = start_over_for_format(format_hint or ("T20" if (entry and entry.is_super_over) else ""))
        if entry and entry.is_super_over:
            start_over = min(start_over, 4)  # one over, plus a couple of boundary-free extras
        balls, reached_start = fetch_innings_balls(page, series_id, match_id, number, start_over)
        if balls:
            kind = "SUPER OVER" if (entry and entry.is_super_over) else "innings"
            if verbose:
                print(f"[+] {kind} {number}: {len(balls)} deliveries from API")
            if not reached_start and not (entry and entry.is_super_over):
                complete = False
        else:
            if entry and entry.is_super_over:
                print(f"[!] Super over innings {number} returned nothing from the API -- falling back to scrolling")
            complete = False
        all_balls.extend(balls)
    return all_balls, complete and bool(all_balls)


# =============================================================================
# QUEUE CONTROLLER
# =============================================================================
def summarize(output_csv: str) -> None:
    import pandas as pd

    df = pd.read_csv(output_csv)
    print("-" * 46)
    print(f"[SUCCESS] {len(df)} deliveries loaded from {output_csv}")
    print("-" * 46)
    print("--- INNINGS BREAKDOWN ---")
    if "inningNumber" in df.columns:
        for inning, sub in df.groupby("inningNumber", sort=True):
            over_col = "overNumber" if "overNumber" in sub.columns else None
            span = ""
            if over_col is not None:
                lo, hi = sub[over_col].min(), sub[over_col].max()
                span = f"overs {lo} -> {hi}"
            kind = "SUPER OVER" if "isSuperOver" in sub.columns and bool(sub["isSuperOver"].iloc[0]) else "regular"
            print(f"  * Innings {int(inning)} [{kind:<10}] {len(sub):4d} deliveries   {span}")
    sample_cols = [c for c in (
        "tournamentName", "matchName", "venue", "inningType", "overNumber", "ballNumber",
        "batsmanName", "batsmanBattingStyle", "bowlerName", "bowlerBowlingStyle", "bowlerBowlingHand",
    ) if c in df.columns]
    if sample_cols:
        print("\n--- SAMPLE ENRICHED DATA ---")
        sample = df[sample_cols].dropna()
        if sample.empty:
            sample = df[sample_cols].head(3)
        print(sample.head(3).to_string(index=False))
    super_rows = df[df["isSuperOver"]] if "isSuperOver" in df.columns else df.iloc[0:0]
    print("\n--- SUPER OVER ---")
    if super_rows.empty:
        print("  (none: this match had no super over)")
    else:
        print(f"  {len(super_rows)} deliveries across {super_rows['superOverNumber'].nunique()} super over(s)")
        with pd.option_context("display.width", 200, "display.max_colwidth", 34):
            cols = [c for c in ("superOverNumber", "overNumber", "ballNumber", "bowlerName", "batsmanName", "title", "text") if c in super_rows.columns]
            print(super_rows[cols].head(12).to_string(index=False))
    print("-" * 46)


def run_queue(urls: Sequence[str], out_dir: str, args: argparse.Namespace) -> None:
    os.makedirs(out_dir, exist_ok=True)
    print(f"Starting match extraction queue for {len(urls)} match(es)...\n")
    failures: List[str] = []
    for i, match_url in enumerate(urls, start=1):
        print("\n" + "=" * 80)
        print(f"PROCESSING MATCH {i}/{len(urls)}")
        print(f"URL: {match_url}")
        print("=" * 80 + "\n")
        try:
            match_id = extract_match_id(match_url)
            output_csv = os.path.join(out_dir, f"{match_id}.csv")
            if os.path.exists(output_csv):
                os.remove(output_csv)  # never summarise a stale file
            env = os.environ.copy()
            env.setdefault("PYTHONIOENCODING", "utf-8")
            result = scrape_match(
                match_url,
                output_csv,
                headless=args.headless,
                use_api=not args.no_api,
                api_only=args.api_only,
                verbose=not args.quiet,
            )
            if result.get("path"):
                summarize(result["path"])
            else:
                failures.append(match_url)
        except Exception as exc:  # noqa: BLE001 - keep the queue moving
            print(f"Failed to process match {i}: {exc}")
            if args.debug:
                raise
            failures.append(match_url)
            time.sleep(1)

    print("\n" + "=" * 80)
    if failures:
        print(f"QUEUE FINISHED WITH {len(failures)} FAILURE(S):")
        for url in failures:
            print(f"  - {url}")
    else:
        print("QUEUE COMPLETED SUCCESSFULLY!")
    print("=" * 80)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape ESPNcricinfo ball-by-ball data, super overs included.")
    parser.add_argument("urls", nargs="*", help="match commentary/scorecard URLs (defaults to MATCH_URLS in this file)")
    parser.add_argument("--urls-file", help="file with one match URL per line ('#' comments allowed)")
    parser.add_argument("--out-dir", default=".", help="directory for <matchId>.csv files (default: .)")
    parser.add_argument("--headless", action="store_true", help="run Chromium headless (Cricinfo may challenge it)")
    parser.add_argument("--no-api", action="store_true", help="skip the JSON API and only use the scroll crawler")
    parser.add_argument("--api-only", action="store_true", help="API only; never open/scroll the innings dropdown")
    parser.add_argument("--quiet", action="store_true", help="less per-innings chatter")
    parser.add_argument("--debug", action="store_true", help="re-raise instead of continuing the queue")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    urls: List[str] = list(args.urls)
    if args.urls_file:
        with open(args.urls_file, "r", encoding="utf-8") as handle:
            urls.extend(line.strip() for line in handle if line.strip() and not line.strip().startswith("#"))
    if not urls:
        urls = list(MATCH_URLS)
    run_queue(urls, args.out_dir, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
