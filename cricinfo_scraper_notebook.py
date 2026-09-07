# -*- coding: utf-8 -*-
"""
cricinfo_scraper_notebook.py -- ESPNcricinfo ball-by-ball scraper for Jupyter / Colab.
Captures EVERY innings, Super Overs included.

One file, no subprocess, no argparse: paste this whole file into a notebook cell and
run it, or put it next to your notebook and run `%run cricinfo_scraper_notebook.py`.
It defines `scrape_match(url)` / `scrape_many([...])`; the bottom of the file then
auto-runs the queue in `MATCH_URLS` and leaves the combined `pandas.DataFrame` in
`combined_df`. Set `AUTO_RUN = False` to only define the functions.

Setup (once per kernel / runtime):

    %pip install -q playwright pandas
    !playwright install --with-deps chromium      # Colab/Linux only needs this once
    # On Windows/macOS you can skip the install step if Chromium is already there.

Notes specific to notebooks
---------------------------
* Playwright's *sync* API refuses to run when an asyncio loop is already running in the
  thread (a Jupyter/Colab kernel can have one). `scrape_match` therefore hops onto a
  dedicated worker thread automatically -- no `nest_asyncio` needed.
* Nothing here writes to stdout in a way that needs a tty; progress lines stream live,
  so a long scrape shows its work in the cell output instead of looking frozen.
* Set `HEADLESS = None` (default) to pick headed on Windows/macOS and headless on
  Linux/Jupyter servers, where there is no display to open a window on.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# =============================================================================
# 1. CONFIG -- edit these and run the cell
# =============================================================================
MATCH_URLS: List[str] = [
    "https://www.espncricinfo.com/series/the-hundred-men-s-competition-2026-1521176/"
    "mi-london-men-vs-sunrisers-leeds-men-1st-match-1521231/ball-by-ball-commentary",
]

OUTPUT_DIR: Optional[str] = None   # e.g. "data" to also write "<matchId>.csv"; None = DataFrame only
HEADLESS: Optional[bool] = None   # None = auto (headed on Win/macOS, headless elsewhere)
USE_API: bool = True       # fetch via Cricinfo's JSON API; browser crawl is the fallback
API_ONLY: bool = False     # True = never touch the innings dropdown (fastest)
SCROLL_IF_INCOMPLETE: bool = True   # crawl the dropdown for any innings with no balls
AUTO_RUN: bool = True      # run the queue in MATCH_URLS when this file/cell is executed
VERBOSE: bool = True
DEBUG: bool = False        # True = re-raise with a full traceback instead of "[x] failed: ..."
USE_WORKER_THREAD: str = "auto"   # "auto" | "never" | "always" (see _run_off_loop)

# =============================================================================
# 2. CONSTANTS
# =============================================================================
API_BASE = "https://hs-consumer-api.espncricinfo.com/v1"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
BLOCKED_RESOURCE_HINTS = (
    "googlesyndication", "doubleclick", "amazon-adsystem", "outbrain",
    "criteo", "clevertap", "wzrk", "google-analytics", "googletagmanager",
)
LEAD_COLUMNS = [
    "tournamentName", "matchName", "venue", "matchId", "seriesId",
    "inningNumber", "inningType", "isSuperOver", "superOverNumber", "inningLabel",
    "overNumber", "ballNumber", "oversActual",
]

# "Super Over", "1st Super Over", "2nd SO", "Super 5", "Super-Over (MI London)"
SUPER_OVER_TEXT_RE = re.compile(
    r"super[\s\-]*(?:over|[\-]?\d)|\bs[\s\-]?over\b|(?<![a-z])s\d?o(?![a-z])", re.IGNORECASE
)
SUPER_OVER_SLUG_RE = re.compile(r"^super[\s\-_]?overs?$|^super[\s\-_]?5$", re.IGNORECASE)
SUPER_OVER_KEY_RE = re.compile(r"super[\s\-_]?overs?$", re.IGNORECASE)
ORDINAL_RE = re.compile(r"\b(1st|2nd|3rd|4th|5th|6th|first|second|third|fourth|fifth|sixth)\b", re.IGNORECASE)
ORDINALS = {"1st": 1, "first": 1, "2nd": 2, "second": 2, "3rd": 3, "third": 3,
            "4th": 4, "fourth": 4, "5th": 5, "fifth": 5, "6th": 6, "sixth": 6}

INN_NUMBER_KEYS = ("Number", "number", "inningsNumber", "inningNumber", "innNo", "inningsNo", "no")
INN_META_KEYS = ("name", "Name", "longName", "shortName", "title", "label", "battingTeam",
                 "battingTeamId", "teams", "teamsList", "status", "inningsId", "id", "nameId", "matchId")
INN_ANCHOR_KEYS = ("inningsId", "teams", "teamsList", "battingTeam", "battingTeamId", "matchId", "status", "nameId")
INNINGS_LABEL_RE = re.compile(r"innin|over|\d(?:st|nd|rd|th)\b", re.IGNORECASE)
BALL_MARKERS = ("oversActual", "seqNo", "ballResult", "comments", "text", "shortText")

DROPDOWN_BTN_SELECTOR = "button:has(i.icon-caret_down), button:has(span.ds-text-button-3)"
OPTION_SELECTORS = (
    "div[data-floating-ui-portal] div.ds-cursor-pointer, div.ds-popper div.ds-cursor-pointer, "
    "[role='listbox'] [role='option'], [role='option'], ul[role='listbox'] li"
)
OVERLAY_JS = """() => {
    const btn = document.getElementById('onetrust-accept-btn-handler');
    if (btn) btn.click();
    ['#onetrust-consent-sdk', '.onetrust-pc-dark-filter', '#wzrk_wrapper', '.wzrk-overlay',
     '.adSlot', '[id*="ad-overlay"]', 'iframe[id*="google_ads"]',
     'div[class*="video-dock"]', 'div[class*="ad-container"]'
    ].forEach(sel => document.querySelectorAll(sel).forEach(el => el.remove()));
}"""


def _log(msg: str = "") -> None:
    print(msg, flush=True)


def describe_exc(exc: BaseException) -> str:
    """
    Many exceptions stringify to nothing (CancelledError from an interrupted cell,
    a bare TimeoutError, NotImplementedError off the main thread...), which is how
    "[x] failed:" with no reason at all happens. Always name the type.
    """
    text = str(exc).strip()
    name = type(exc).__name__
    if isinstance(exc, asyncio.CancelledError) or (not text and isinstance(exc, (KeyboardInterrupt, SystemExit))):
        return (f"{name}: the kernel/cell was interrupted mid-run. Playwright keeps running "
                "in its own thread, so re-run the cell (and prefer DEBUG=True while setting up)")
    if not text:
        hints = {
            "NotImplementedError": " usually means an asyncio call that only works on the main "
                                   "thread -- try USE_WORKER_THREAD = 'never'",
            "TimeoutError": " usually means Chromium stalled on first launch; check that "
                            "`playwright install chromium` completed",
            "PermissionError": " usually means the browser binary or OUTPUT_DIR is not writable",
        }
        return f"{name}: <no message>{hints.get(name, '')}"
    return f"{name}: {text}"


def _browser_missing_hint() -> str:
    pip_cmd, install_cmd = _install_commands()
    return f"Chromium looks unavailable for Playwright. Run:  {pip_cmd}   then   {install_cmd}"


def _launch_chromium(p: Any, headless: bool) -> Any:
    """
    Launch with the configured flags, then fall back to a plain headless launch.

    `--start-minimized` and headed mode both fail on display-less hosts (Colab,
    Jupyter on a server, CI), and a failed launch used to surface as an empty error.
    """
    base = ["--disable-blink-features=AutomationControlled"]
    attempts = [(bool(headless), base + (["--start-minimized"] if sys.platform.startswith("win") and not headless else []))]
    if headless:
        attempts.append((False, base))
    else:
        attempts.append((True, base))
        attempts.append((True, []))          # last resort: no custom flags at all
    last: Optional[BaseException] = None
    for attempt, (hl, args) in enumerate(attempts, start=1):
        try:
            if attempt > 1:
                _log(f"[!] launch attempt {attempt}: headless={hl}, args={args or '[]'}")
            return p.chromium.launch(headless=hl, args=args)
        except Exception as exc:  # noqa: BLE001
            last = exc
    message = describe_exc(last) if last else "unknown launch failure"
    raise RuntimeError(f"Could not start Chromium ({message})\n{_browser_missing_hint()}") from last


# =============================================================================
# 3. URL + FIELD HELPERS
# =============================================================================
def extract_match_id(url: str) -> str:
    """Handles both URL layouts: .../<teams>-1st-match-<id>/<page> and /series/<sid>/commentary/<id>/..."""
    path = re.split(r"[?#]", url, 1)[0].rstrip("/")
    segments = [s for s in path.split("/") if s]
    if "series" in segments:  # /series/<slug>-<seriesId>/ -- never let the series id masquerade as the match id
        idx = len(segments) - 1 - segments[::-1].index("series")
        if idx + 1 < len(segments):
            segments.pop(idx + 1)
    for seg in reversed(segments):
        m = re.fullmatch(r"[a-z0-9.\-]*?-(\d{5,})", seg, re.IGNORECASE)
        if m and re.search(r"(match|game|-)\d*$", seg):
            return m.group(1)
    for seg in reversed(segments):
        if re.fullmatch(r"\d{5,}", seg):
            return seg
    for pattern in (r"/(\d+)(?=[-/](?:ball|live|full|score|commentary|result))", r"/(?:id|matchId|match)[=/](\d+)"):
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    numbers = re.findall(r"\d+", url)
    if numbers:
        return numbers[-1]
    raise ValueError(f"Could not extract match ID from URL: {url}")


def extract_series_id(url: str) -> Optional[str]:
    m = re.search(r"/series/[^/]*?-(\d{4,})/", url) or re.search(r"/series/(\d+)/", url)
    return m.group(1) if m else None


def url_for_page(match_url: str, page: str) -> str:
    base = re.sub(
        r"/(ball-by-ball-commentary|full-scorecard|live-blog/[^/]+|commentary/[^/]+|scorecard|live-match-details)\s*$",
        "", match_url.rstrip("/"), flags=re.I,
    )
    return f"{base}/{page}" if base else match_url


def clean_style_str(val: Any) -> Optional[str]:
    """RIGHT_ARM_MEDIUM_FAST -> 'Right-arm Medium Fast' (the naive .replace('Arm','-arm') gave 'Right -arm')."""
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
    style_clean, hand_clean = clean_style_str(style_val), clean_style_str(hand_val)
    if not hand_clean and style_clean:
        low = style_clean.lower()
        hand_clean = "Right-arm" if "right" in low else ("Left-arm" if "left" in low else None)
    elif hand_clean:
        low = hand_clean.lower()
        if "right" in low:
            hand_clean = "Right-arm"
        elif "left" in low:
            hand_clean = "Left-arm"
    return style_clean, hand_clean


def over_key(overs_actual: Any) -> Tuple[int, int]:
    """"12.4" -> (12, 4); used instead of sorting the raw string."""
    if overs_actual is None:
        return (-1, -1)
    if isinstance(overs_actual, (int, float)) and not isinstance(overs_actual, bool):
        return (int(overs_actual), -1)
    m = re.match(r"^(\d+)(?:[.](\d+))?$", str(overs_actual).strip())
    if not m:
        return (-1, -1)
    return (int(m.group(1)), int(m.group(2)) if m.group(2) is not None else -1)


# =============================================================================
# 4. SUPER-OVER DISCOVERY
# =============================================================================
class InningsPlan:
    __slots__ = ("number", "label", "is_super_over", "super_over_number", "team_names")

    def __init__(self, number: int, label: Optional[str] = None, is_super_over: bool = False,
                 super_over_number: Optional[int] = None, team_names: Optional[List[str]] = None):
        self.number, self.label, self.is_super_over = number, label, is_super_over
        self.super_over_number, self.team_names = super_over_number, team_names or []

    @property
    def sort_key(self) -> Tuple[int, int]:
        return (1 if self.is_super_over else 0, self.number)

    def __repr__(self) -> str:
        return f"<InningsPlan {self.number} {'SO' + str(self.super_over_number or '') if self.is_super_over else 'regular'} {self.label or ''}>"


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


def _is_super_over_dict(obj: Dict[str, Any], inside_super_block: bool) -> bool:
    if inside_super_block:
        return True
    if any(obj.get(k) for k in ("isSuperOver", "superOver", "isSo", "soFlag")):
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
    batting = obj.get("battingTeam")
    if isinstance(batting, dict):
        nm = batting.get("name") or batting.get("shortName")
        if nm:
            names.append(str(nm))
    elif isinstance(batting, str):
        names.append(batting)
    return names


def find_innings_candidates(payload: Any, inside_super_block: bool = False, depth: int = 0) -> List[Dict[str, Any]]:
    """Walk Cricinfo JSON and collect innings-shaped dicts, remembering `superOvers` nesting."""
    found: List[Dict[str, Any]] = []
    if depth > 14:
        return found
    if isinstance(payload, dict):
        number = _number_of(payload)
        meta_hits = sum(1 for k in INN_META_KEYS if k in payload)
        if number is not None and meta_hits >= 2 and not any(k in payload for k in BALL_MARKERS):
            labelled = " ".join(str(payload[k]) for k in ("name", "Name", "longName", "shortName", "title", "label")
                                if isinstance(payload.get(k), str))
            if any(k in payload for k in INN_ANCHOR_KEYS) or INNINGS_LABEL_RE.search(labelled):
                found.append({"__innings__": payload, "__super__": inside_super_block})
        for key, val in payload.items():
            if isinstance(val, (dict, list)):
                found.extend(find_innings_candidates(val, inside_super_block or bool(SUPER_OVER_KEY_RE.search(str(key))), depth + 1))
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
    -> (plan, extra_innings_numbers_to_probe)

    plan holds every innings found in the payloads, super overs last, each tagged with
    which tie-breaker it belongs to. `extra` covers the awkward case where the match was
    tied but no super-over innings was advertised: we still ask for the trailing numbers.
    """
    if isinstance(payloads, (dict, list)):
        payloads = [payloads]

    candidates: List[Dict[str, Any]] = []
    seen: set = set()
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
            key = (number, label or "")
            if key in seen:
                continue
            seen.add(key)
            candidates.append({"number": number, "label": label,
                               "is_super_over": _is_super_over_dict(obj, bool(item["__super__"])),
                               "teams": _teams_of(obj)})

    regular = sorted({c["number"] for c in candidates if not c["is_super_over"]})
    super_overs = sorted({c["number"] for c in candidates if c["is_super_over"]})
    plan: List[InningsPlan] = []
    for number in sorted(set(regular) | set(super_overs)):
        rows = [c for c in candidates if c["number"] == number]
        teams: List[str] = []
        for row in rows:
            teams += [t for t in row["teams"] if t not in teams]
        plan.append(InningsPlan(number=number, label=next((r["label"] for r in rows if r["label"]), None),
                                is_super_over=any(r["is_super_over"] for r in rows), team_names=teams))

    so_entries = [p for p in plan if p.is_super_over]
    if any(_ordinal_in(p.label) for p in so_entries):
        for entry in so_entries:
            entry.super_over_number = _ordinal_in(entry.label) or 1
    else:
        # no ordinal to lean on: a super over is bowled as two halves, so pair them up
        for idx, entry in enumerate(so_entries):
            entry.super_over_number = idx // 2 + 1

    extra: List[int] = []
    numbers = sorted({p.number for p in plan})
    mentioned = bool(status_text and SUPER_OVER_TEXT_RE.search(status_text))
    tied = bool(status_text and re.search(r"\btie\b|tied", status_text, re.IGNORECASE))
    if (mentioned or tied) and not so_entries:
        start = (numbers[-1] if numbers else 2) + 1
        width = 6 if mentioned else 4
        extra = [n for n in range(start, start + width) if n not in numbers]

    plan.sort(key=lambda p: p.sort_key)
    return plan, extra


# =============================================================================
# 5. PAYLOAD PARSING
# =============================================================================
def parse_comments(obj: Any) -> List[Dict[str, Any]]:
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
                        dt = flat["dismissalText"]
                        flat["dismissal_text_short"] = dt.get("short")
                        flat["dismissal_text_long"] = dt.get("long")
                        flat["dismissal_text_commentary"] = dt.get("commentary")
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
        if p_id and any(k in obj for k in ("longName", "fullName", "knownAs", "battingStyle", "bowlingStyle",
                                            "battingStyles", "bowlingStyles", "battingStyleType", "bowlingStyleType")):
            try:
                p_id = int(p_id)
            except (TypeError, ValueError):
                p_id = None
            if p_id is not None:
                name = next((obj[k] for k in PLAYER_NAME_KEYS if isinstance(obj.get(k), str) and obj[k].strip()), None)
                bat = clean_style_str(obj.get("battingStyle") or obj.get("battingStyles")
                                      or obj.get("battingStyleType") or obj.get("longBattingStyles"))
                bowl, hand = derive_bowling_hand_and_style(
                    obj.get("bowlingStyle") or obj.get("bowlingStyles") or obj.get("bowlingStyleType")
                    or obj.get("longBowlingStyles"),
                    obj.get("bowlingHand") or obj.get("bowlingHandType") or obj.get("hand"))
                slot = player_map.setdefault(p_id, {})
                for key, val in (("name", name), ("battingStyle", bat), ("bowlingStyle", bowl), ("bowlingHand", hand)):
                    if val and not slot.get(key):
                        slot[key] = val
        for val in obj.values():
            find_players_anywhere(val, player_map)
    elif isinstance(obj, list):
        for item in obj:
            find_players_anywhere(item, player_map)


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


def metadata_from_url(match_url: str) -> Dict[str, Optional[str]]:
    out = {"tournamentName": None, "matchName": None, "venue": None}
    t_match = re.search(r"/series/([^/]+?)(?:-\d+)?/", match_url)
    if t_match:
        out["tournamentName"] = re.sub(r"-\d+$", "", t_match.group(1)).replace("-", " ").title()
    m_match = re.search(r"/([^/]+?-\d+(?:st|nd|rd|th)?-match(?:-\d+)?|[^/]+?-vs-[^/]+?-\d+)(?:-\d+)?/", match_url)
    if m_match:
        slug = re.sub(r"-(\d+(?:st|nd|rd|th)-match)?(\d+)?$", "", m_match.group(1))
        out["matchName"] = slug.replace("-", " ").title().replace(" Vs ", " vs ")
    return out


def ball_dedupe_key(ball: Dict[str, Any], default_inning: Optional[int] = None) -> str:
    ball_id = ball.get("id")
    if ball_id not in (None, ""):
        return f"id:{ball_id}"
    inn = ball.get("inningNumber", ball.get("inningsNumber"))
    if inn is None:
        inn = default_inning
    return "k:{}|{}|{}|{}".format(inn, ball.get("seqNo"), ball.get("oversActual"),
                                  ball.get("title") or ball.get("text") or "")


def merge_ball_records(records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Same ball from two sources: richest record first, gaps filled from the others."""
    ordered = sorted(records, key=lambda r: -sum(1 for v in r.values() if v not in (None, "", [], {}, False)))
    merged: Dict[str, Any] = dict(ordered[0])
    for record in ordered[1:]:
        for key, val in record.items():
            if (merged.get(key) is None or merged.get(key) == "" or merged.get(key) in ([], {})) and val not in (None, "", [], {}):
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
    return next((e for e in plan if e.number == number), None)


# =============================================================================
# 6. DATAFRAME ASSEMBLY
# =============================================================================
def build_output_frame(balls: Sequence[Dict[str, Any]], player_map: Dict[int, Dict[str, Any]],
                       metadata: Dict[str, Optional[str]], plan: Sequence[InningsPlan]):
    import pandas as pd

    if not balls:
        return pd.DataFrame()

    df = pd.DataFrame(list(balls))
    if "inningNumber" not in df.columns and "inningsNumber" in df.columns:
        df["inningNumber"] = df["inningsNumber"]
    if "inningsNumber" in df.columns:
        df.drop(columns=["inningsNumber"], inplace=True)
    if "inningNumber" not in df.columns:
        df["inningNumber"] = None
    df["inningNumber"] = pd.to_numeric(df["inningNumber"], errors="coerce")

    overs_actual = df["oversActual"].tolist() if "oversActual" in df.columns else [None] * len(df)
    parsed = [over_key(v) for v in overs_actual]
    for col, pos in (("overNumber", 0), ("ballNumber", 1)):   # ballNumber = ball *within* the over
        if col not in df.columns:
            df[col] = [p[pos] if p[pos] != -1 else None for p in parsed]
        else:
            current = df[col].tolist()
            df[col] = [c if c is not None and c == c else (p[pos] if p[pos] != -1 else None)
                       for c, p in zip(current, parsed)]
        df[col] = pd.to_numeric(df[col], errors="coerce")

    types, flags, so_nums, labels, unknown = [], [], [], [], set()
    for row_inning in df["inningNumber"].tolist():
        key = None if row_inning is None or pd.isna(row_inning) else int(row_inning)
        entry = plan_for_inning(plan, key)
        if entry is None:
            unknown.add(key)
            types.append("Regular"); flags.append(False); so_nums.append(pd.NA); labels.append(pd.NA)
            continue
        types.append("Super Over" if entry.is_super_over else "Regular")
        flags.append(bool(entry.is_super_over))
        so_nums.append(entry.super_over_number if entry.super_over_number is not None else pd.NA)
        labels.append(entry.label or pd.NA)
    if unknown:
        _log(f"[!] Balls for innings not in the plan: {sorted(x for x in unknown if x is not None)} (tagged Regular)")
    df["inningType"], df["isSuperOver"] = types, flags
    df["superOverNumber"] = pd.array(so_nums, dtype="Int64")
    df["inningLabel"] = labels
    for col in ("inningNumber", "overNumber", "ballNumber"):
        df[col] = df[col].fillna(0 if col == "inningNumber" else -1).astype("Int64")

    for role, id_col in (("batsman", "batsmanPlayerId"), ("nonStriker", "nonStrikerPlayerId"),
                         ("bowler", "bowlerPlayerId"), ("outPlayer", "outPlayerId")):
        if id_col not in df.columns:
            continue

        def lookup(x: Any, key: str, _map=player_map) -> Optional[str]:
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

    df["__key__"] = [ball_dedupe_key(rec) for rec in df.to_dict("records")]
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

    for col in ("tournamentName", "matchName", "venue", "matchId", "seriesId"):
        if metadata.get(col) is not None:
            df[col] = metadata[col]

    df = df.sort_values(["isSuperOver", "inningNumber", "overNumber", "ballNumber"], na_position="last")
    front = [c for c in LEAD_COLUMNS if c in df.columns]
    return df[front + [c for c in df.columns if c not in front]].reset_index(drop=True)


def innings_breakdown(df) -> str:
    """Human-readable innings summary; safe to `print()` in a notebook."""
    if df is None or len(df) == 0:
        return "no deliveries captured"
    lines = []
    for inning, sub in df.groupby("inningNumber", sort=True):
        kind = "SUPER OVER" if bool(sub["isSuperOver"].iloc[0]) else "regular"
        lo, hi = sub["overNumber"].min(), sub["overNumber"].max()
        lines.append(f"  innings {int(inning)} [{kind:<10}] {len(sub):4d} balls  overs {lo}->{hi}")
    n_so = int(df["isSuperOver"].sum())
    lines.append(f"  total {len(df)} balls, {n_so} of them in "
                 f"{df.loc[df['isSuperOver'], 'superOverNumber'].nunique() if n_so else 0} super over(s)")
    return "\n".join(lines)


# =============================================================================
# 7. NETWORKING (Playwright)
# =============================================================================
def _install_commands() -> Tuple[str, str]:
    """The two lines to run, phrased for whichever environment is executing this."""
    try:
        get_ipython()  # noqa: F821  (only defined inside IPython / Jupyter / Colab)
        return ("%pip install -q playwright pandas", "!playwright install --with-deps chromium")
    except NameError:
        return ("pip install playwright pandas", "playwright install --with-deps chromium")


def _import_playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: WPS433
    except ImportError as exc:
        pip_cmd, install_cmd = _install_commands()
        raise ImportError(
            "Playwright is not installed here. Run these two lines, then restart the "
            f"kernel and re-execute:\n    {pip_cmd}\n    {install_cmd}"
        ) from exc
    return sync_playwright


def api_get_json(page: Any, url: str, timeout_ms: int = 25000) -> Optional[Any]:
    """Fetch a Cricinfo JSON endpoint from inside the page (real cookies + TLS, so Akamai
    treats it like the site's own XHR); falls back to Playwright's request context."""
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
    except Exception:  # noqa: BLE001 - detached frame, closed page, CSP, ...
        result = None
    if isinstance(result, dict) and result.get("__status") == 200 and result.get("__json") is not None:
        return result["__json"]
    try:
        resp = page.context.request.get(url, timeout=timeout_ms,
                                        headers={"Accept": "application/json",
                                                 "Referer": "https://www.espncricinfo.com/",
                                                 "User-Agent": BROWSER_UA})
        if resp.status == 200:
            return resp.json()
    except Exception:  # noqa: BLE001
        pass
    return None


def comments_api_url(series_id: Optional[str], match_id: str, inning_number: int, from_over: int) -> str:
    return (f"{API_BASE}/pages/match/comments?lang=en"
            + (f"&seriesId={series_id}" if series_id else "")
            + f"&matchId={match_id}&inningNumber={inning_number}&commentType=ALL"
            + f"&fromInningOver={from_over}&sortDirection=DESC")


def scorecard_api_url(series_id: Optional[str], match_id: str) -> str:
    return (f"{API_BASE}/pages/match/scorecard?lang=en" + (f"&seriesId={series_id}" if series_id else "")
            + f"&matchId={match_id}")


def home_api_url(series_id: Optional[str], match_id: str) -> str:
    return (f"{API_BASE}/pages/match/home?lang=en" + (f"&seriesId={series_id}" if series_id else "")
            + f"&matchId={match_id}")


def start_over_for_format(format_hint: str, overs_hint: Optional[float] = None) -> int:
    """Where to begin paging backwards from; deliberately generous -- the pager stops at
    ball 0.1, so over-estimating costs an empty request while under-estimating clips the
    innings."""
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


def fetch_innings_balls(page: Any, series_id: Optional[str], match_id: str, inning_number: int,
                        start_over: int, max_pages: int = 14) -> Tuple[List[Dict[str, Any]], bool]:
    """Page one innings' commentary backwards, over-block by over-block, until ball 0.1."""
    balls: List[Dict[str, Any]] = []
    seen: set = set()
    reached_start, window, empty_pages = False, start_over, 0
    for _ in range(max_pages):
        payload = api_get_json(page, comments_api_url(series_id, match_id, inning_number, window))
        if payload is None:
            break
        new, lowest = 0, None
        for ball in parse_comments(payload):
            if isinstance(ball, dict):
                ball.setdefault("inningNumber", inning_number)
            key = ball_dedupe_key(ball, inning_number)
            if key in seen:
                continue
            seen.add(key)
            balls.append(ball)
            new += 1
            ok = over_key(ball.get("oversActual"))
            if ok != (-1, -1) and (lowest is None or ok < lowest):
                lowest = ok
            if ok == (0, 1):
                reached_start = True
        if new == 0:
            empty_pages += 1
            if not balls:
                break        # a real innings always answers a DESC request at/past its last over
            if empty_pages >= 2:
                break
            window -= 10
        else:
            empty_pages = 0
            if reached_start:
                break
            window = (lowest[0] - 1) if lowest else window - 10
        if window < 0:
            if lowest is not None and lowest[0] <= 0:
                reached_start = True
            break
    return balls, reached_start


def fetch_all_innings(page: Any, series_id: Optional[str], match_id: str, plan: Sequence[InningsPlan],
                      probe_numbers: Sequence[int], format_hint: str, verbose: bool = True):
    all_balls: List[Dict[str, Any]] = []
    complete = True
    planned = {e.number for e in plan}
    for number in [e.number for e in plan] + [n for n in probe_numbers if n not in planned]:
        entry = plan_for_inning(plan, number)
        start_over = start_over_for_format(format_hint or ("T20" if (entry and entry.is_super_over) else ""))
        if entry and entry.is_super_over:
            start_over = min(start_over, 4)
        balls, reached_start = fetch_innings_balls(page, series_id, match_id, number, start_over)
        if balls:
            if verbose:
                kind = "SUPER OVER" if (entry and entry.is_super_over) else "innings"
                _log(f"[+] {kind} {number}: {len(balls)} deliveries from the API")
            if not reached_start and not (entry and entry.is_super_over):
                complete = False
        else:
            if entry and entry.is_super_over and verbose:
                _log(f"[!] super-over innings {number} returned nothing from the API")
            complete = False
        all_balls.extend(balls)
    return all_balls, (complete and bool(all_balls))


def crawl_needed(plan: Sequence[InningsPlan], api_balls: Sequence[Dict[str, Any]],
                 intercepted: Sequence[Dict[str, Any]], api_complete: bool) -> Tuple[bool, List[str]]:
    """Trust the API only if it paged everything AND each planned innings has balls.
    An empty super over is precisely the failure worth re-crawling for, and a
    'did we get lots of balls' check cannot see it."""
    captured: Dict[int, int] = {}
    for ball in list(api_balls) + list(intercepted):
        raw = ball.get("inningNumber", ball.get("inningsNumber"))
        try:
            key = int(raw)
        except (TypeError, ValueError):
            continue
        captured[key] = captured.get(key, 0) + 1
    missing = [e for e in plan if not captured.get(e.number)]
    reasons = [f"innings {e.number} ({e.label or 'unlabelled'})" + (" [SUPER OVER]" if e.is_super_over else "")
               for e in missing]
    if not api_complete:
        return True, reasons
    return (bool(missing) or not (api_balls or intercepted)), reasons


# --- dropdown crawler (fallback path) ----------------------------------------
def _remove_overlays(page: Any) -> None:
    try:
        page.evaluate(OVERLAY_JS)
    except Exception:  # noqa: BLE001
        pass


def _open_dropdown(page: Any) -> bool:
    _remove_overlays(page)
    try:
        page.evaluate("window.scrollTo(0, 0)")
    except Exception:  # noqa: BLE001
        pass
    page.wait_for_timeout(400)
    button = page.locator(DROPDOWN_BTN_SELECTOR).first
    if button.count() == 0:
        return False
    try:
        button.click(force=True, timeout=4000)
    except Exception:  # noqa: BLE001
        try:
            page.evaluate("() => { const b = document.querySelector('button:has(i.icon-caret_down)'); if (b) b.click(); }")
        except Exception:  # noqa: BLE001
            return False
    page.wait_for_timeout(900)
    return True


def read_dropdown_options(page: Any) -> List[Tuple[int, str]]:
    """(dom_position, label) for each innings view. Positions, not labels, so that two
    super-over entries sharing one label are still both visited."""
    if not _open_dropdown(page):
        return []
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
        page.mouse.click(10, 10)
    except Exception:  # noqa: BLE001
        pass
    page.wait_for_timeout(300)
    return entries


def select_dropdown_option(page: Any, position: int) -> bool:
    if not _open_dropdown(page):
        return False
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
    """Scroll until this innings' opening ball shows up in the intercepted feed. Stopping
    at 0.1 is what keeps a 6-ball super over cheap."""
    _log(f"[+] scrolling '{label}' ...")
    start_index, last_count, stagnant = len(balls), len(balls), 0
    for idx in range(1, max_scrolls + 1):
        _remove_overlays(page)
        try:
            page.evaluate("() => { window.scrollTo(0, document.body.scrollHeight); "
                          "setTimeout(() => window.scrollBy(0, -400), 100); "
                          "setTimeout(() => window.scrollTo(0, document.body.scrollHeight), 250); }")
            page.keyboard.press("PageDown")
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(950)
        current = len(balls)
        if current > last_count:
            if idx % 4 == 0 or current - last_count > 20:
                _log(f"    [{idx:3d}] {current} deliveries so far")
            last_count, stagnant = current, 0
            if any(over_key(b.get("oversActual")) == (0, 1) for b in balls[start_index:]):
                _log(f"    -> reached ball 0.1 of '{label}'")
                return current - start_index
        else:
            stagnant += 1
        if stagnant >= 6:
            _log(f"    -> feed stopped growing for '{label}'")
            break
    return len(balls) - start_index


# =============================================================================
# 8. ONE MATCH
# =============================================================================
def _scrape_match_impl(match_url: str, output_csv: Optional[str], headless: bool, use_api: bool,
                       api_only: bool, crawl_if_incomplete: bool, verbose: bool):
    sync_playwright = _import_playwright()
    match_id, series_id = extract_match_id(match_url), extract_series_id(match_url)
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

    _log(f"[+] starting Chromium ({'headless' if headless else 'headed'}) -- the very first "
          "run after `playwright install` can take ~30s")
    with sync_playwright() as p:
        browser = _launch_chromium(p, headless)
        try:
            context = browser.new_context(user_agent=BROWSER_UA, viewport={"width": 1366, "height": 900})
            page = context.new_page()
            page.route("**/*", lambda route: route.abort()
                       if any(h in route.request.url.lower() for h in BLOCKED_RESOURCE_HINTS) else route.continue_())

            def on_response(response: Any) -> None:
                low = response.url.lower()
                if "comments" in low or "scorecard" in low or "match" in low:
                    try:
                        absorb(response.json())
                    except Exception:  # noqa: BLE001 - non-JSON body
                        pass

            page.on("response", on_response)
            _log(f"[+] {commentary_url}")
            page.goto(commentary_url, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(3500)
            _remove_overlays(page)
            try:
                next_data = page.evaluate('() => document.getElementById("__NEXT_DATA__") '
                                           '? document.getElementById("__NEXT_DATA__").textContent : null')
                if next_data:
                    absorb(json.loads(next_data))
            except Exception:  # noqa: BLE001
                pass

            # the scorecard payload is what advertises the super-over innings
            got = api_get_json(page, scorecard_api_url(series_id, match_id)) if use_api else None
            if got is not None:
                absorb(got)
            else:
                _log("[-] scorecard API not reachable from the page; using the live feed + rendered page")
            sc_page = None
            try:
                sc_page = context.new_page()
                sc_page.goto(url_for_page(match_url, "full-scorecard"), wait_until="domcontentloaded", timeout=45000)
                sc_page.wait_for_timeout(2500)
                raw = sc_page.evaluate('() => document.getElementById("__NEXT_DATA__") '
                                       '? document.getElementById("__NEXT_DATA__").textContent : null')
                if raw:
                    absorb(json.loads(raw))
            except Exception as exc:  # noqa: BLE001
                if verbose:
                    _log(f"[-] scorecard page skipped: {exc}")
            finally:
                try:
                    if sc_page is not None:
                        sc_page.close()
                except Exception:  # noqa: BLE001
                    pass
            got = api_get_json(page, home_api_url(series_id, match_id)) if use_api else None
            if got is not None:
                absorb(got)

            status_text = " ".join(extract_status_text(payload) for payload in payloads)[:4000]
            plan, probe_numbers = build_innings_plan(payloads, status_text)
            if not plan:
                plan, probe_numbers = [InningsPlan(number=n, label=f"Innings {n}") for n in (1, 2)], []
            for key, val in metadata_from_url(match_url).items():
                if val and not metadata.get(key):
                    metadata[key] = val
            metadata["matchId"], metadata["seriesId"] = match_id, series_id

            _log("=" * 66)
            _log(f"[+] match {match_id} | {metadata.get('matchName') or 'untitled'} | {metadata.get('venue') or 'venue?'}")
            for entry in plan:
                kind = f"SUPER OVER #{entry.super_over_number}" if entry.is_super_over else "regular"
                _log(f"      innings {entry.number:<3} {kind:<16} {entry.label or ''}")
            if probe_numbers:
                _log(f"[+] tie-breaker hinted in the status but absent from the payload -> probing {probe_numbers}")
            _log("=" * 66)

            api_balls: List[Dict[str, Any]] = []
            api_complete = False
            if use_api:
                hint = " ".join(v for payload in payloads
                                for v in collect_strings(payload, ("format", "matchType", "matchTypeCode")))[:200]
                api_balls, api_complete = fetch_all_innings(page, series_id, match_id, plan, probe_numbers,
                                                             hint, verbose=verbose)

            must_crawl, reasons = crawl_needed(plan, api_balls, intercepted, api_complete) if use_api else (True, [])
            if api_only or not crawl_if_incomplete:
                if must_crawl and verbose and reasons:
                    _log(f"[!] innings still empty: {'; '.join(reasons)} (crawler disabled by flag)")
                must_crawl = False
            if must_crawl:
                why = f" ({'; '.join(reasons)})" if reasons else ""
                _log(f"[+] API path incomplete{why} -- crawling the innings dropdown"
                     if use_api else "[+] crawling the innings dropdown (API disabled) -- super overs included")
                # append into the shared list: live interception keeps writing there
                balls = intercepted
                for idx, (position, label) in enumerate(read_dropdown_options(page)):
                    if idx > 0 and not select_dropdown_option(page, position):
                        _log(f"[-] could not switch to view {idx} ({label})")
                        continue
                    got_here = scroll_active_feed(page, label, balls)
                    if got_here:
                        _log(f"[+] view '{label}': +{got_here} deliveries")
            elif verbose:
                _log("[+] every planned innings came from the API; no scrolling needed")

            all_balls = combine_balls(api_balls, intercepted)
        finally:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass

    df = build_output_frame(all_balls, player_map, metadata, plan)
    if len(df) == 0:
        _log("[!] no deliveries captured")
        return df
    if output_csv:
        os.makedirs(os.path.dirname(os.path.abspath(output_csv)), exist_ok=True)
        df.to_csv(output_csv, index=False)
        _log(f"[SUCCESS] {len(df)} deliveries -> {output_csv}")
    return df


_LOOP_ERROR_HINTS = ("asyncio loop", "sync api inside", "event loop", "another loop")


def _loop_conflict(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(hint in text for hint in _LOOP_ERROR_HINTS)


def _in_worker_thread(fn):
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="playwright") as pool:
        return pool.submit(fn).result()


def _run_off_loop(fn):
    """
    Playwright's sync API refuses to start while an asyncio loop is running in the current
    thread, which some Jupyter/Colab kernels do.

    Rather than always hopping threads (which itself can fail: `asyncio` primitives and
    signal handlers are main-thread-only on Windows), run inline first and hop *only* if
    the failure says the event loop is the problem. Set USE_WORKER_THREAD to "always" or
    "never" to override.
    """
    policy = (USE_WORKER_THREAD or "auto").lower()
    if policy == "always":
        return _in_worker_thread(fn)

    loop_running = False
    try:
        asyncio.get_running_loop()
        loop_running = True
    except RuntimeError:
        pass

    if policy == "never":
        return fn()
    if loop_running:
        return _in_worker_thread(fn)
    try:
        return fn()
    except BaseException as exc:  # noqa: BLE001 - re-raised below if it is not loop-related
        if not _loop_conflict(exc):
            raise
        _log("[!] the kernel has an asyncio loop running; retrying on a worker thread")
        return _in_worker_thread(fn)


def scrape_match(match_url: str, output_dir: Optional[str] = None, headless: Optional[bool] = None,
                 use_api: bool = USE_API, api_only: bool = API_ONLY,
                 crawl_if_incomplete: bool = SCROLL_IF_INCOMPLETE, verbose: bool = VERBOSE):
    """
    Scrape one match (all innings + super overs) and return a pandas DataFrame.

    In a notebook, put `scrape_match(url)` last in the cell and it renders as a table.
    """
    if headless is None:
        headless = HEADLESS if HEADLESS is not None else (os.name == "posix" and not os.environ.get("DISPLAY"))
    # None means "whatever OUTPUT_DIR says"; "" explicitly means "write nothing"
    output_dir = OUTPUT_DIR if output_dir is None else output_dir
    match_id = extract_match_id(match_url)
    csv_path = os.path.join(output_dir, f"{match_id}.csv") if output_dir else None
    if threading.current_thread() is threading.main_thread():
        return _run_off_loop(lambda: _scrape_match_impl(match_url, csv_path, bool(headless), use_api,
                                                         api_only, crawl_if_incomplete, verbose))
    return _scrape_match_impl(match_url, csv_path, bool(headless), use_api, api_only,
                              crawl_if_incomplete, verbose)


def scrape_many(match_urls: Sequence[str], strict: Optional[bool] = None, **kwargs):
    """
    Scrape a queue of matches and return one concatenated DataFrame.

    `df.attrs['per_match']` maps url -> that match's frame (or None), and
    `df.attrs['errors']` maps url -> (message, traceback) for the failures, so a
    half-succeeded batch can be triaged without re-running anything.
    """
    import pandas as pd

    strict = DEBUG if strict is None else strict
    frames, per_match, errors = [], {}, {}
    for i, url in enumerate(match_urls, start=1):
        _log(f"\n{'=' * 78}\nMATCH {i}/{len(match_urls)}  {url}\n{'=' * 78}")
        try:
            df = scrape_match(url, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - report properly, then decide
            tb = traceback.format_exc()
            errors[url] = (describe_exc(exc), tb)
            per_match[url] = None
            _log(f"[x] failed: {describe_exc(exc)}")
            _log("    full traceback below (set DEBUG=True to raise instead of continuing)")
            print(tb, file=sys.stderr, flush=True)
            if strict:
                raise
            continue
        if len(df):
            frames.append(df)
            per_match[url] = df
            _log(innings_breakdown(df))
        else:
            per_match[url] = None
    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    combined.attrs["per_match"] = per_match
    combined.attrs["errors"] = errors
    if errors:
        _log(f"\n[x] {len(errors)} of {len(match_urls)} match(es) failed:")
        for url, (msg, _tb) in errors.items():
            _log(f"      {url}\n          {msg}")
    else:
        _log(f"[SUCCESS] {len(match_urls)} match(es), {len(combined)} deliveries")
    return combined


# =============================================================================
# 9. AUTO-RUN
# =============================================================================
def _main() -> None:
    """Runs the queue in MATCH_URLS and leaves the result in `combined_df`."""
    globals()["combined_df"] = scrape_many(MATCH_URLS)


# Auto-run when pasted into a cell or `%run` (both give __name__ == "__main__"), but NOT
# when imported as a module -- `import cricinfo_scraper_notebook as cric` should only
# define things. Set AUTO_RUN = False to opt out entirely.
if AUTO_RUN and __name__ == "__main__":
    _main()
