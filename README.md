# WT20_winprob
Women's Twenty20 Cricket Win Probability Model &amp; results
Trained on, and suitable to use on Women's Big Bash League, Women's Super League, Women's T20 Challenge, Women's Premier League, and the Women's Super Smash League
Not suitable to use on the Women's Caribbean Premier League or the T20 Blaze as the levels of competition are too different.

## Ball-by-ball scraper (`scrape_cricinfo_balls.py`)

Pulls ESPNcricinfo delivery data (including **Super Overs**) into one CSV per match,
named `<matchId>.csv`, with batsman/bowler names and playing styles resolved.

```bash
pip install playwright pandas
playwright install chromium

python scrape_cricinfo_balls.py                                   # uses MATCH_URLS in the file
python scrape_cricinfo_balls.py <commentary-or-scorecard-url> ...
python scrape_cricinfo_balls.py --urls-file matches.txt --out-dir data/
python scrape_cricinfo_balls.py --api-only <url>                  # no scrolling at all
python scrape_cricinfo_balls.py --no-api <url>                    # dropdown crawler only
```

Super Overs are *discovered* rather than assumed: every innings the scorecard payload
flags as a super over (any numbering, `isSuperOver` / `superOvers[]` / `nameId:
"super-over"` / a "Super Over"/"Super 5"-style label) is added to the fetch plan, and a
tied match whose tie-breaker isn't listed still gets its trailing innings numbers probed.

Columns that matter for modelling:

| column | meaning |
| --- | --- |
| `inningNumber` | Cricinfo innings number (tie-breakers keep their own, higher numbers) |
| `inningType` / `isSuperOver` | `Regular`/`Super Over`, and the boolean form |
| `superOverNumber` | which tie-breaker a row belongs to (1, 2, ... if one super over is still tied) |
| `inningLabel` | the label Cricinfo gave that innings |
| `overNumber`, `ballNumber`, `oversActual` | over, ball-within-over, and the original string |

The win-probability model here is trained on full 20-over innings, so filter on
`isSuperOver == False` (or model super overs separately) before feeding these CSVs to
it -- a 6-ball innings with a 100% win probability at ball 6 would otherwise distort
match-level state estimation.

Tests (no network, no browser): `python -m unittest discover -s tests`
