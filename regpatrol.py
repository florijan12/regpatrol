"""
RegPatrol - Daily Job (Exa edition)
====================================
Runs two tasks in sequence:
  1. Queries Exa for fresh regulatory updates -> saves to Airtable
  2. Pulls Netlify form submissions -> adds new subscribers to Airtable

Runs locally. Exa free tier covers daily runs comfortably.

Setup:
    pip install requests exa-py pyairtable python-dotenv beautifulsoup4

Create a `.env` file next to this script:
    AIRTABLE_TOKEN=pat...
    EXA_API_KEY=...
    NETLIFY_TOKEN=nfp_...

Usage:
    python regpatrol.py

Adjust LOOKBACK_DAYS in the config to change how far back results are kept.
"""

import os
import re
import sys
import requests
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from exa_py import Exa
from pyairtable import Api
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Load tokens from .env file in the same directory as the script
load_dotenv()

# ═════════════════════════════════════════
# CONFIGURATION
# ═════════════════════════════════════════

# --- Tokens loaded from .env (never hard-code these) ---
AIRTABLE_TOKEN = os.getenv("AIRTABLE_TOKEN", "")
EXA_API_KEY    = os.getenv("EXA_API_KEY", "")
NETLIFY_TOKEN  = os.getenv("NETLIFY_TOKEN", "")

# --- Non-secret config (safe to keep here) ---
AIRTABLE_BASE_ID  = "appHE7piNN3WdvGcu"
ALERTS_TABLE      = "Regulert"
SUBSCRIBERS_TABLE = "Subscribers"
NETLIFY_SITE      = "regpatrol.com"

# --- How far back to look ---
# Keep results published within the last N days.
# Lower this number once you're getting daily volume of new items.
LOOKBACK_DAYS = 60

# --- Debug: print every result and why it was kept or rejected ---
# Set to True to see exactly which URLs/titles failed which filter.
# Useful when tuning a new source. Set back to False for normal runs.
DEBUG_FILTERS = False

# ─────────────────────────────────────────
# What to search for, per market
# ─────────────────────────────────────────

EXA_QUERIES = [
    # NO FILTERS — every Exa result is saved. Use this to evaluate raw output
    # and decide what filtering rules make sense for each market. Add filter
    # keys (url_must_contain, url_must_not_contain, title_must_contain) back
    # to individual queries once you know what to filter.
    {
        "query": "FDA medical device guidance document",
        "source": "FDA Guidance",
        "category": "Guidance",
        "include_domains": ["fda.gov"],
        "num_results": 10,
        # Stay strictly in CDRH territory — block biologics, drugs, food, vaccines, etc.
        "url_must_contain": [
            "/medical-devices/",
            "/regulatory-information/search-fda-guidance-documents/",
            "/cdrh/",
        ],
        "url_must_not_contain": [
            "/vaccines-blood-biologics/", "/drugs/", "/food/",
            "/animal-veterinary/", "/tobacco-products/", "/cosmetics/",
            "/dietary-supplements/", "/radiation-emitting-products/",
        ],
        "title_must_contain": [
            "medical device", "device", "ivd", "in vitro", "samd",
            "510(k)", "premarket", "udi", "cdrh", "diagnostic",
            "implant", "instrument",
        ],
    },
    {
        "query": "FDA medical device safety communication",
        "source": "FDA Safety",
        "category": "Safety",
        "include_domains": ["fda.gov"],
        "num_results": 10,
        # Same scope — CDRH safety communications only
        "url_must_contain": [
            "/medical-devices/",
            "/safety-communications/",
            "/cdrh/",
        ],
        "url_must_not_contain": [
            "/vaccines-blood-biologics/", "/drugs/", "/food/",
            "/animal-veterinary/", "/tobacco-products/", "/cosmetics/",
            "/dietary-supplements/", "/radiation-emitting-products/",
        ],
        "title_must_contain": [
            "medical device", "device", "ivd", "in vitro", "samd",
            "implant", "diagnostic", "instrument", "monitor",
            "scanner", "pump",
        ],
    },
    # EU MDR updates are pulled from the official European Commission RSS feed
    # — see run_eu_mdr_updates() below. Skips Exa entirely for this market.
    {
        "query": "Health Canada medical device guidance",
        "source": "Health Canada Guidance",
        "category": "Guidance",
        "include_domains": ["canada.ca"],
        "num_results": 10,
    },
    {
        "query": "MHRA medical device guidance UK",
        "source": "UK MHRA Guidance",
        "category": "Guidance",
        "include_domains": ["gov.uk"],
        "num_results": 10,
    },
    # ISO standards monitoring — DISABLED for now.
    # Reason: iso.org doesn't reliably index individual standards in Exa,
    # and the page structure makes neural search noisy. Re-enable when we
    # find a reliable feed (ISO RSS / IEC RSS / or paid API).
    # {
    #     "query": "ISO medical device standard published",
    #     "source": "ISO Standard",
    #     "category": "Guidance",
    #     "include_domains": ["iso.org"],
    #     "num_results": 10,
    # },
    # TGA (Australia) — DISABLED: TGA's RSS feeds consistently time out from
    # outside Australia. Handler code lives in run_tga_feeds() below but is not
    # called from main(). Re-enable when a reliable transport is available.
    # BfArM Field Corrective Actions are pulled from the official BfArM RSS feed
    # — see run_bfarm_fcas() below. Skips Exa entirely for this market.
]


# ═════════════════════════════════════════
# TASK 1 — EXA REGULATORY MONITOR
# ═════════════════════════════════════════

def search_exa(exa, cfg):
    """Run a single Exa search and return normalised alert dicts."""
    print(f"\n🔍 Exa search: {cfg['source']}")
    try:
        # Ask Exa for items published within the lookback window
        start_date = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        results = exa.search_and_contents(
            cfg["query"],
            type="auto",
            category="news",
            num_results=cfg["num_results"],
            include_domains=cfg["include_domains"],
            highlights=True,
            start_published_date=start_date,
        )
    except Exception as e:
        print(f"  ❌ Exa search failed: {e}")
        return []

    url_must_contain       = [s.lower() for s in cfg.get("url_must_contain", [])]
    url_must_not_contain   = [s.lower() for s in cfg.get("url_must_not_contain", [])]
    title_must_contain     = [s.lower() for s in cfg.get("title_must_contain", [])]

    alerts = []
    skipped_filter = 0
    skipped_title  = 0
    skipped_date   = 0

    for r in results.results:
        title = (r.title or "").strip()
        url   = (r.url or "").strip()
        if not title or not url:
            if DEBUG_FILTERS:
                print(f"    🚫 SKIP (empty title/url): {title[:60] or '(no title)'}")
            continue

        # Filter 1: drop generic page titles ("Public Health - European Commission" etc.)
        if len(title) < 20:
            skipped_title += 1
            if DEBUG_FILTERS:
                print(f"    🚫 SKIP (title <20 chars): \"{title}\"  ← {url}")
            continue

        # Filter 2: drop landing/index pages (titles that match generic patterns)
        title_lower = title.lower()
        landing_page_patterns = [
            # FDA landing pages
            "safety communications - fda",
            "safety communications | fda",
            "medical device safety communications",
            "medical device recalls",
            "medical device guidance documents",
            "guidance documents (medical",
            # EU landing pages
            "endorsed documents and other guidance",
            "market surveillance and vigilance",
            "field safety corrective action - fsca",
            # Year-only "X Safety Communications" landing pages
        ]
        matched_landing = next((p for p in landing_page_patterns if p in title_lower), None)
        if matched_landing:
            skipped_title += 1
            if DEBUG_FILTERS:
                print(f"    🚫 SKIP (landing page \"{matched_landing}\"): \"{title[:70]}\"")
            continue
        # Year-prefixed landing pages like "2024 Safety Communications - FDA"
        if len(title) > 4 and title[:4].isdigit() and (
            "safety communications" in title_lower
            or "recalls" in title_lower
            or "guidance documents" in title_lower
        ):
            skipped_title += 1
            if DEBUG_FILTERS:
                print(f"    🚫 SKIP (year-prefixed index): \"{title[:70]}\"")
            continue

        url_lower = url.lower()

        # Filter: URL must contain at least one allowed pattern (if set)
        if url_must_contain and not any(p in url_lower for p in url_must_contain):
            skipped_filter += 1
            if DEBUG_FILTERS:
                print(f"    🚫 SKIP (URL missing required pattern): {url}")
                print(f"         needed one of: {url_must_contain}")
            continue

        # Filter: URL must not contain any blocked pattern (if set)
        blocked = next((p for p in url_must_not_contain if p in url_lower), None) if url_must_not_contain else None
        if blocked:
            skipped_filter += 1
            if DEBUG_FILTERS:
                print(f"    🚫 SKIP (URL contains blocked \"{blocked}\"): {url}")
            continue

        # Filter: title must contain at least one alert-like keyword (if set)
        if title_must_contain and not any(p in title_lower for p in title_must_contain):
            skipped_title += 1
            if DEBUG_FILTERS:
                print(f"    🚫 SKIP (title missing required keyword): \"{title[:70]}\"")
                print(f"         needed one of: {title_must_contain}")
            continue

        snippet = ""
        if getattr(r, "highlights", None) and r.highlights:
            snippet = r.highlights[0].strip()

        # Parse published date.
        raw_date = getattr(r, "published_date", None)
        if not raw_date:
            skipped_date += 1
            if DEBUG_FILTERS:
                print(f"    🚫 SKIP (no published date): \"{title[:70]}\"")
            continue
        pub_date = raw_date.split("T")[0] if "T" in raw_date else raw_date

        # Drop anything older than the lookback window
        cutoff = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        if pub_date < cutoff:
            skipped_date += 1
            if DEBUG_FILTERS:
                print(f"    🚫 SKIP (pub_date {pub_date} < cutoff {cutoff}): \"{title[:60]}\"")
            continue

        if DEBUG_FILTERS:
            print(f"    ✅ KEEP: \"{title[:70]}\"")

        alerts.append({
            "title":    title,
            "url":      url,
            "snippet":  snippet,
            "pub_date": pub_date,
            "source":   cfg["source"],
            "category": cfg["category"],
        })

    print(f"  ✅ {len(alerts)} results kept "
          f"(filtered out: {skipped_filter} URL, {skipped_title} title, {skipped_date} not today)")
    return alerts


def alert_exists(table, url):
    try:
        formula = f'{{FDA link}} = "{url}"'
        records = table.all(formula=formula, max_records=1)
        return len(records) > 0
    except Exception as e:
        print(f"  ⚠️  Airtable search error: {e}")
        return False


def extract_manufacturer_from_title(title):
    """Extract a manufacturer name from an alert title. Handles two common patterns:

    1. BfArM/MHRA style: "Urgent Field Safety Notice for <Product> by <Manufacturer>"
       → splits on ' by '
    2. FDA Recalls style: "Boston Scientific: ICD Premature Battery..."
       → splits on ': ' and takes the first part

    Returns a cleaned manufacturer string or empty string.
    """
    if not title:
        return ""
    t = title.strip()

    # Pattern 1: " by <Manufacturer>" — used by BfArM, sometimes MHRA
    # Stop at parens, brackets, or em/en-dashes (NOT regular hyphens, which appear
    # inside company names like "Carl-Zeiss-Meditec").
    by_match = re.search(r"\bby\s+(.+?)(?:\s*[\(\[–—]|\s*$)", t, re.IGNORECASE)
    if by_match:
        mfr = by_match.group(1).strip().rstrip(".,;:")
        return mfr[:80]

    # Pattern 2: "<Manufacturer>: <product>" — used by FDA Recalls
    if ":" in t:
        left = t.split(":", 1)[0].strip()
        # Heuristic: short enough to be a company name, not a long sentence,
        # and doesn't start with a generic alert-prefix word
        if (3 <= len(left) <= 80
                and not left.lower().startswith(("urgent", "class", "recall",
                                                  "notice", "alert", "safety",
                                                  "field", "important"))):
            return left

    return ""


def infer_product_group(text):
    """When a source doesn't expose a structured product category, infer one from
    the alert title and description using a keyword catalog. Returns a single
    canonical label or empty string if no confident match."""
    if not text:
        return ""
    t = text.lower()

    # Order matters: more-specific patterns come first
    catalog = [
        ("Cardiovascular",    ["pacemaker", "defibrillator", "cardiac", "icd ", "stent", "heart", "atrial", "ventricular", "coronary"]),
        ("Neurology",         ["deep brain stimulation", "neurostimulator", "neuro ", "spinal cord stimulator", "vagus nerve"]),
        ("Orthopaedics",      ["hip implant", "knee implant", "hip replacement", "knee replacement", "spinal screw", "bone cement", "prosth", "orthop"]),
        ("Radiology",         ["mri ", "ct scanner", "x-ray", "radiograph", "ultrasound", "mammograph", "imaging"]),
        ("In-vitro diagnostics", ["in vitro", "in-vitro", "ivd ", "assay", "test kit", "diagnostic kit", "immunoassay", "elisa", "pcr ", "immunolog"]),
        ("Surgical",          ["surgical", "scalpel", "laparoscop", "endoscop", "robotic surger"]),
        ("Infusion / Drug delivery", ["infusion pump", "syringe pump", "insulin pump", "drug delivery", "injector"]),
        ("Respiratory",       ["ventilator", "anesthesia", "anaesth", "cpap", "bipap", "breathing", "oxygen concentrator"]),
        ("Diabetes",          ["glucose monitor", "cgm ", "insulin", "blood glucose"]),
        ("Wound care",        ["wound", "dressing", "bandage"]),
        ("Ophthalmology",     ["intraocular", "contact lens", "cataract", "ophthalm"]),
        ("Dental",            ["dental", "implant abutment", "endodont", "orthodont"]),
        ("Hearing",           ["hearing aid", "cochlear implant"]),
        ("Software",          ["software as a medical device", "samd ", "mobile medical app", "clinical decision support"]),
    ]

    for label, keywords in catalog:
        if any(kw in t for kw in keywords):
            return label
    return ""


def save_alert(table, alert):
    try:
        fields = {
            "Title":           alert["title"],
            "Source":          alert["source"],
            "Device category": [alert["category"]],
            "FDA link":        alert["url"],
            "Published date":  alert["pub_date"],
            "Summary":         alert["snippet"],
            "Sent":            False,
        }
        # Optional Product group field (only if the source provided one)
        if alert.get("product_group"):
            fields["Product group"] = alert["product_group"]
        # Optional Manufacturer field
        if alert.get("manufacturer"):
            fields["Manufacturer"] = alert["manufacturer"]
        table.create(fields)
        print(f"  💾 Saved: {alert['title'][:60]}...")
        return True
    except Exception as e:
        print(f"  ❌ Failed to save: {e}")
        return False


def run_regulatory_monitor(api):
    print("\n" + "─" * 60)
    print("  TASK 1 — REGULATORY MONITOR (Exa)")
    print("─" * 60)

    if not EXA_API_KEY:
        print("  ⚠️  Skipping — EXA_API_KEY not set in .env.")
        return

    exa = Exa(api_key=EXA_API_KEY)
    table = api.table(AIRTABLE_BASE_ID, ALERTS_TABLE)

    total_new, total_skipped = 0, 0

    for cfg in EXA_QUERIES:
        alerts = search_exa(exa, cfg)
        for alert in alerts:
            if alert_exists(table, alert["url"]):
                total_skipped += 1
                continue
            if save_alert(table, alert):
                total_new += 1

    print(f"\n  📥 New alerts saved:   {total_new}")
    print(f"  ⏭️  Duplicates skipped: {total_skipped}")


# ═════════════════════════════════════════
# TASK 1b — HEALTH CANADA OPEN DATA RECALLS
# ═════════════════════════════════════════
# Pulled directly from Health Canada's official open data feed instead of via Exa.
# Source: https://open.canada.ca/data/en/dataset/d38de914-c94c-429b-8ab1-8776c31643e3
# Feed:   https://recalls-rappels.canada.ca/sites/default/files/opendata-donneesouvertes/HCRSAMOpenData.json

HC_FEED_URL = "https://recalls-rappels.canada.ca/sites/default/files/opendata-donneesouvertes/HCRSAMOpenData.json"


def run_health_canada_recalls(api):
    print("\n" + "─" * 60)
    print("  TASK 1b — HEALTH CANADA RECALLS (open data)")
    print("─" * 60)

    try:
        print(f"\n📡 Fetching: {HC_FEED_URL}")
        resp = requests.get(HC_FEED_URL, timeout=30)
        resp.raise_for_status()
        records = resp.json()
        print(f"  ✅ Got {len(records)} total records (all categories)")
    except Exception as e:
        print(f"  ❌ Failed to fetch Health Canada feed: {e}")
        return

    # Filter to medical devices only, within the lookback window
    cutoff = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    md_records = []
    for r in records:
        if r.get("Organization") != "Medical devices":
            continue
        last_updated = r.get("Last updated", "")
        if last_updated < cutoff:
            continue
        if r.get("Archived") == "1":
            continue
        md_records.append(r)

    print(f"  ✅ {len(md_records)} medical device recalls within last {LOOKBACK_DAYS} days")

    if not md_records:
        return

    table = api.table(AIRTABLE_BASE_ID, ALERTS_TABLE)
    total_new, total_skipped = 0, 0

    for r in md_records:
        url   = r.get("URL", "").strip()
        title = r.get("Title", "").strip()
        if not url or not title:
            continue

        if alert_exists(table, url):
            total_skipped += 1
            continue

        # Build a rich summary directly from the feed's structured fields
        # Order: recall class → device category → issue → product → action
        recall_class = r.get("Recall class", "").strip()
        category     = r.get("Category", "").strip()
        issue        = r.get("Issue", "").strip()
        product      = r.get("Product", "").strip()
        action       = r.get("What you should do", "").strip()
        summary_parts = []
        if recall_class:
            summary_parts.append(f"[{recall_class}]")
        if category:
            summary_parts.append(f"Category: {category}.")
        if issue:
            summary_parts.append(f"Issue: {issue}.")
        if product:
            summary_parts.append(f"Product: {product}.")
        if action:
            action_preview = action[:400] + ("..." if len(action) > 400 else "")
            summary_parts.append(f"Action: {action_preview}")
        summary = " ".join(summary_parts)

        # Health Canada record fields don't include a clean firm name field
        # for medical devices — derive from title where possible.
        manufacturer = (r.get("Firm", "") or r.get("Company", "")).strip() \
                       or extract_manufacturer_from_title(title)

        alert = {
            "title":         title,
            "url":           url,
            "snippet":       summary,
            "pub_date":      r.get("Last updated", datetime.now().strftime("%Y-%m-%d")),
            "source":        "Health Canada Recall",
            "category":      "Recall",
            "product_group": category or "",
            "manufacturer":  manufacturer,
        }

        if save_alert(table, alert):
            total_new += 1

    print(f"\n  📥 New alerts saved:   {total_new}")
    print(f"  ⏭️  Duplicates skipped: {total_skipped}")


# ═════════════════════════════════════════
# TASK 1c — FDA DEVICE RECALLS (direct scrape of curated FDA page)
# ═════════════════════════════════════════
# Scrapes FDA's official curated "Medical Device Recalls and Early Alerts" page.
# Every row gets a real consumer-facing FDA.gov URL — not an API endpoint, not
# a Google search link. Subscribers click through to the actual FDA page that
# describes the recall in plain English.
# Source: https://www.fda.gov/medical-devices/medical-device-safety/medical-device-recalls-and-early-alerts
#
# Volume: ~5-15 recalls/month (FDA-curated "most serious" — quality over quantity).
# For the full firehose, openFDA's enforcement endpoint is available but has no
# stable per-recall URLs (cfRES requires session state).

FDA_RECALLS_PAGE_URL = "https://www.fda.gov/medical-devices/medical-device-safety/medical-device-recalls-and-early-alerts"


def parse_fda_recall_date(text):
    """FDA dates on the curated page are MM/DD/YYYY. Return YYYY-MM-DD, or None."""
    text = (text or "").strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def run_fda_recalls(api):
    print("\n" + "─" * 60)
    print("  TASK 1c — FDA RECALLS (direct scrape)")
    print("─" * 60)

    # FDA's "abuse detection" blocks bot-looking requests, especially from cloud IPs
    # (GitHub Actions, AWS, etc.). Send a full set of realistic browser headers
    # and retry with backoff if blocked.
    import time
    browser_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Referer": "https://www.fda.gov/",
    }

    resp = None
    for attempt in range(3):
        try:
            print(f"\n📡 Fetching: {FDA_RECALLS_PAGE_URL}" + (f" (attempt {attempt+1})" if attempt else ""))
            resp = requests.get(FDA_RECALLS_PAGE_URL, headers=browser_headers, timeout=60,
                                allow_redirects=True)
            # Treat the abuse-detection redirect explicitly — its status is 200 but the
            # response body is the apology page, so check the final URL.
            if "abuse-detection-apology" in (resp.url or "") or resp.status_code == 404:
                raise requests.HTTPError(
                    f"FDA blocked the request as bot (redirected to {resp.url})"
                )
            resp.raise_for_status()
            break
        except Exception as e:
            print(f"  ⚠️  Attempt {attempt+1} failed: {e}")
            resp = None
            if attempt < 2:
                wait = (attempt + 1) * 5
                print(f"  ⏳ Waiting {wait}s before retry...")
                time.sleep(wait)
    if resp is None:
        print(f"  ❌ Failed to fetch FDA recalls page after 3 attempts. Skipping FDA recalls for this run.")
        return

    soup = BeautifulSoup(resp.text, "html.parser")

    # The page renders the recalls table with rows like:
    #   <td>Date</td>  <td><a href="/medical-devices/...">Title</a></td>
    #   <td>Product Area</td>  <td>Status</td>
    rows = []
    for tbl in soup.find_all("table"):
        thead = tbl.find("thead")
        head_text = (thead.get_text(" ", strip=True).lower() if thead else "")
        if "issue" not in head_text or "date" not in head_text:
            continue
        for tr in tbl.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue
            link = tds[1].find("a")
            if not link or not link.get("href"):
                continue
            rows.append({
                "date_text":   tds[0].get_text(" ", strip=True),
                "title":       link.get_text(" ", strip=True),
                "href":        link["href"],
                "product_area": tds[2].get_text(" ", strip=True) if len(tds) > 2 else "",
                "status":       tds[3].get_text(" ", strip=True) if len(tds) > 3 else "",
            })

    if not rows:
        print("  ⚠️  Couldn't find any recall rows on the page (FDA may have changed layout).")
        return

    print(f"  ✅ {len(rows)} recall row(s) found on page")

    cutoff = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    table = api.table(AIRTABLE_BASE_ID, ALERTS_TABLE)
    total_new, total_skipped, skipped_old = 0, 0, 0

    for r in rows:
        title = r["title"]
        href  = r["href"]
        # Absolute URL
        if href.startswith("/"):
            href = f"https://www.fda.gov{href}"

        pub_date = parse_fda_recall_date(r["date_text"]) or datetime.now().strftime("%Y-%m-%d")
        if pub_date < cutoff:
            skipped_old += 1
            continue

        if alert_exists(table, href):
            total_skipped += 1
            continue

        # Build a structured summary from the row metadata
        summary_parts = []
        if r["status"]:
            summary_parts.append(f"[{r['status']}]")
        if r["product_area"]:
            summary_parts.append(f"Product area: {r['product_area']}.")
        summary = " ".join(summary_parts)

        alert = {
            "title":         title[:200],
            "url":           href,
            "snippet":       summary,
            "pub_date":      pub_date,
            "source":        "FDA Recalls",
            "category":      "Recall",
            "product_group": r["product_area"] or "",
            "manufacturer":  extract_manufacturer_from_title(title),
        }

        if save_alert(table, alert):
            total_new += 1

    print(f"\n  📥 New alerts saved:    {total_new}")
    print(f"  ⏭️  Duplicates skipped:  {total_skipped}")
    print(f"  📅 Outside window:      {skipped_old}")


# ═════════════════════════════════════════
# TASK 1d — MHRA ALERTS via GOV.UK SEARCH API
# ═════════════════════════════════════════
# Source: https://www.gov.uk/api/search.json
# Public, no auth, predictable JSON. Filter by document type "medical_safety_alert".

GOVUK_SEARCH_URL = "https://www.gov.uk/api/search.json"


def run_mhra_alerts(api):
    print("\n" + "─" * 60)
    print("  TASK 1d — MHRA ALERTS (GOV.UK API)")
    print("─" * 60)

    params = {
        "filter_content_store_document_type": "medical_safety_alert",
        "order":  "-public_timestamp",
        "count":  50,
        "fields": "title,link,description,public_timestamp",
    }

    try:
        print(f"\n📡 Fetching MHRA medical safety alerts")
        resp = requests.get(GOVUK_SEARCH_URL, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        records = data.get("results", [])
        print(f"  ✅ {len(records)} alert(s) returned")
    except Exception as e:
        print(f"  ❌ Failed to fetch GOV.UK search API: {e}")
        return

    cutoff = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    table = api.table(AIRTABLE_BASE_ID, ALERTS_TABLE)
    total_new, total_skipped = 0, 0

    for r in records:
        title       = (r.get("title") or "").strip()
        link        = (r.get("link") or "").strip()
        description = (r.get("description") or "").strip()
        timestamp   = (r.get("public_timestamp") or "").strip()
        if not title or not link:
            continue

        # GOV.UK API returns relative links (e.g. "/drug-device-alerts/...") — make absolute
        if link.startswith("/"):
            link = f"https://www.gov.uk{link}"

        # Parse and filter by date
        pub_date = datetime.now().strftime("%Y-%m-%d")
        if timestamp:
            pub_date = timestamp.split("T")[0] if "T" in timestamp else timestamp
        if pub_date < cutoff:
            continue

        if alert_exists(table, link):
            total_skipped += 1
            continue

        alert = {
            "title":         title[:200],
            "url":           link,
            "snippet":       description[:500],
            "pub_date":      pub_date,
            "source":        "UK MHRA Alert",
            "category":      "Safety",
            "product_group": infer_product_group(title + " " + description),
            "manufacturer":  extract_manufacturer_from_title(title),
        }

        if save_alert(table, alert):
            total_new += 1

    print(f"\n  📥 New alerts saved:   {total_new}")
    print(f"  ⏭️  Duplicates skipped: {total_skipped}")


# ═════════════════════════════════════════
# TASK 1e — EU MDR UPDATES (RSS feed)
# ═════════════════════════════════════════
# Pulls from the European Commission's official RSS feed for medical device sector
# latest updates. This is the canonical EU MDR / IVDR update feed — every news
# announcement on the EC's medical-devices-sector page is in this feed with
# title, link, and publication date.
# Source page: https://health.ec.europa.eu/medical-devices-sector/latest-updates_en
# RSS feed:    https://health.ec.europa.eu/node/12916/rss_en

EU_MDR_RSS_URL = "https://health.ec.europa.eu/node/12916/rss_en"


def parse_rss_pub_date(text):
    """RSS pubDate format is RFC 822 like 'Wed, 11 Jun 2026 14:00:00 +0200'.
    Return YYYY-MM-DD, or today if parsing fails."""
    if not text:
        return datetime.now().strftime("%Y-%m-%d")
    # Try a couple of common RFC 822 formats
    for fmt in ("%a, %d %b %Y %H:%M:%S %z",
                "%a, %d %b %Y %H:%M:%S %Z",
                "%a, %d %b %Y %H:%M:%S",
                "%d %b %Y %H:%M:%S %z"):
        try:
            return datetime.strptime(text.strip(), fmt).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            continue
    return datetime.now().strftime("%Y-%m-%d")


def run_eu_mdr_updates(api):
    print("\n" + "─" * 60)
    print("  TASK 1e — EU MDR UPDATES (RSS feed)")
    print("─" * 60)

    try:
        print(f"\n📡 Fetching: {EU_MDR_RSS_URL}")
        resp = requests.get(EU_MDR_RSS_URL, timeout=30,
                            headers={"User-Agent": "RegPatrol/1.0"})
        resp.raise_for_status()
    except Exception as e:
        print(f"  ❌ Failed to fetch EU MDR RSS: {e}")
        return

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        print(f"  ❌ Failed to parse RSS XML: {e}")
        return

    # RSS structure: <rss><channel><item>...</item><item>...</item></channel></rss>
    items = root.findall(".//item")
    print(f"  ✅ {len(items)} item(s) returned in feed")

    if not items:
        return

    cutoff = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    table = api.table(AIRTABLE_BASE_ID, ALERTS_TABLE)
    total_new, total_skipped, skipped_old, skipped_offtopic = 0, 0, 0, 0

    for item in items:
        title = (item.findtext("title") or "").strip()
        link  = (item.findtext("link")  or "").strip()
        desc  = (item.findtext("description") or "").strip()
        pubdate_raw = item.findtext("pubDate") or ""
        if not title or not link:
            continue

        # The EC RSS feed at node/12916 pulls items from across all DG-SANTE
        # subject areas, not only medical devices. Filter to medical-device-
        # relevant items by URL path AND title/description keywords.
        link_lower = link.lower()
        # Must come from the medical devices section of health.ec.europa.eu
        # OR be an EU regulation file mentioning medical devices.
        is_md_url = (
            "/medical-devices" in link_lower
            or "/medical_device" in link_lower
            or "eudamed" in link_lower
            or "mdcg" in link_lower
        )
        # Fallback keyword check in title/description for items that link to
        # general /system/files paths (PDFs) where the URL doesn't reveal topic
        md_keywords = ["medical device", "ivd", "in vitro diagn", "mdr",
                       "ivdr", "mdcg", "eudamed", "udi", "samd",
                       "field safety", "implant", "notified body"]
        text_lower = (title + " " + desc).lower()
        is_md_keyword = any(kw in text_lower for kw in md_keywords)

        if not (is_md_url or is_md_keyword):
            skipped_offtopic += 1
            continue

        pub_date = parse_rss_pub_date(pubdate_raw)
        if pub_date < cutoff:
            skipped_old += 1
            continue

        if alert_exists(table, link):
            total_skipped += 1
            continue

        # Strip HTML tags from description for a clean summary preview
        if desc:
            desc = BeautifulSoup(desc, "html.parser").get_text(" ", strip=True)
            desc = desc[:500] + ("..." if len(desc) > 500 else "")

        alert = {
            "title":         title[:200],
            "url":           link,
            "snippet":       desc,
            "pub_date":      pub_date,
            "source":        "EU MDR Update",
            "category":      "Guidance",
            "product_group": infer_product_group(title + " " + desc),
        }

        if save_alert(table, alert):
            total_new += 1

    print(f"\n  📥 New alerts saved:    {total_new}")
    print(f"  ⏭️  Duplicates skipped:  {total_skipped}")
    print(f"  📅 Outside window:      {skipped_old}")
    print(f"  ⏭️  Non-device items:    {skipped_offtopic}")


# ═════════════════════════════════════════
# TASK 1g — TGA ALERTS & PUBLICATIONS (RSS feeds)
# ═════════════════════════════════════════
# Australia's Therapeutic Goods Administration publishes RSS feeds for all
# alerts (recalls, hazard alerts, safety advisories) and publications.
# The "alert" feed covers all therapeutic goods (medicines + devices) so we
# filter for medical-device-relevant items by keyword.
# RSS feeds:
#   https://tga.gov.au/feeds/alert.xml              (all alerts)
#   https://tga.gov.au/feeds/publication/publications.xml  (publications)

TGA_FEEDS = [
    {
        "url":      "https://tga.gov.au/feeds/alert.xml",
        "source":   "TGA Alert",
        "category": "Recall",
    },
    {
        "url":      "https://tga.gov.au/feeds/publication/publications.xml",
        "source":   "TGA Publication",
        "category": "Guidance",
    },
]

# Keywords that mark an item as medical-device-relevant.
# TGA covers medicines, vaccines, devices, etc — these keep us in scope.
TGA_DEVICE_KEYWORDS = [
    "device", "implant", "pump", "catheter", "stent", "valve", "scanner",
    "monitor", "defibrillator", "pacemaker", "syringe", "needle", "ventilator",
    "diagnostic", "ivd", "in vitro", "udi", "samd", "software as a medical",
    "infusion", "endoscop", "ultrasound", "mri", "ct scan", "x-ray",
    "orthopaedic", "prosthe", "surgical", "wheelchair", "hearing aid",
    "breast implant", "hip replacement", "knee replacement",
    "medical device", "in-vitro",
]


def is_device_relevant(text):
    """True if the text mentions a medical-device-related keyword."""
    text_lower = (text or "").lower()
    return any(kw in text_lower for kw in TGA_DEVICE_KEYWORDS)


def run_tga_feeds(api):
    print("\n" + "─" * 60)
    print("  TASK 1g — TGA ALERTS & PUBLICATIONS (RSS feeds)")
    print("─" * 60)

    cutoff = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    table = api.table(AIRTABLE_BASE_ID, ALERTS_TABLE)

    # Real browser User-Agent — TGA blocks custom/bot-looking UAs
    browser_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/rss+xml, application/xml, text/xml, */*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    for feed_cfg in TGA_FEEDS:
        print(f"\n📡 Fetching: {feed_cfg['url']}")

        # TGA's server can be slow from outside Australia. Retry once with a
        # longer timeout before giving up.
        resp = None
        for attempt in range(2):
            try:
                timeout_secs = 90 if attempt == 0 else 120
                resp = requests.get(feed_cfg["url"], timeout=timeout_secs,
                                    headers=browser_headers)
                resp.raise_for_status()
                break
            except Exception as e:
                if attempt == 0:
                    print(f"  ⚠️  Attempt 1 failed ({e}). Retrying with longer timeout...")
                else:
                    print(f"  ❌ Failed to fetch {feed_cfg['source']} after retry: {e}")
                    resp = None
        if resp is None:
            continue

        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as e:
            print(f"  ❌ Failed to parse RSS XML: {e}")
            continue

        items = root.findall(".//item")
        print(f"  ✅ {len(items)} item(s) returned")

        total_new, total_skipped, skipped_old, skipped_offtopic = 0, 0, 0, 0

        for item in items:
            title = (item.findtext("title") or "").strip()
            link  = (item.findtext("link")  or "").strip()
            desc  = (item.findtext("description") or "").strip()
            pubdate_raw = item.findtext("pubDate") or ""
            if not title or not link:
                continue

            # Filter to medical-device-relevant items only (TGA covers all therapeutic goods)
            if not (is_device_relevant(title) or is_device_relevant(desc)):
                skipped_offtopic += 1
                continue

            pub_date = parse_rss_pub_date(pubdate_raw)
            if pub_date < cutoff:
                skipped_old += 1
                continue

            if alert_exists(table, link):
                total_skipped += 1
                continue

            if desc:
                desc = BeautifulSoup(desc, "html.parser").get_text(" ", strip=True)
                desc = desc[:500] + ("..." if len(desc) > 500 else "")

            alert = {
                "title":    title[:200],
                "url":      link,
                "snippet":  desc,
                "pub_date": pub_date,
                "source":   feed_cfg["source"],
                "category": feed_cfg["category"],
            }

            if save_alert(table, alert):
                total_new += 1

        print(f"  📥 New alerts saved:    {total_new}")
        print(f"  ⏭️  Duplicates skipped:  {total_skipped}")
        print(f"  📅 Outside window:      {skipped_old}")
        print(f"  ⏭️  Non-device items:    {skipped_offtopic}")


# ═════════════════════════════════════════
# TASK 1f — BfArM FIELD CORRECTIVE ACTIONS (RSS feed)
# ═════════════════════════════════════════
# Germany's Federal Institute for Drugs and Medical Devices publishes every
# Field Safety Notice for medical devices sold in Germany via this official
# RSS feed. Each item is a real FSN with manufacturer, device name, and link
# to BfArM's English customer information page.
# RSS feed: https://www.bfarm.de/SiteGlobals/Functions/RSSFeed/EN/Kundeninfo/RSSNewsfeed.xml?nn=708434

BFARM_RSS_URL = ("https://www.bfarm.de/SiteGlobals/Functions/RSSFeed/EN/"
                 "Kundeninfo/RSSNewsfeed.xml?nn=708434")


def run_bfarm_fcas(api):
    print("\n" + "─" * 60)
    print("  TASK 1f — BfArM FIELD CORRECTIVE ACTIONS (RSS feed)")
    print("─" * 60)

    try:
        print(f"\n📡 Fetching: {BFARM_RSS_URL}")
        resp = requests.get(BFARM_RSS_URL, timeout=30,
                            headers={"User-Agent": "RegPatrol/1.0"})
        resp.raise_for_status()
    except Exception as e:
        print(f"  ❌ Failed to fetch BfArM RSS: {e}")
        return

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        print(f"  ❌ Failed to parse RSS XML: {e}")
        return

    items = root.findall(".//item")
    print(f"  ✅ {len(items)} item(s) returned in feed")

    if not items:
        return

    cutoff = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    table = api.table(AIRTABLE_BASE_ID, ALERTS_TABLE)
    total_new, total_skipped, skipped_old = 0, 0, 0

    for item in items:
        title = (item.findtext("title") or "").strip()
        link  = (item.findtext("link")  or "").strip()
        desc  = (item.findtext("description") or "").strip()
        pubdate_raw = item.findtext("pubDate") or ""
        if not title or not link:
            continue

        pub_date = parse_rss_pub_date(pubdate_raw)
        if pub_date < cutoff:
            skipped_old += 1
            continue

        if alert_exists(table, link):
            total_skipped += 1
            continue

        # Clean up description (usually a short manufacturer summary)
        if desc:
            desc = BeautifulSoup(desc, "html.parser").get_text(" ", strip=True)
            desc = desc[:500] + ("..." if len(desc) > 500 else "")

        alert = {
            "title":         title[:200],
            "url":           link,
            "snippet":       desc,
            "pub_date":      pub_date,
            "source":        "BfArM FSN",
            "category":      "Recall",
            "product_group": infer_product_group(title + " " + desc),
            "manufacturer":  extract_manufacturer_from_title(title),
        }

        if save_alert(table, alert):
            total_new += 1

    print(f"\n  📥 New alerts saved:    {total_new}")
    print(f"  ⏭️  Duplicates skipped:  {total_skipped}")
    print(f"  📅 Outside window:      {skipped_old}")


# ═════════════════════════════════════════
# TASK 2 — NETLIFY SUBSCRIBER SYNC
# ═════════════════════════════════════════

def get_netlify_submissions():
    headers = {"Authorization": f"Bearer {NETLIFY_TOKEN}"}

    print("\n📡 Connecting to Netlify...")
    sites_resp = requests.get("https://api.netlify.com/api/v1/sites", headers=headers, timeout=30)
    sites_resp.raise_for_status()
    sites = sites_resp.json()

    site_id = None
    for site in sites:
        if NETLIFY_SITE in (site.get("custom_domain") or "") or \
           NETLIFY_SITE in (site.get("name") or "") or \
           NETLIFY_SITE in (site.get("url") or ""):
            site_id = site["id"]
            break

    if not site_id:
        print(f"  ❌ Could not find a site matching '{NETLIFY_SITE}'.")
        return []

    print(f"  ✅ Found site (id: {site_id})")

    subs_resp = requests.get(
        f"https://api.netlify.com/api/v1/sites/{site_id}/submissions",
        headers=headers, params={"per_page": 200}, timeout=30,
    )
    subs_resp.raise_for_status()
    submissions = subs_resp.json()

    results = []
    for sub in submissions:
        data = sub.get("data", {})
        email = (data.get("email") or "").strip().lower()
        if not email or "@" not in email:
            continue
        results.append({
            "email":  email,
            "source": data.get("source", "unknown"),
        })

    print(f"  ✅ Found {len(results)} submissions with valid emails")
    return results


def get_existing_emails(table):
    existing = set()
    for record in table.all(fields=["Email"]):
        email = record["fields"].get("Email", "").strip().lower()
        if email:
            existing.add(email)
    return existing


def run_subscriber_sync(api):
    print("\n" + "─" * 60)
    print("  TASK 2 — SUBSCRIBER SYNC")
    print("─" * 60)

    if not NETLIFY_TOKEN:
        print("  ⚠️  Skipping — NETLIFY_TOKEN not set in .env.")
        return

    submissions = get_netlify_submissions()
    if not submissions:
        return

    table = api.table(AIRTABLE_BASE_ID, SUBSCRIBERS_TABLE)
    existing = get_existing_emails(table)
    print(f"  ✅ {len(existing)} subscribers already in Airtable")

    added = 0
    for sub in submissions:
        if sub["email"] in existing:
            continue
        try:
            table.create({
                "Email":       sub["email"],
                "Source":      sub["source"],
                "Plan":        "Free",
                "Active":      True,
                "Joined date": datetime.now().strftime("%Y-%m-%d"),
            })
            print(f"  💾 Added: {sub['email']}")
            added += 1
            existing.add(sub["email"])
        except Exception as e:
            print(f"  ❌ Failed to add {sub['email']}: {e}")

    print(f"\n  📥 New subscribers added: {added}")


# ═════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════

def main():
    print("=" * 60)
    print("  RegPatrol Daily Job (Exa)")
    print(f"  Running at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Lookback window: last {LOOKBACK_DAYS} days")
    print("=" * 60)

    if not AIRTABLE_TOKEN:
        print("\n❌ ERROR: AIRTABLE_TOKEN not found.")
        print("   Create a .env file next to this script containing:")
        print("     AIRTABLE_TOKEN=pat...")
        print("     EXA_API_KEY=...")
        print("     NETLIFY_TOKEN=nfp_...")
        sys.exit(1)

    api = Api(AIRTABLE_TOKEN)

    try:
        run_regulatory_monitor(api)
    except Exception as e:
        print(f"\n  ❌ Regulatory monitor failed: {e}")

    try:
        run_health_canada_recalls(api)
    except Exception as e:
        print(f"\n  ❌ Health Canada recalls failed: {e}")

    try:
        run_fda_recalls(api)
    except Exception as e:
        print(f"\n  ❌ FDA recalls failed: {e}")

    try:
        run_mhra_alerts(api)
    except Exception as e:
        print(f"\n  ❌ MHRA alerts failed: {e}")

    try:
        run_eu_mdr_updates(api)
    except Exception as e:
        print(f"\n  ❌ EU MDR updates failed: {e}")

    # TGA disabled — TGA's RSS endpoints consistently time out from outside Australia.
    # Re-enable once we have a reliable scraping path or a proxy/CDN solution.
    # try:
    #     run_tga_feeds(api)
    # except Exception as e:
    #     print(f"\n  ❌ TGA feeds failed: {e}")

    try:
        run_bfarm_fcas(api)
    except Exception as e:
        print(f"\n  ❌ BfArM FCAs failed: {e}")

    try:
        run_subscriber_sync(api)
    except Exception as e:
        print(f"\n  ❌ Subscriber sync failed: {e}")

    # Rebuild the public /alerts archive page from Airtable
    try:
        import build_alerts_page
        print("\n" + "─" * 60)
        print("  TASK 3 — REBUILD PUBLIC /alerts PAGE")
        print("─" * 60)
        build_alerts_page.main()
    except ImportError:
        print("\n  ⚠️  build_alerts_page.py not found alongside regpatrol.py — skipping archive rebuild.")
    except Exception as e:
        print(f"\n  ❌ Archive rebuild failed: {e}")

    print("\n" + "=" * 60)
    print("  ✅ All tasks complete!")
    print("=" * 60)


if __name__ == "__main__":
    main()
