# Local AI Visibility Grid (Google AI Overviews + Maps)

**For local SEO agencies, freelancers and local business owners.** Customers now ask Google *"best emergency plumber near me"* and read the **AI Overview** before they ever see the map. This Actor asks your customers' questions **at every point of a map grid** and tells you where Google's AI names your business, where it names competitors instead, and which websites it trusts as sources.

**Outcome:** a heat grid of your **AI visibility share** per neighbourhood, next to your **Google Maps rank** at the same spot, so you can see where you rank on the map but are missing from the AI answer (and the other way round).

- 🤖 **Google AI Overviews** at each grid point, logged out, location pinned with Google's `uule` parameter (the same idea geo-grid rank trackers use).
- 🧭 **Named businesses matched to Google Maps place IDs.** Uses Google's own business cards in the answer (exact IDs), then phone and address. Other branches of a chain are flagged, never counted as you.
- 📍 **Your position in the answer** (1st, 2nd, 3rd business named) and whether your own site is linked.
- 🔗 **Cited sources:** real URLs and domains (Google's redirect links resolved), typed as business site, directory/review, forum, social, listicle.
- 🗺️ **Maps rank at the same point** for the same question, so AI and map visibility sit side by side.
- 🔁 **Several samples per point:** AI answers change between asks; 2–3 samples give a steadier share.
- 🧪 **Google AI Mode (beta, optional, free).** Off by default and **not charged**. Google often blocks automated AI Mode visits; after 4 blocks in a row it is switched off for the rest of the run.
- 📊 **REPORT.html + CSV + SUMMARY:** visibility share per point, per question and per engine, top competitors, top cited domains.
- 💸 **Pay only for real AI Overview answers** and successful Maps rank checks. No-answer, blocked and error rows are free. No login, no API key.

**Need done-for-you setup or a recurring client report?** → [Fiverr Apify scrape gig](https://www.fiverr.com/latent_lark/scrape-website-data-with-apify-into-clean-csv-excel-or-google-sheets) + [Fiverr n8n automation gig](https://www.fiverr.com/latent_lark/build-n8n-ai-automation-workflows-for-your-business)

## Quick start (first run in about a minute)
1. Click **Try for free** (or **Start** in Console). The form is prefilled with `William Parrish Plumbing` in `Raleigh, NC`, the question `best emergency plumber near me` and **3 points**: downtown, 8 km north and 8 km west.
2. Click **Start**. You get 3 rows in **Output** and **REPORT.html** in **Storage → Key-value store**. It finishes in about 1–2 minutes and costs about **$0.05** if all 3 points return an AI Overview and a Maps check (3 × $0.015 + 3 × $0.002).
3. Put in your own business and city, **clear the Points field**, and pick a grid (3×3 for a town, 5×5 for a metro area). Add 2–3 questions and 2 samples per point.

**Tip:** paste the business's **Google Maps URL** for an exact match. It matters for chains and franchises with many branches.

## What you can do with it
- **Local SEO audits:** "You rank #1 on the map downtown but Google's AI never names you 8 km north." One picture, no spreadsheet.
- **AI visibility reporting:** run the same grid weekly and track the share over time.
- **Competitor intel:** which businesses AI recommends in each part of town, with Maps place IDs, rating and reviews.
- **Citation gap:** which directories, review sites and blogs the AI cites, so you know where to get listed or reviewed.
- **Before/after proof:** run before and after Google Business Profile, review or website changes.
- Pair it with [Google Maps Rank Tracker (Geo-Grid Local SEO Heatmap)](https://apify.com/latentlark/google-maps-rank-tracker) for a deep Maps grid.

## Input
| Field | What it does |
|---|---|
| `businessName` + `city` / `mapsUrl` / `placeId` | The business to check. Maps URL or Place ID is the most precise |
| `questions` | What customers type into Google (up to 10). Each question is asked at every point |
| `points` | Exact `lat,lng` points (optional `|label`). Overrides the grid. Clear it to use the grid |
| `center`, `gridSize`, `gridSpacingKm` / `radiusKm` | N×N grid (1, 3, 5 or 7 per side). Empty center = the business's location |
| `samplesPerPoint` | How many times each question is asked per point (1–5, default 2) |
| `includeAiOverviews` | Google AI Overviews (default on) |
| `includeAiMode` | Google AI Mode, **beta and free**, default off |
| `includeMapsRank` / `mapsKeyword` | Maps rank (top 20) per point × question. Optional fixed Maps keyword, e.g. `plumber` |
| `maxAnswers` | Safety cap on paid AI Overview answers per run. The run stops cleanly when reached |
| `maxConcurrency` | Parallel browsers (1–4). **Default 1** is more reliable; 2 is faster but may raise blocks |
| `country` / `language` | Google `gl` / `hl` codes (US English works best for local AI Overviews) |
| `saveScreenshots` | Save a screenshot of every answer page (proof for clients) |
| `proxyConfiguration` | **Optional fallback only.** Leave off for normal use |

Example (3×3 grid, 2 questions, 2 samples = up to 36 answers):
```json
{
  "businessName": "William Parrish Plumbing",
  "city": "Raleigh, NC",
  "questions": ["best emergency plumber near me", "water heater repair near me"],
  "gridSize": 3,
  "gridSpacingKm": 5,
  "samplesPerPoint": 2,
  "mapsKeyword": "plumber",
  "maxAnswers": 40
}
```

The prefilled Try-it example is smaller: 3 points × 1 question × 1 sample (about **$0.05** if all three return an answer).

## Output
One dataset item per **point × question × sample** (and engine):
```json
{
  "engine": "google_aio", "pointLabel": "center", "lat": 35.78045, "lng": -78.63518,
  "question": "best emergency plumber near me", "sample": 1,
  "status": "answer", "answerFound": true, "charged": true,
  "targetMentioned": true, "targetPosition": 1, "targetOtherBranchMentioned": false,
  "businessesNamed": 5,
  "businesses": [
    { "position": 1, "name": "William Parrish Plumbing", "placeId": "ChIJeYDs30-TrIkRu3swF0b7nxM",
      "hexId": "0x89ac934fdfec8079:0x139ffb4617307bbb", "address": "235 New Bern Pl #301, Raleigh, NC 27601",
      "rating": 4.9, "reviewCount": 2112, "matchMethod": "google_card_hex_id", "isTarget": true },
    { "position": 2, "name": "Out The Door Plumbing", "placeId": "ChIJaddeACeLrIkRPBUfEJJ1qsQ",
      "rating": 4.9, "reviewCount": 511, "matchMethod": "google_card_hex_id", "isTarget": false }
  ],
  "citedDomains": ["michaelandson.com", "outthedoorplumbing.com", "williamparrishplumbing.com", "yelp.com"],
  "sourceDomains": ["yelp.com"],
  "citedDomainTypes": { "yelp.com": "directory/review", "outthedoorplumbing.com": "named business site" },
  "targetSiteCited": true,
  "mapsRank": 1, "mapsKeyword": "best emergency plumber near me",
  "answerText": "AI Overview\nTop-rated emergency plumbers in the Raleigh, NC area ..."
}
```
- `status`: `answer` (an AI Overview was parsed; Google AI Overviews are charged, AI Mode is free), `no_answer` (Google showed no AI Overview there, free), `blocked` (free), `error` (free).
- `citedDomains` = every site linked in the answer, including the businesses' own sites. `sourceDomains` = sources linked outside the business cards.
- `matchMethod`: `google_card_hex_id` (exact ID from Google's own card in the answer) or `maps_name_match (score)` (name looked up on Maps near that point).

The key-value store also has:
- `REPORT.html`: visibility heat grid (share % + Maps rank per point), per question, top competitors, top cited sites.
- `SUMMARY`: the same as JSON: `visibilitySharePct`, `engines`, `perPoint`, `perQuestion`, `topCompetitors`, `topCitedDomains`.
- `REPORT.csv`: one line per check for Google Sheets or Excel.

## Run it on a schedule
1. Fill in your business, questions and grid, then **Save as task**.
2. **Schedule** it weekly, e.g. Monday `0 7 * * 1`.
3. Send results to a sheet with [Google Sheets Export](https://apify.com/latentlark/sheets-export), or to Slack/email via **Integrations**.

## Pricing
Pay per event. You only pay for **successful Google AI Overview answers** and **successful Maps rank checks**. No-answer, blocked and error rows are free. Google AI Mode is beta and **not charged**.

| Event | When it fires | Price |
|---|---|---|
| `ai-answer-google-aio` | Google showed an AI Overview and we parsed it | **$0.015** |
| `maps-rank-check` | A Maps rank lookup at that point succeeded | **$0.002** |

Set a **maximum cost per run** in the run options and use `maxAnswers` as a second cap.

Example costs at these prices (upper bound if every check returns an answer):

| Run | Answers | Maps checks | Cost to you |
|---|---|---|---|
| Prefill / Try-it (3 points × 1 question × 1 sample) | ≤3 | 3 | ≈ **$0.051** |
| 3×3 grid × 2 questions × 2 samples | ≤36 | 18 | ≈ $0.58 |
| 5×5 grid × 3 questions × 2 samples | ≤150 | 75 | ≈ $2.40 |
| 7×7 grid × 3 questions × 3 samples | ≤441 | 147 | ≈ $6.92 |

## How it works and limitations
- Each check is a logged-out browser visit to Google. The searcher's location is pinned to that grid point with Google's `uule` parameter — not browser geolocation. One browser context (and the consent cookie) is reused across the grid. Requests are paced with a short delay by default.
- **No proxy is needed in normal use.** The Actor runs on Apify's US servers. If a large grid returns many `blocked` rows, you can turn on a proxy as an **optional fallback** (residential is the usual choice). Proxy traffic is billed separately by Apify and is not included in the event prices above.
- **Google doesn't show an AI Overview for every question, every time.** Those rows come back as `no_answer` and are free. Commercial "near me" questions get local AI Overviews most often in the US.
- AI answers vary between asks and through the day. Use 2–3 samples per point and compare shares, not single answers.
- **AI Mode is beta and free.** Google blocks many automated AI Mode visits. After 4 blocks in a row AI Mode is switched off for the rest of the run (still not charged).
- Each answer page is heavy (about 5 MB, often 10–25 s plus pacing), so big grids take a while: a 5×5 × 3 questions × 2 samples run takes on the order of 30–40 minutes at 2 parallel browsers, longer at the default of 1.
- Public Google results only. No login, no personal data. Google can change its page layout; we monitor the parser.

## FAQ
**Every row says `no_answer`.** Google didn't show an AI Overview for that question there. Try a more commercial wording ("best … near me", "… company in [city]"), keep `country` = `us`, and use 2 samples.

**My business is never named, but it ranks on Maps.** That is the gap this Actor shows. Check `topCitedDomains`: AI often recommends businesses with strong reviews on the cited directories and a clear website.

**It says another branch was named.** `targetOtherBranchMentioned` = a business with your brand name but a different Google listing (another franchise location). It is not counted as you.

**The run returned one row with an `error`.** The input could not be used (no question, or the business or center could not be found). The run still succeeds and that row is free. Paste the Google Maps URL and try again.

**Is a proxy needed?** No. Leave it off. Use a proxy only as a fallback if you see many blocked rows on a large grid.

**Why was a row not charged?** Only parsed Google AI Overview answers (`ai-answer-google-aio`) and successful Maps lookups (`maps-rank-check`) are billed. Blocked, timed-out, no-answer and AI Mode rows are free.

## More from LatentLark
- [Google Maps Rank Tracker (Geo-Grid Local SEO Heatmap)](https://apify.com/latentlark/google-maps-rank-tracker): Maps rank at every point of an N×N grid with a heatmap.
- [Google Maps No-Website Leads (Audit & Pitch)](https://apify.com/latentlark/local-lead-audit): audit local business websites and get a cold-email pitch.
- [Google Local Services Ads Scraper](https://apify.com/latentlark/google-local-services-ads): who runs Google-guaranteed ads in a city.
- [Google Sheets Export & Import (No OAuth)](https://apify.com/latentlark/sheets-export): send any Actor's results to a Google Sheet on a schedule.
