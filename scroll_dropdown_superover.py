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

def normalise_url(url):
    """
    www.cricinfo.com is only a redirect target: loading it leaves the page navigating
    while the next Playwright call runs, which is what raises
    "Execution context was destroyed, most likely because of a navigation".
    Go straight to the canonical host (and https).
    """
    url = url.strip()
    url = re.sub(r'^http://', 'https://', url)
    url = re.sub(r'^(https?://)?(www\.)?cricinfo\.com', r'https://www.espncricinfo.com', url)
    url = url.replace("https://www.espncricinfo.com/info", "https://www.espncricinfo.com")
    return url

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

SUPER_OVER_RE = re.compile(r'super|\bso\b|\bs[\s\-]?over\b|super[\s\-]?5', re.IGNORECASE)
DROPDOWN_SELECTOR = "button:has(i.icon-caret_down), button:has(span.ds-text-button-3)"
OPTIONS_SELECTOR = ("div[data-floating-ui-portal] div.ds-cursor-pointer, div.ds-popper div.ds-cursor-pointer, "
                    "[role='option'], li")

def normalise_url(url):
    url = str(url).strip()
    url = re.sub(r'^http://', 'https://', url)
    url = re.sub(r'^(https?://)?(www\.)?cricinfo\.com', r'https://www.espncricinfo.com', url)
    return url

match_url = normalise_url(match_url)
scorecard_url_base = match_url

def safe_evaluate(page, script, arg=None, retries=3, optional=True):
    """
    page.evaluate() raises "Execution context was destroyed, most likely because of a
    navigation" whenever the site is still redirecting/loading (cricinfo.com ->
    espncricinfo.com, cookie walls, A/B redirects). Waiting for the load state and
    retrying is enough in practice; metadata scrapes are cosmetic, so they must never
    abort the run (optional=True).
    """
    last = None
    for attempt in range(1, retries + 1):
        try:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            return page.evaluate(script, arg) if arg is not None else page.evaluate(script)
        except Exception as exc:
            last = exc
            text = str(exc)
            recoverable = ("Execution context was destroyed" in text or "navigation" in text.lower()
                           or "Target closed" in text or "has been closed" in text or "detached" in text.lower())
            if not recoverable:
                break
            print(f"  -> page is still navigating (attempt {attempt}/{retries}); waiting and retrying...")
            try:
                page.wait_for_timeout(1500 * attempt)
            except Exception:
                break
    if optional:
        return None
    raise last

def is_super_over_label(label):
    """A dropdown view whose label mentions a tie-breaker ('Super Over', '1st Super Over', 'Super 5')."""
    return bool(label) and bool(SUPER_OVER_RE.search(label))

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
        val = " ".join(w.capitalize() for w in re.findall(r'[A-Za-z0-9]+', val))
    val = re.sub(r'\b(Right|Left)[\s\-]*Arm\b', r'\1-arm', val, flags=re.IGNORECASE)
    val = re.sub(r'\b(Right|Left)[\s\-]*Hand\b', r'\1-hand', val, flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', val).strip() or None

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

def parse_comments(obj):
    extracted = []
    if isinstance(obj, dict):
        if "comments" in obj and isinstance(obj["comments"], list):
            for c in obj["comments"]:
                if isinstance(c, dict) and ("id" in c or "oversActual" in c):
                    c_flat = dict(c)
                    if "predictions" in c_flat and isinstance(c_flat["predictions"], dict):
                        for k, v in c_flat["predictions"].items():
                            c_flat[k] = v
                    if "dismissalText" in c_flat and isinstance(c_flat["dismissalText"], dict):
                        c_flat["dismissal_text_short"] = c_flat["dismissalText"].get("short")
                        c_flat["dismissal_text_long"] = c_flat["dismissalText"].get("long")
                        c_flat["dismissal_text_commentary"] = c_flat["dismissalText"].get("commentary")
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

def find_super_over_innings(obj, depth=0, inside_super_block=False):
    """
    Look for super-over innings inside a scorecard payload so we can check the dropdown
    actually offered them. Returns a list of (innings_number, label) tuples.
    """
    found = []
    if depth > 12:
        return found
    if isinstance(obj, dict):
        here = inside_super_block
        label = ""
        number = None
        for k, v in obj.items():
            if re.search(r'super[\s\-_]?overs?', str(k), re.IGNORECASE):
                here = True
            if k in ("Number", "number", "inningsNumber", "inningNumber") and isinstance(v, (int, float, str)):
                try:
                    number = int(v)
                except (TypeError, ValueError):
                    pass
            if k in ("name", "Name", "longName", "shortName", "title") and isinstance(v, str):
                label = label or v
            if isinstance(v, (dict, list)):
                found.extend(find_super_over_innings(v, depth + 1, here))
        is_super = here or bool(label and SUPER_OVER_RE.search(label)) or bool(obj.get("isSuperOver"))
        if is_super and number is not None:
            found.append((number, label or "Super Over"))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(find_super_over_innings(item, depth + 1, inside_super_block))
    return found

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
    try:
        dropdown_btn = page.locator(DROPDOWN_SELECTOR).first
    except Exception:
        return "Default Innings"
    if dropdown_btn.count() > 0:
        try:
            return dropdown_btn.inner_text().strip().split('\n')[0].strip()
        except Exception:
            pass
    return "Default Innings"

def open_dropdown(page):
    """Open the innings selector and return the list of option handles (DOM order)."""
    remove_ad_and_cookie_overlays(page)
    safe_evaluate(page, "window.scrollTo(0, 0)")
    page.wait_for_timeout(500)

    dropdown_btn = page.locator(DROPDOWN_SELECTOR).first
    if dropdown_btn.count() == 0:
        print("  -> No dropdown button available.")
        return None

    try:
        dropdown_btn.click(force=True, timeout=4000)
    except Exception:
        if safe_evaluate(page, "() => { const b = document.querySelector('button:has(i.icon-caret_down)'); if(b) b.click(); }") is None:
            return None

    page.wait_for_timeout(1000)
    return page.locator(OPTIONS_SELECTOR).all()

def list_innings_options(page):
    """[(position, label)] for every view in the dropdown, including super overs."""
    options = open_dropdown(page)
    if not options:
        return []

    entries = []
    for position, opt in enumerate(options):
        try:
            txt = opt.inner_text().strip()
        except Exception:
            continue
        name = txt.split('\n')[0].strip()
        if not name or "feedback" in name.lower():
            continue
        entries.append((position, name))

    try:
        page.mouse.click(10, 10)   # close the popup again
    except Exception:
        pass
    page.wait_for_timeout(400)
    return entries

def switch_to_innings_option(page, position, expected_label):
    """Select the view at a given dropdown POSITION (not label)."""
    options = open_dropdown(page)
    if not options or position >= len(options):
        return False

    try:
        actual = options[position].inner_text().strip().split('\n')[0].strip()
    except Exception:
        actual = ""
    if actual and expected_label and actual != expected_label:
        print(f"  -> note: option {position} reads '{actual}' (expected '{expected_label}')")

    print(f"\n[+] Switching to innings view {position + 1}: '{expected_label}'...")
    try:
        options[position].click(force=True)
        page.wait_for_timeout(3500)
        return True
    except Exception as e:
        print(f"  ⚠ Could not click option {position}: {e}")
        try:
            page.mouse.click(10, 10)
        except Exception:
            pass
        return False

def scroll_active_feed(page, label, view_start, max_scrolls=140):
    """
    Scroll until this view's ball 0.1 shows up (or the feed stops growing). The
    view_start index lets us only look for the opening ball of *this* innings, so a
    6-ball super over stops after a couple of scrolls instead of idling 15 times.
    """
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

            # Stop only on 0.1. Deliberately NOT on 1.1: the feed arrives newest-first,
            # so 1.x is seen a full over BEFORE 0.x and stopping there would drop the
            # opening over of the innings (and of a super over, which is only 6 balls).
            this_view = all_captured_balls[view_start:]
            if any(str(b.get("oversActual", "")).strip() == "0.1" for b in this_view):
                print(f"  -> Reached start of innings for '{label}'.\n")
                return current_count - view_start
        else:
            stagnant_count += 1

        if stagnant_count >= 6:
            print(f"  -> Feed stopped growing for '{label}' ({current_count} total match balls captured).\n")
            break

    return len(all_captured_balls) - view_start

def tag_view(view_start, view_end, label):
    """Attribute the balls captured for one view to that innings / super over."""
    flag = is_super_over_label(label)
    tagged = 0
    for b in all_captured_balls[view_start:view_end]:
        if b.get("_viewLabel") is None:
            b["_viewLabel"] = label
            b["_isSuperOver"] = flag
            tagged += 1
    print(f"  🏷 tagged {tagged} deliveries to view '{label}' {'(SUPER OVER)' if flag else ''}")

print("[+] Launching Chromium browser...")
try:
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
        print(f"[+] Loading match commentary: {match_url}")
        page.goto(match_url, wait_until="domcontentloaded", timeout=60000)
        try:
            # espncricinfo keeps redirecting/hydrating after domcontentloaded; let the final
            # URL settle so the JS context is not destroyed under the next evaluate()
            page.wait_for_load_state("load", timeout=20000)
        except Exception:
            pass
        if "espncricinfo.com" not in page.url:
            print(f"  -> note: page.url is {page.url}")
        page.wait_for_timeout(3500)
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
        expected_super_overs = []
        try:
            sc_page = context.new_page()
            sc_page.goto(scorecard_url, wait_until="domcontentloaded", timeout=30000)
            sc_page.wait_for_timeout(2000)

            sc_json_str = safe_evaluate(sc_page, '() => document.getElementById("__NEXT_DATA__") ? document.getElementById("__NEXT_DATA__").textContent : null')
            if sc_json_str:
                sc_json = json.loads(sc_json_str)
                find_players_anywhere(sc_json)
                extract_direct_match_header_json(sc_json)
                expected_super_overs = sorted(set(find_super_over_innings(sc_json)))

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
        if expected_super_overs:
            print(f"[+] Scorecard reports {len(expected_super_overs)} super-over innings: {expected_super_overs}")
        print("=" * 60)

        # 3. Walk EVERY view in the innings dropdown. Positions, not labels: a tied match
        #    lists "Super Over" / "1st Super Over" entries, sometimes twice with the same
        #    label (once per side), which label-matching silently skips.
        dropdown_views = list_innings_options(page)
        current_label = get_current_button_text(page)
        if dropdown_views and dropdown_views[0][0] == 0:
            # position 0 is whatever is already on screen; prefer the live button label
            label0 = current_label if current_label not in ("", "Default Innings") else dropdown_views[0][1]
            all_innings_views = [(0, label0)] + dropdown_views[1:]
        else:
            all_innings_views = [(0, current_label)] + list(dropdown_views)

        print(f"[+] Innings views found: {[lab for _, lab in all_innings_views]}")
        if expected_super_overs and not any(is_super_over_label(lab) for _, lab in all_innings_views):
            print("⚠️  Scorecard has a super over but NO dropdown view was labelled as one -")
            print("   the tie-breaker may be missing from this CSV. Check the innings selector manually.")

        for step, (position, label) in enumerate(all_innings_views, start=1):
            view_start = len(all_captured_balls)
            print("=" * 60)
            try:
                if step == 1:
                    print(f"[+] STEP {step}: Processing default loaded view: '{label}'")
                else:
                    if not switch_to_innings_option(page, position, label):
                        print(f"[-] Skipping view {position} ('{label}') - could not select it")
                        continue
                    print(f"[+] STEP {step}: Processing switched view: '{label}'")
                print("=" * 60)
                scroll_active_feed(page, label=label, view_start=view_start)
            except Exception as view_err:
                # keep whatever this view already yielded: super overs are the LAST views, so
                # one uncaught error earlier in the loop used to lose them without a trace
                print(f"[-] View '{label}' errored: {view_err} - keeping its partial data")
            tag_view(view_start, len(all_captured_balls), label)

        browser.close()
except KeyboardInterrupt:
    print("\n⚠ Interrupted by user - keeping the deliveries captured so far...")
except Exception as run_err:
    print(f"\n⚠ Browser phase ended early: {run_err}")
    print("   Salvaging the deliveries captured so far instead of losing the whole run...")
    import traceback; traceback.print_exc()

# ==== DATAFRAME ASSEMBLY ====
def _is_missing(v):
    if v is None:
        return True
    if isinstance(v, (list, dict, tuple, set)):
        return len(v) == 0
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False

def over_parts(val):
    m = re.match(r'^(\d+)(?:[.](\d+))?$', str(val).strip())
    if not m:
        return (0, 0)
    return (int(m.group(1)), int(m.group(2)) if m.group(2) else 0)

def build_dataframe(balls, players, metadata, csv_path):
    df = pd.DataFrame(balls)

    if "inningNumber" in df.columns:
        df["inningNumber"] = pd.to_numeric(df["inningNumber"], errors="coerce").fillna(0).astype(int)
    else:
        df["inningNumber"] = 0

    if "_isSuperOver" in df.columns:
        df["isSuperOver"] = df.pop("_isSuperOver").fillna(False).astype(bool)
    else:
        df["isSuperOver"] = False
    if "_viewLabel" in df.columns:
        df["inningLabel"] = df.pop("_viewLabel")
    else:
        df["inningLabel"] = None
    df["inningType"] = df["isSuperOver"].map({True: "Super Over", False: "Regular"})

    df.insert(0, "venue", metadata["venue"])
    df.insert(0, "matchName", metadata["matchName"])
    df.insert(0, "tournamentName", metadata["tournamentName"])

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
                lambda x: players.get(int(x), {}).get("name") if pd.notna(x) and int(x) in players else None
            )

            if role in ["batsman", "nonStriker"]:
                df[f"{role}BattingStyle"] = df[id_col].map(
                    lambda x: players.get(int(x), {}).get("battingStyle") if pd.notna(x) and int(x) in players else None
                )
            elif role == "bowler":
                df[f"{role}BowlingStyle"] = df[id_col].map(
                    lambda x: players.get(int(x), {}).get("bowlingStyle") if pd.notna(x) and int(x) in players else None
                )
                df[f"{role}BowlingHand"] = df[id_col].map(
                    lambda x: players.get(int(x), {}).get("bowlingHand") if pd.notna(x) and int(x) in players else None
                )

            df.drop(columns=[id_col], inplace=True, errors="ignore")

    # Coalesce duplicates instead of dropping them: the same super-over ball is often
    # seen twice (once half-filled by the page load, once complete from the feed).
    if "id" in df.columns and df["id"].notna().all():
        # never de-duplicate on `id` alone: the tie-breaker feed restarts its counter, so a
        # super-over ball can carry the same id as a regular delivery and be merged away
        # silently (this is how super overs vanished with no error at all).
        keys = df["inningNumber"].astype(str) + "|" + df["id"].astype(str)
    else:
        keys = df["inningNumber"].astype(str) + "|" + df["oversActual"].astype(str) + "|" + df.get("title", pd.Series([""] * len(df))).astype(str)
    ordered = list(zip(keys.tolist(), range(len(df))))
    keep_idx, by_key = [], {}
    records = df.to_dict("records")
    for key, idx in ordered:
        if key not in by_key:
            by_key[key] = idx
            keep_idx.append(idx)
            continue
        target = by_key[key]
        for field, value in records[idx].items():
            if _is_missing(records[target].get(field)) and not _is_missing(value):
                records[target][field] = value
    df = pd.DataFrame([records[i] for i in keep_idx])

    # Sort on real numbers: 'oversActual' is a string, so 10.1 sorted before 2.5.
    parts = df["oversActual"].map(over_parts) if "oversActual" in df.columns else pd.Series([(0, 0)] * len(df))
    df["_over"] = [p[0] for p in parts]
    df["_ball"] = [p[1] for p in parts]
    df = df.sort_values(["isSuperOver", "inningNumber", "_over", "_ball"]).drop(columns=["_over", "_ball"])

    df.to_csv(csv_path, index=False)
    return df

if all_captured_balls:
    df = build_dataframe(all_captured_balls, player_map, match_metadata, output_csv)

    super_rows = int(df["isSuperOver"].sum()) if "isSuperOver" in df.columns else 0
    print("=" * 60)
    print(f"✅ Extracted {len(df)} total deliveries across ALL innings!")
    print(f"✅ {super_rows} of them are SUPER OVER deliveries" if super_rows else "ℹ️  No super over in this match (or no super-over view was found)")
    print(f"✅ Saved full dataset to '{output_csv}'")
    print("=" * 60)
else:
    print("[!] No deliveries were captured.")
'''

# Save the multi-purpose runner script once
worker_file = "scroll_dropdown_superover_worker.py"
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
        match_url = normalise_url(match_url)
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
                    kind = "SUPER OVER" if "isSuperOver" in inn_df.columns and bool(inn_df["isSuperOver"].iloc[0]) else "regular"
                    print(f"  * Innings {inn} [{kind}]: {count:4d} deliveries (overs {inn_df['oversActual'].min()} -> {inn_df['oversActual'].max()})")
            print("-" * 40)

            sample_cols = [c for c in [
                "tournamentName", "matchName", "venue", "oversActual", "inningType",
                "batsmanName", "batsmanBattingStyle",
                "bowlerName", "bowlerBowlingStyle", "bowlerBowlingHand"
            ] if c in df_match.columns]

            print("\n--- SAMPLE ENRICHED DATA ---")
            print(df_match[sample_cols].dropna().head(3).to_string(index=False))

            if "isSuperOver" in df_match.columns and df_match["isSuperOver"].any():
                print("\n--- SUPER OVER SAMPLE ---")
                so_cols = [c for c in ["oversActual", "bowlerName", "batsmanName", "title", "text"]
                           if c in df_match.columns]
                print(df_match[df_match["isSuperOver"]][so_cols].head(8).to_string(index=False))
            print("-" * 40)
        else:
            print(f"[-] Subprocess closed (exit code {process.returncode}), but '{output_csv}' was not generated.")
            print("    Scroll up: the worker now prints why it stopped instead of dying quietly.")

    except Exception as match_err:
        print(f"❌ Failed to process Match {i} due to error: {type(match_err).__name__}: {match_err}")
        import traceback; traceback.print_exc()
        continue

print("\n" + "=" * 80)
print("QUEUE COMPLETED SUCCESSFULLY!")
print("=" * 80)
