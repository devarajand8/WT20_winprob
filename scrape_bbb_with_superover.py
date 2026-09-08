import os
import sys
import subprocess
import pandas as pd
import re

# =============================================================================
# MATCH LIST CONFIGURATION (Add your match commentary URLs here)
# =============================================================================
match_urls = ["https://www.cricinfo.com/series/icc-men-s-t20-world-cup-2024-1411166/united-states-of-america-vs-pakistan-11th-match-group-a-1415711/ball-by-ball-commentary",
]

def extract_match_id(url):
    patterns = [
        r'/(\d+)(?=[-/](?:ball|live|full|score|commentary))',
        r'/(\d+)(?=[-/]?$)'
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    numbers = re.findall(r'\d+', url)
    if numbers:
        return numbers[-1]
    raise ValueError(f"❌ Could not extract match ID from URL: {url}")

# =============================================================================
# WORKER SCRIPT DEFINITION (Accepts sys.argv arguments)
# =============================================================================
worker_code = r'''
import os
import sys
import re
import json
import time
import pandas as pd
from playwright.sync_api import sync_playwright

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# Parse command line arguments passed from main controller
if len(sys.argv) < 3:
    print("❌ Error: Missing arguments. Usage: python worker.py <match_url> <output_csv>")
    sys.exit(1)

match_url = sys.argv[1]
output_csv = sys.argv[2]

all_captured_balls = []
player_map = {}  # player_id -> details dict
match_metadata = {
    "tournamentName": None,
    "matchName": None,
    "venue": None
}

# -----------------------------------------------------------------------
# TEAM / BATTING-BOWLING TRACKING
# -----------------------------------------------------------------------
# team_registry maps a team's objectId (as a string) -> {"name", "abbreviation"}.
# match_team_ids holds the objectIds of every team seen batting in THIS
# match (should end up with exactly two -- order not significant, just
# used to figure out "the other team" for the bowling side).
# innings_team_map maps inningNumber -> the objectId of the team that BATTED
# in that innings (this naturally covers Super Over innings too, since
# ESPNcricinfo keeps incrementing inningNumber for them, e.g. 3, 4, ...).
#
# IMPORTANT: these are populated ONLY from each ball's own "over.team"
# field (the same trusted, ball-scoped field already used to determine
# isSuperOver). Earlier this session we tried a generic scan of any
# "teams"/"innings" list found anywhere in a network response, but that
# picked up unrelated team pairings from other widgets on the page (e.g.
# a "Pamir Legends" team that has nothing to do with this match),
# corrupting bowlingTeamName. Deriving strictly from over.team on this
# match's own balls avoids that entirely.
team_registry = {}
match_team_ids = set()
innings_team_map = {}

def register_team(team_obj):
    if not isinstance(team_obj, dict):
        return None
    tid = team_obj.get("objectId") or team_obj.get("id") or team_obj.get("teamId")
    if tid is None:
        return None
    tid = str(tid)
    name = (
        team_obj.get("longName")
        or team_obj.get("name")
        or team_obj.get("shortName")
        or team_obj.get("abbreviation")
    )
    abbr = team_obj.get("abbreviation") or team_obj.get("shortName")
    if tid not in team_registry:
        team_registry[tid] = {}
    if name and not team_registry[tid].get("name"):
        team_registry[tid]["name"] = str(name).strip()
    if abbr and not team_registry[tid].get("abbreviation"):
        team_registry[tid]["abbreviation"] = str(abbr).strip()
    return tid

def register_innings_team_from_ball(c_flat):
    # Every ball that ends an over carries a nested "over" object with the
    # batting team for that specific inningNumber. We use ONLY this
    # ball-scoped data (never a generic site-wide JSON scan) so we can't
    # accidentally pick up an unrelated team from some other widget.
    over_obj = c_flat.get("over")
    if not isinstance(over_obj, dict):
        return
    inn_num = over_obj.get("inningNumber", c_flat.get("inningNumber"))
    team_obj = over_obj.get("team")
    if isinstance(team_obj, dict) and inn_num is not None:
        tid = register_team(team_obj)
        if tid:
            try:
                innings_team_map[int(inn_num)] = tid
                match_team_ids.add(tid)
            except (TypeError, ValueError):
                pass

def resolve_team_names_for_inning(inning_num):
    # Given an inningNumber, returns (battingTeamName, bowlingTeamName),
    # using only the team/innings metadata collected from this match's own
    # balls (see register_innings_team_from_ball). Falls back to (None,
    # None) if we never saw an over.team for that particular innings (e.g.
    # every over-ending-ball response for it was missed).
    try:
        inning_num_int = int(inning_num)
    except (TypeError, ValueError):
        return None, None

    batting_id = innings_team_map.get(inning_num_int)
    if batting_id is None:
        return None, None

    bowling_id = None
    for tid in match_team_ids:
        if tid != batting_id:
            bowling_id = tid
            break

    batting_name = team_registry.get(batting_id, {}).get("name")
    bowling_name = team_registry.get(bowling_id, {}).get("name") if bowling_id else None
    return batting_name, bowling_name

# -----------------------------------------------------------------------
# SUPER OVER TRACKING
# -----------------------------------------------------------------------
# Whichever innings/tab is currently selected in the commentary dropdown is
# tracked here so that every ball captured via the network response
# listener can be stamped with the label that was active when it arrived.
# ESPNcricinfo labels Super Over tabs things like "Super Over 1",
# "Super Over 2", "1st Super Over", etc. Regular innings tabs are labelled
# with team names, e.g. "IND Innings".
CURRENT_LABEL = {"value": "Default Innings"}

def is_super_over_label(label):
    if not label:
        return False
    return "super over" in str(label).lower()

def extract_super_over_number_from_label(label):
    if not label:
        return None
    if is_super_over_label(label):
        m = re.search(r'(\d+)', str(label))
        if m:
            return int(m.group(1))
        return 1  # "Super Over" with no explicit number -> treat as the first
    return None

def clean_style_str(val):
    if not val:
        return None
    if isinstance(val, list):
        val = ", ".join([str(x) for x in val if x])
    if not isinstance(val, str):
        val = str(val)
    val = val.strip()
    if not val:
        return None
    if "_" in val and val.isupper():
        val = val.replace("_", " ").title()
        val = val.replace("Arm", "-arm").replace("Hand", "-hand")
    return val

def derive_bowling_hand_and_style(style_val, hand_val=None):
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

def tag_super_over_fields(c_flat, from_super_over_container):
    # --- Super Over tagging ---
    # Determine isSuperOver / superOverNumber purely from data that is
    # intrinsic to the ball itself, never from which dropdown tab happened
    # to be selected in the browser at capture time (that approach is
    # fragile: stray/duplicate network responses can arrive while a
    # different tab is active, silently mislabeling ordinary innings 1/2
    # deliveries as Super Over balls, or vice versa).
    #
    # ESPNcricinfo's __NEXT_DATA__ payload exposes Super Over deliveries
    # two ways, both of which we check here:
    #   1. They live in their own top-level "superOverBallComments" array
    #      (separate from the regular "comments" array).
    #   2. Each ball's nested "over" object carries its own boolean
    #      "isSuperOver" flag (e.g. over.isSuperOver == true).
    # As a last-resort fallback (e.g. if a response is missing the "over"
    # sub-object), inningNumber > 2 is used, since ESPNcricinfo continues
    # incrementing inningNumber past 2 for Super Overs (3, 4, 5, ...).
    over_obj = c_flat.get("over")
    over_is_super = isinstance(over_obj, dict) and bool(over_obj.get("isSuperOver"))

    inning_num = c_flat.get("inningNumber")
    inning_num_over_2 = isinstance(inning_num, (int, float)) and inning_num > 2

    is_super_over = bool(from_super_over_container or over_is_super or inning_num_over_2)
    c_flat["isSuperOver"] = is_super_over

    super_over_number = None
    if is_super_over and isinstance(inning_num, (int, float)):
        try:
            super_over_number = int(inning_num) - 2
        except (TypeError, ValueError):
            super_over_number = None
    c_flat["superOverNumber"] = super_over_number

    # Descriptive label for humans reading the CSV. Derived deterministically
    # from the fields we just computed above (never from the browser's
    # dropdown state, which is unreliable -- see tag_super_over_fields()).
    if is_super_over:
        c_flat["inningsLabel"] = (
            "Super Over %d" % super_over_number if super_over_number else "Super Over"
        )
    elif isinstance(inning_num, (int, float)):
        c_flat["inningsLabel"] = "Innings %d" % int(inning_num)
    else:
        c_flat["inningsLabel"] = CURRENT_LABEL.get("value")


def flatten_ball(c):
    c_flat = dict(c)
    if "predictions" in c_flat and isinstance(c_flat["predictions"], dict):
        for k, v in c_flat["predictions"].items():
            c_flat[k] = v
    if "dismissalText" in c_flat and isinstance(c_flat["dismissalText"], dict):
        c_flat["dismissal_text_short"] = c_flat["dismissalText"].get("short")
        c_flat["dismissal_text_long"] = c_flat["dismissalText"].get("long")
        c_flat["dismissal_text_commentary"] = c_flat["dismissalText"].get("commentary")
    return c_flat


def parse_comments(obj):
    extracted = []
    if isinstance(obj, dict):
        if "comments" in obj and isinstance(obj["comments"], list):
            for c in obj["comments"]:
                if isinstance(c, dict) and ("id" in c or "oversActual" in c):
                    c_flat = flatten_ball(c)
                    tag_super_over_fields(c_flat, from_super_over_container=False)
                    register_innings_team_from_ball(c_flat)
                    extracted.append(c_flat)
        # Super Over deliveries are also exposed via their own dedicated
        # "superOverBallComments" array in the __NEXT_DATA__ payload.
        if "superOverBallComments" in obj and isinstance(obj["superOverBallComments"], list):
            for c in obj["superOverBallComments"]:
                if isinstance(c, dict) and ("id" in c or "oversActual" in c):
                    c_flat = flatten_ball(c)
                    tag_super_over_fields(c_flat, from_super_over_container=True)
                    register_innings_team_from_ball(c_flat)
                    extracted.append(c_flat)
        for k, v in obj.items():
            extracted.extend(parse_comments(v))
    elif isinstance(obj, list):
        for item in obj:
            extracted.extend(parse_comments(item))
    return extracted

def find_players_anywhere(obj):
    if isinstance(obj, dict):
        p_id = obj.get("id") or obj.get("objectId") or obj.get("playerId") or obj.get("player_id")
        has_player_keys = any(k in obj for k in [
            "longName", "fullName", "knownAs", "battingStyle", "bowlingStyle", 
            "battingStyles", "bowlingStyles", "battingStyleType", "bowlingStyleType"
        ])
        
        if p_id and has_player_keys:
            try:
                p_id = int(p_id)
                name = obj.get("longName") or obj.get("fullName") or obj.get("name") or obj.get("knownAs") or obj.get("shortName")
                raw_bat_style = obj.get("battingStyle") or obj.get("battingStyles") or obj.get("battingStyleType") or obj.get("longBattingStyles")
                raw_bowl_style = obj.get("bowlingStyle") or obj.get("bowlingStyles") or obj.get("bowlingStyleType") or obj.get("longBowlingStyles")
                raw_bowl_hand = obj.get("bowlingHand") or obj.get("bowlingHandType") or obj.get("hand")
                
                bat_style = clean_style_str(raw_bat_style)
                bowl_style, bowl_hand = derive_bowling_hand_and_style(raw_bowl_style, raw_bowl_hand)
                
                if p_id not in player_map:
                    player_map[p_id] = {}
                
                if name and not player_map[p_id].get("name"): 
                    player_map[p_id]["name"] = name
                if bat_style and not player_map[p_id].get("battingStyle"): 
                    player_map[p_id]["battingStyle"] = bat_style
                if bowl_style and not player_map[p_id].get("bowlingStyle"): 
                    player_map[p_id]["bowlingStyle"] = bowl_style
                if bowl_hand and not player_map[p_id].get("bowlingHand"): 
                    player_map[p_id]["bowlingHand"] = bowl_hand
            except (ValueError, TypeError):
                pass

        for k, v in obj.items():
            find_players_anywhere(v)
    elif isinstance(obj, list):
        for item in obj:
            find_players_anywhere(item)

def extract_direct_match_header_json(root_json):
    if not isinstance(root_json, dict):
        return
        
    def search_header(obj, depth=0):
        if depth > 4 or not isinstance(obj, dict):
            return None
        for key in ["matchHeader", "matchInfo", "match"]:
            if key in obj and isinstance(obj[key], dict) and ("ground" in obj[key] or "series" in obj[key]):
                return obj[key]
        for k, v in obj.items():
            if isinstance(v, dict):
                res = search_header(v, depth + 1)
                if res:
                    return res
        return None

    header = search_header(root_json)
    if header and isinstance(header, dict):
        if not match_metadata["tournamentName"]:
            s = header.get("series")
            if isinstance(s, dict):
                t_val = s.get("longName") or s.get("name") or s.get("alternateName")
                if t_val: match_metadata["tournamentName"] = t_val.strip()
                
        if not match_metadata["matchName"]:
            m_title = header.get("title") or header.get("longName")
            if m_title: match_metadata["matchName"] = m_title.strip()

        if not match_metadata["venue"]:
            g = header.get("ground") or header.get("venue")
            if isinstance(g, dict):
                g_name = g.get("longName") or g.get("name") or g.get("smallName")
                town = g.get("town", {}).get("name") if isinstance(g.get("town"), dict) else g.get("town")
                if g_name and town and str(town).lower() not in str(g_name).lower():
                    match_metadata["venue"] = f"{g_name}, {town}".strip()
                elif g_name:
                    match_metadata["venue"] = str(g_name).strip()

def normalize_cricinfo_url(url):
    # "cricinfo.com" is just a redirector domain -- it client-side-navigates
    # to "www.espncricinfo.com" a couple seconds after load. If our
    # page.evaluate() calls race that redirect, Playwright raises
    # "Execution context was destroyed, most likely because of a
    # navigation" and kills the whole worker. Rewriting the URL up front
    # avoids that navigation altogether.
    return re.sub(r'https?://(www\.)?cricinfo\.com/', 'https://www.espncricinfo.com/', url)

def safe_evaluate(page, script, arg=None, retries=4, wait_ms=800):
    # Wrapper around page.evaluate() that tolerates a navigation happening
    # mid-call (e.g. a stray client-side redirect, ad script, or SPA route
    # change destroying the JS execution context). Instead of the whole
    # worker crashing, we just wait briefly and retry a few times, and
    # finally give up gracefully by returning `default`.
    last_err = None
    for attempt in range(retries):
        try:
            if arg is not None:
                return page.evaluate(script, arg)
            return page.evaluate(script)
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            if "execution context was destroyed" in msg or "navigation" in msg or "target closed" in msg:
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=8000)
                except Exception:
                    pass
                page.wait_for_timeout(wait_ms)
                continue
            else:
                break
    print(f"  [!] safe_evaluate: giving up after {attempt + 1} attempt(s) ({last_err})")
    return None

def remove_ad_and_cookie_overlays(page):
    safe_evaluate(page, """() => {
        const otBtn = document.getElementById('onetrust-accept-btn-handler');
        if (otBtn) otBtn.click();
        const selectors = [
            '#onetrust-consent-sdk', '.onetrust-pc-dark-filter',
            '#wzrk_wrapper', '.wzrk-overlay',
            '.adSlot', '[id*="ad-overlay"]', 'iframe[id*="google_ads"]',
            'div[class*="video-dock"]', 'div[class*="ad-container"]'
        ];
        selectors.forEach(sel => {
            document.querySelectorAll(sel).forEach(el => el.remove());
        });
    }""")

def get_current_button_text(page):
    remove_ad_and_cookie_overlays(page)
    dropdown_btn = page.locator("button:has(i.icon-caret_down), button:has(span.ds-text-button-3)").first
    if dropdown_btn.count() > 0:
        return dropdown_btn.inner_text().strip().split('\n')[0].strip()
    return "Default Innings"

def switch_to_next_unvisited_innings(page, visited_names):
    remove_ad_and_cookie_overlays(page)
    safe_evaluate(page, "window.scrollTo(0, 0)")
    page.wait_for_timeout(500)
    
    dropdown_btn = page.locator("button:has(i.icon-caret_down), button:has(span.ds-text-button-3)").first
    if dropdown_btn.count() == 0:
        print("  -> No dropdown button available.")
        return None
        
    try:
        dropdown_btn.click(force=True, timeout=4000)
    except Exception:
        safe_evaluate(page, "() => { const b = document.querySelector('button:has(i.icon-caret_down)'); if(b) b.click(); }")
        
    page.wait_for_timeout(1000)
    
    options = page.locator("div[data-floating-ui-portal] div.ds-cursor-pointer, div.ds-popper div.ds-cursor-pointer, [role='option'], li").all()
    
    target_option = None
    target_name = None
    
    for opt in options:
        txt = opt.inner_text().strip()
        name = txt.split('\n')[0].strip()
        if name and name not in visited_names and "feedback" not in name.lower():
            target_option = opt
            target_name = name
            break
            
    if target_option:
        tag = " [SUPER OVER]" if is_super_over_label(target_name) else ""
        print(f"\n[+] Found unvisited innings option: '{target_name}'{tag}. Switching...")
        target_option.click(force=True)
        page.wait_for_timeout(3500)
        return target_name
    else:
        page.mouse.click(10, 10)
        page.wait_for_timeout(400)
        return None

def scroll_active_feed(page, label, max_scrolls=140):
    print(f"[+] Scrolling '{label}' feed down to ball 0.1...")
    last_count = len(all_captured_balls)
    stagnant_count = 0
    
    for s_idx in range(1, max_scrolls + 1):
        remove_ad_and_cookie_overlays(page)
        
        safe_evaluate(page, """() => {
            window.scrollTo(0, document.body.scrollHeight);
            setTimeout(() => window.scrollBy(0, -400), 100);
            setTimeout(() => window.scrollTo(0, document.body.scrollHeight), 250);
        }""")
        page.keyboard.press("PageDown")
        page.wait_for_timeout(950)
        
        current_count = len(all_captured_balls)
        
        if current_count > last_count:
            if s_idx % 4 == 0 or (current_count - last_count) > 20:
                print(f"     [Scroll #{s_idx:3d}] {current_count} total match deliveries captured so far...")
            last_count = current_count
            stagnant_count = 0
        else:
            stagnant_count += 1
            
        if stagnant_count >= 15:
            print(f"  -> Reached start of innings for '{label}' ({current_count} total match balls captured).\n")
            break

print("[+] Launching Chromium browser...")
with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=False,
        args=["--start-minimized", "--disable-blink-features=AutomationControlled"]
    )
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        viewport={"width": 1366, "height": 900}
    )
    page = context.new_page()

    page.route("**/*", lambda route: route.abort() if any(d in route.request.url.lower() for d in [
        "googlesyndication", "doubleclick", "amazon-adsystem", "outbrain", "criteo", "clevertap", "wzrk"
    ]) else route.continue_())

    def on_response(response):
        if "comments" in response.url or "scorecard" in response.url or "match" in response.url:
            try:
                res_json = response.json()
                balls = parse_comments(res_json)
                if balls:
                    all_captured_balls.extend(balls)
                find_players_anywhere(res_json)
                extract_direct_match_header_json(res_json)
            except Exception:
                pass

    page.on("response", on_response)

    # 1. Load Commentary Page
    # Normalize away the cricinfo.com -> www.espncricinfo.com redirector so
    # we land straight on the final URL and don't race a client-side
    # navigation with our page.evaluate() calls below.
    match_url = normalize_cricinfo_url(match_url)
    print(f"[+] Loading match commentary: {match_url}")
    page.goto(match_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3500)
    # Settle any residual client-side redirects/navigations before we start
    # calling page.evaluate() so we don't hit "Execution context was
    # destroyed" errors.
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    remove_ad_and_cookie_overlays(page)

    try:
        next_json_str = safe_evaluate(page, '() => document.getElementById("__NEXT_DATA__") ? document.getElementById("__NEXT_DATA__").textContent : null')
        if next_json_str:
            next_json = json.loads(next_json_str)
            all_captured_balls.extend(parse_comments(next_json))
            find_players_anywhere(next_json)
            extract_direct_match_header_json(next_json)
    except Exception:
        pass

    dom_meta = safe_evaluate(page, r"""() => {
        let tournament = "";
        let matchName = "";
        let venue = "";

        const sLink = document.querySelector('a[href^="/series/"][class*="ds-text-typo"], a[href^="/series/"] span, nav a[href*="/series/"]');
        if (sLink) tournament = sLink.innerText.trim();

        const h1 = document.querySelector('h1.ds-text-title-xs, h1');
        if (h1) matchName = h1.innerText.trim();

        const gLink = document.querySelector('a[href*="/cricket-grounds/"], a[href*="/ground/"]');
        if (gLink) venue = gLink.innerText.trim();

        return { tournament, matchName, venue };
    }""") or {}
    
    if not match_metadata["tournamentName"] and dom_meta.get("tournament"):
        match_metadata["tournamentName"] = dom_meta["tournament"]
    if not match_metadata["matchName"] and dom_meta.get("matchName"):
        match_metadata["matchName"] = dom_meta["matchName"]
    if not match_metadata["venue"] and dom_meta.get("venue"):
        match_metadata["venue"] = dom_meta["venue"]

    # 2. Extract Squad Profiles & Exact Match Info from Scorecard page
    scorecard_url = match_url.replace("ball-by-ball-commentary", "full-scorecard")
    print(f"[+] Fetching scorecard squad & venue metadata from: {scorecard_url}")
    try:
        sc_page = context.new_page()
        sc_page.goto(scorecard_url, wait_until="domcontentloaded", timeout=30000)
        sc_page.wait_for_timeout(2000)
        try:
            sc_page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        
        sc_json_str = safe_evaluate(sc_page, '() => document.getElementById("__NEXT_DATA__") ? document.getElementById("__NEXT_DATA__").textContent : null')
        if sc_json_str:
            sc_json = json.loads(sc_json_str)
            find_players_anywhere(sc_json)
            extract_direct_match_header_json(sc_json)

        sc_dom_meta = safe_evaluate(sc_page, r"""() => {
            let venue = "";
            let tournament = "";
            
            const rows = document.querySelectorAll('tr, div.ds-grid');
            for (const row of rows) {
                const text = row.innerText || "";
                if ((text.includes("Venue") || text.includes("Stadium")) && !venue) {
                    const gEl = row.querySelector('a[href*="/ground/"], a[href*="/cricket-grounds/"]') || row.lastElementChild;
                    if (gEl) venue = gEl.innerText.trim();
                }
                if (text.includes("Series") && !tournament) {
                    const sEl = row.querySelector('a[href*="/series/"]') || row.lastElementChild;
                    if (sEl) tournament = sEl.innerText.trim();
                }
            }
            if (!venue) {
                const gLink = document.querySelector('a[href*="/cricket-grounds/"], a[href*="/ground/"]');
                if (gLink) venue = gLink.innerText.trim();
            }
            return { venue, tournament };
        }""") or {}
        
        if sc_dom_meta.get("venue"):
            match_metadata["venue"] = sc_dom_meta["venue"]
        if sc_dom_meta.get("tournament") and not match_metadata["tournamentName"]:
            match_metadata["tournamentName"] = sc_dom_meta["tournament"]
            
        sc_page.close()
    except Exception as e:
        print(f"[-] Supplementary scorecard query note: {e}")

    if not match_metadata["tournamentName"]:
        t_match = re.search(r'/series/([^/]+?)(?:-\d+)?/', match_url)
        if t_match:
            slug = re.sub(r'-\d+$', '', t_match.group(1))
            match_metadata["tournamentName"] = slug.replace('-', ' ').title()
            
    if not match_metadata["matchName"]:
        m_match = re.search(r'/([^/]+?-\d+(?:st|nd|rd|th)?-match-\d+|[^/]+?-vs-[^/]+?-\d+)/', match_url)
        if m_match:
            slug = re.sub(r'-\d+$', '', m_match.group(1))
            match_metadata["matchName"] = slug.replace('-', ' ').title()

    if not match_metadata["venue"]:
        match_metadata["venue"] = "Unknown Venue"

    print("=" * 60)
    print(f"[+] Tournament : {match_metadata['tournamentName']}")
    print(f"[+] Match      : {match_metadata['matchName']}")
    print(f"[+] Venue      : {match_metadata['venue']}")
    print(f"[+] Registered metadata for {len(player_map)} squad players.")
    print("=" * 60)

    # 3. Scroll through innings (including any Super Over tabs the dropdown exposes)
    current_label = get_current_button_text(page)
    CURRENT_LABEL["value"] = current_label
    visited_innings = [current_label]
    
    print(f"[+] STEP 1: Processing default loaded view: '{current_label}'")
    print("=" * 60)
    scroll_active_feed(page, label=current_label)

    step = 2
    while True:
        next_name = switch_to_next_unvisited_innings(page, visited_innings)
        if not next_name:
            print("[+] No more unvisited innings remaining.")
            break
            
        visited_innings.append(next_name)
        CURRENT_LABEL["value"] = next_name
        tag = " (SUPER OVER)" if is_super_over_label(next_name) else ""
        print("=" * 60)
        print(f"[+] STEP {step}: Processing switched view: '{next_name}'{tag}")
        print("=" * 60)
        scroll_active_feed(page, label=next_name)
        step += 1

    browser.close()

if all_captured_balls:
    df = pd.DataFrame(all_captured_balls)
    df["inningNumber"] = pd.to_numeric(df.get("inningNumber"), errors="coerce").fillna(0).astype(int)

    # Batting/bowling team names, one lookup per unique inningNumber (covers
    # Super Over innings too, since ESPNcricinfo keeps incrementing
    # inningNumber for them -- innings_team_map was populated the same way
    # regardless of whether the innings was a regular one or a Super Over).
    team_name_cache = {
        inn: resolve_team_names_for_inning(inn) for inn in df["inningNumber"].unique()
    }
    df["battingTeamName"] = df["inningNumber"].map(lambda inn: team_name_cache.get(inn, (None, None))[0])
    df["bowlingTeamName"] = df["inningNumber"].map(lambda inn: team_name_cache.get(inn, (None, None))[1])

    # Guarantee the Super Over columns exist even if, for some reason, no
    # ball ever went through the tagging branch above (e.g. very old cached
    # responses replayed without going through parse_comments this run).
    if "isSuperOver" not in df.columns:
        df["isSuperOver"] = df["inningNumber"] > 2
    else:
        df["isSuperOver"] = df["isSuperOver"].fillna(df["inningNumber"] > 2)

    if "superOverNumber" not in df.columns:
        df["superOverNumber"] = None
    df.loc[df["isSuperOver"] & df["superOverNumber"].isna(), "superOverNumber"] = (
        df.loc[df["isSuperOver"] & df["superOverNumber"].isna(), "inningNumber"] - 2
    )

    df.insert(0, "venue", match_metadata["venue"])
    df.insert(0, "matchName", match_metadata["matchName"])
    df.insert(0, "tournamentName", match_metadata["tournamentName"])

    role_configs = [
        ("batsman", "batsmanPlayerId"),
        ("nonStriker", "nonStrikerPlayerId"),
        ("bowler", "bowlerPlayerId"),
        ("outPlayer", "outPlayerId")
    ]
    
    for role, id_col in role_configs:
        if id_col in df.columns:
            df[id_col] = pd.to_numeric(df[id_col], errors="coerce")
            
            df[f"{role}Name"] = df[id_col].map(
                lambda x: player_map.get(int(x), {}).get("name") if pd.notna(x) and int(x) in player_map else None
            )
            
            if role in ["batsman", "nonStriker"]:
                df[f"{role}BattingStyle"] = df[id_col].map(
                    lambda x: player_map.get(int(x), {}).get("battingStyle") if pd.notna(x) and int(x) in player_map else None
                )
            elif role == "bowler":
                df[f"{role}BowlingStyle"] = df[id_col].map(
                    lambda x: player_map.get(int(x), {}).get("bowlingStyle") if pd.notna(x) and int(x) in player_map else None
                )
                df[f"{role}BowlingHand"] = df[id_col].map(
                    lambda x: player_map.get(int(x), {}).get("bowlingHand") if pd.notna(x) and int(x) in player_map else None
                )
                
            df.drop(columns=[id_col], inplace=True, errors="ignore")
    
    if "id" in df.columns:
        df = df.drop_duplicates(subset=["id"]).sort_values(by=["isSuperOver", "inningNumber", "oversActual"])
    else:
        df = df.drop_duplicates(subset=["inningNumber", "oversActual", "title"]).sort_values(by=["isSuperOver", "inningNumber", "oversActual"])
        
    df.to_csv(output_csv, index=False)
    n_so = int(df["isSuperOver"].sum())
    print("=" * 60)
    print(f"[SUCCESS] Extracted {len(df)} total deliveries across ALL innings!")
    if n_so:
        print(f"[SUCCESS] Of which {n_so} delivery(ies) came from Super Over(s).")
    print(f"[SUCCESS] Saved full dataset to '{output_csv}'")
    print("=" * 60)
else:
    print("[!] No deliveries were captured.")
'''

# Save the multi-purpose runner script once
worker_file = "scroll_dropdown_fixed.py"
with open(worker_file, "w", encoding="utf-8") as f:
    f.write(worker_code)

# =============================================================================
# ITERATIVE EXECUTION CONTROLLER
# =============================================================================
print(f"Starting Match Extraction Queue for {len(match_urls)} matches...\n")

for i, match_url in enumerate(match_urls, start=1):
    print("\n" + "=" * 80)
    print(f"PROCESSING MATCH {i}/{len(match_urls)}")
    print(f"URL: {match_url}")
    print("=" * 80 + "\n")
    
    try:
        match_id = extract_match_id(match_url)
        output_csv = f"{match_id}.csv"
        
        # Remove old output to prevent processing stale files
        if os.path.exists(output_csv):
            os.remove(output_csv)
        
        # Build environments for subprocess
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"

        # Spawn subprocess worker, passing parameters as arguments
        process = subprocess.Popen(
            [sys.executable, worker_file, match_url, output_csv],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env
        )

        # Stream real-time standard output from subprocess
        for line in process.stdout:
            print(line, end="")

        process.wait()
        
        # Display match summary of newly generated data
        if os.path.exists(output_csv):
            df_match = pd.read_csv(output_csv)
            print("\n" + "-" * 40)
            print(f"[SUCCESS] Loaded {len(df_match)} total deliveries from '{output_csv}'\n")
            
            print("--- INNINGS BREAKDOWN ---")
            if "inningNumber" in df_match.columns:
                counts = df_match["inningNumber"].value_counts().sort_index()
                for inn, count in counts.items():
                    inn_df = df_match[df_match["inningNumber"] == inn]
                    is_so = bool(inn_df["isSuperOver"].any()) if "isSuperOver" in inn_df.columns else False
                    tag = " (SUPER OVER)" if is_so else ""
                    teams_note = ""
                    if "battingTeamName" in inn_df.columns and "bowlingTeamName" in inn_df.columns:
                        bat_team = inn_df["battingTeamName"].dropna().iloc[0] if inn_df["battingTeamName"].notna().any() else None
                        bowl_team = inn_df["bowlingTeamName"].dropna().iloc[0] if inn_df["bowlingTeamName"].notna().any() else None
                        if bat_team and bowl_team:
                            teams_note = f" [{bat_team} batting vs {bowl_team} bowling]"
                    print(f"  * Innings {inn}{tag}{teams_note}: {count:4d} deliveries (overs {inn_df['oversActual'].min()} -> {inn_df['oversActual'].max()})")
            print("-" * 40)
            
            sample_cols = [c for c in [
                "tournamentName", "matchName", "venue", "isSuperOver", "superOverNumber", "oversActual",
                "battingTeamName", "bowlingTeamName",
                "batsmanName", "batsmanBattingStyle",
                "bowlerName", "bowlerBowlingStyle", "bowlerBowlingHand"
            ] if c in df_match.columns]
            
            print("\n--- SAMPLE ENRICHED DATA ---")
            print(df_match[sample_cols].dropna(subset=[c for c in sample_cols if c not in ("isSuperOver", "superOverNumber")]).head(3).to_string(index=False))
            print("-" * 40)
        else:
            print(f"[-] Subprocess closed, but '{output_csv}' was not generated.")
            
    except Exception as match_err:
        print(f"❌ Failed to process Match {i} due to error: {match_err}")
        continue

print("\n" + "=" * 80)
print("QUEUE COMPLETED SUCCESSFULLY!")
print("=" * 80)
