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

                    # --- Super Over tagging ---
                    # Stamp the ball with whatever dropdown/tab label was
                    # active in the browser at the moment it was captured.
                    # This lets us reliably tell Super Over deliveries apart
                    # from normal innings 1/2 deliveries downstream, even
                    # though ESPNcricinfo just keeps incrementing
                    # inningNumber (3, 4, ...) for Super Overs under the hood.
                    label = CURRENT_LABEL.get("value")
                    c_flat["inningsLabel"] = label
                    c_flat["isSuperOver"] = bool(
                        is_super_over_label(label) or
                        (isinstance(c_flat.get("inningNumber"), (int, float)) and c_flat.get("inningNumber", 0) > 2)
                    )
                    so_num = extract_super_over_number_from_label(label)
                    if so_num is None and c_flat["isSuperOver"]:
                        # Fall back to deriving Super Over # from inningNumber
                        # (inningNumber 3 -> Super Over 1, 4 -> Super Over 2, ...)
                        try:
                            so_num = int(c_flat.get("inningNumber")) - 2
                        except (TypeError, ValueError):
                            so_num = None
                    c_flat["superOverNumber"] = so_num

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

def remove_ad_and_cookie_overlays(page):
    page.evaluate("""() => {
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
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(500)
    
    dropdown_btn = page.locator("button:has(i.icon-caret_down), button:has(span.ds-text-button-3)").first
    if dropdown_btn.count() == 0:
        print("  -> No dropdown button available.")
        return None
        
    try:
        dropdown_btn.click(force=True, timeout=4000)
    except Exception:
        page.evaluate("() => { const b = document.querySelector('button:has(i.icon-caret_down)'); if(b) b.click(); }")
        
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
        
        page.evaluate("""() => {
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
    print(f"[+] Loading match commentary: {match_url}")
    page.goto(match_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3500)
    remove_ad_and_cookie_overlays(page)

    try:
        next_json_str = page.evaluate('() => document.getElementById("__NEXT_DATA__") ? document.getElementById("__NEXT_DATA__").textContent : null')
        if next_json_str:
            next_json = json.loads(next_json_str)
            all_captured_balls.extend(parse_comments(next_json))
            find_players_anywhere(next_json)
            extract_direct_match_header_json(next_json)
    except Exception:
        pass

    dom_meta = page.evaluate(r"""() => {
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
    }""")
    
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
        
        sc_json_str = sc_page.evaluate('() => document.getElementById("__NEXT_DATA__") ? document.getElementById("__NEXT_DATA__").textContent : null')
        if sc_json_str:
            sc_json = json.loads(sc_json_str)
            find_players_anywhere(sc_json)
            extract_direct_match_header_json(sc_json)

        sc_dom_meta = sc_page.evaluate(r"""() => {
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
        }""")
        
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
                    print(f"  * Innings {inn}{tag}: {count:4d} deliveries (overs {inn_df['oversActual'].min()} -> {inn_df['oversActual'].max()})")
            print("-" * 40)
            
            sample_cols = [c for c in [
                "tournamentName", "matchName", "venue", "isSuperOver", "superOverNumber", "oversActual",
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
