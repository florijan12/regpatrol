"""
RegPatrol - Static Alerts Page Builder
=======================================
Reads every alert from Airtable and generates a single static HTML page
(alerts.html) with client-side filtering, search, and pagination.

Run after regpatrol.py to rebuild the public archive at regpatrol.com/alerts.

Setup:
    pip install pyairtable python-dotenv

Usage:
    python build_alerts_page.py
"""

import os
import sys
import json
import html
from datetime import datetime
from pyairtable import Api
from dotenv import load_dotenv

load_dotenv()

AIRTABLE_TOKEN   = os.getenv("AIRTABLE_TOKEN", "")
AIRTABLE_BASE_ID = "appHE7piNN3WdvGcu"
ALERTS_TABLE     = "Regulert"

OUTPUT_FILE = "alerts.html"


def fetch_all_alerts():
    """Pull every row from Airtable Regulert table."""
    if not AIRTABLE_TOKEN:
        print("❌ AIRTABLE_TOKEN not set in .env")
        sys.exit(1)

    print(f"📡 Connecting to Airtable...")
    api = Api(AIRTABLE_TOKEN)
    table = api.table(AIRTABLE_BASE_ID, ALERTS_TABLE)
    records = table.all()
    print(f"  ✅ {len(records)} alerts found")

    alerts = []
    for r in records:
        f = r.get("fields", {})
        title    = (f.get("Title") or "").strip()
        url      = (f.get("FDA link") or "").strip()
        source   = (f.get("Source") or "").strip()
        category = (f.get("Device category") or [])
        if isinstance(category, list):
            category = category[0] if category else ""
        pub_date = f.get("Published date") or ""
        summary  = (f.get("Summary") or "").strip()
        if not title or not url:
            continue
        alerts.append({
            "title":    title,
            "url":      url,
            "source":   source,
            "category": category,
            "date":     pub_date,
            "summary":  summary,
        })

    # Sort newest first
    alerts.sort(key=lambda a: a["date"], reverse=True)
    return alerts


def build_html(alerts):
    """Embed all alerts as JSON inside a static HTML page with client-side UI."""
    # Pre-compute filter options
    sources    = sorted({a["source"]   for a in alerts if a["source"]})
    categories = sorted({a["category"] for a in alerts if a["category"]})

    # Region mapping — each region maps to one or more source prefixes/labels
    region_map = {
        "us":      {"label": "🇺🇸 United States", "sources": ["FDA"]},
        "eu":      {"label": "🇪🇺 EU",            "sources": ["EU MDR"]},
        "de":      {"label": "🇩🇪 Germany",       "sources": ["BfArM"]},
        "uk":      {"label": "🇬🇧 United Kingdom","sources": ["UK MHRA"]},
        "ca":      {"label": "🇨🇦 Canada",        "sources": ["Health Canada"]},
    }

    def alert_region(src):
        for code, meta in region_map.items():
            if any(src.startswith(prefix) for prefix in meta["sources"]):
                return code
        return ""

    # Add region code to every alert (used by JS for tab filtering)
    for a in alerts:
        a["region"] = alert_region(a["source"])

    # Count alerts per region for the tab badges
    region_counts = {code: 0 for code in region_map}
    for a in alerts:
        if a["region"] in region_counts:
            region_counts[a["region"]] += 1

    # JSON blob embedded in the page so JS can filter/paginate without API calls
    alerts_json = json.dumps(alerts, ensure_ascii=False)

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    total = len(alerts)

    # Build region tabs HTML
    tabs_html = (
        f'    <button class="region-tab active" data-region="">'
        f'All <span class="tab-count">{total}</span></button>\n'
    )
    for code, meta in region_map.items():
        count = region_counts[code]
        tabs_html += (
            f'    <button class="region-tab" data-region="{code}">'
            f'{meta["label"]} <span class="tab-count">{count}</span></button>\n'
        )

    source_opts = "\n".join(
        f'        <option value="{html.escape(s)}">{html.escape(s)}</option>'
        for s in sources
    )
    category_opts = "\n".join(
        f'        <option value="{html.escape(c)}">{html.escape(c)}</option>'
        for c in categories
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Regulatory Alerts Archive — RegPatrol</title>
<meta name="description" content="Browse {total}+ medical device regulatory alerts from FDA, EU MDR, BfArM, Health Canada, MHRA, and TGA. Updated daily.">
<style>
  :root {{
    --navy:        #0f1e3d;
    --navy-deep:   #0a1530;
    --silver:      #a8c0dd;
    --silver-pale: #e3ecf8;
    --accent:      #d4af37;
    --text:        #0f1e3d;
    --muted:       #5a6c87;
    --line:        #e0e7f2;
    --bg:          #fafbfd;
    --white:       #ffffff;
    --class-i:     #c0392b;
    --class-ii:    #d4881f;
    --class-iii:   #2c7be5;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }}

  /* Header */
  header {{
    background: linear-gradient(135deg, var(--navy) 0%, var(--navy-deep) 100%);
    color: var(--white);
    padding: 2rem 1.5rem 2.5rem;
  }}
  .header-inner {{
    max-width: 1100px;
    margin: 0 auto;
  }}
  .logo {{
    font-size: 0.95rem;
    font-weight: 600;
    letter-spacing: 0.18em;
    color: var(--silver);
    margin-bottom: 1rem;
    text-transform: uppercase;
  }}
  .logo::before {{ content: "🛡️"; margin-right: 0.5rem; }}
  .logo a {{ color: var(--silver); text-decoration: none; }}
  h1 {{
    font-size: clamp(1.6rem, 3.5vw, 2.2rem);
    font-weight: 700;
    margin-bottom: 0.4rem;
    letter-spacing: -0.015em;
  }}
  .subtitle {{
    color: var(--silver-pale);
    font-size: 1rem;
    margin-bottom: 1rem;
  }}
  .meta-line {{
    color: var(--silver);
    font-size: 0.85rem;
  }}
  .meta-line strong {{ color: var(--white); }}
  .cta-bar {{
    margin-top: 1.5rem;
    background: rgba(255,255,255,0.08);
    border: 1px solid rgba(168,192,221,0.2);
    border-radius: 10px;
    padding: 1rem 1.25rem;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 1rem;
    flex-wrap: wrap;
  }}
  .cta-bar p {{
    color: var(--silver-pale);
    font-size: 0.95rem;
    margin: 0;
  }}
  .cta-bar a {{
    background: var(--silver-pale);
    color: var(--navy);
    padding: 0.55rem 1rem;
    border-radius: 8px;
    text-decoration: none;
    font-weight: 600;
    font-size: 0.9rem;
    white-space: nowrap;
  }}
  .cta-bar a:hover {{ background: var(--white); }}

  /* Region tabs */
  .region-tabs {{
    background: var(--white);
    border-bottom: 1px solid var(--line);
    padding: 0.75rem 1.5rem 0;
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
  }}
  .region-tabs-inner {{
    max-width: 1100px;
    margin: 0 auto;
    display: flex;
    gap: 0.4rem;
    flex-wrap: nowrap;
  }}
  .region-tab {{
    background: transparent;
    border: 1px solid transparent;
    border-bottom: none;
    color: var(--muted);
    padding: 0.6rem 1rem;
    border-radius: 8px 8px 0 0;
    font-size: 0.92rem;
    font-weight: 500;
    cursor: pointer;
    font-family: inherit;
    white-space: nowrap;
    display: flex;
    align-items: center;
    gap: 0.45rem;
    transition: color 0.15s, background 0.15s;
  }}
  .region-tab:hover {{
    color: var(--text);
    background: var(--bg);
  }}
  .region-tab.active {{
    color: var(--navy);
    background: var(--white);
    border-color: var(--line);
    border-bottom-color: var(--white);
    margin-bottom: -1px;
    font-weight: 600;
  }}
  .tab-count {{
    background: var(--silver-pale);
    color: var(--navy);
    font-size: 0.75rem;
    padding: 0.1rem 0.5rem;
    border-radius: 100px;
    font-weight: 600;
    min-width: 1.5rem;
    text-align: center;
  }}
  .region-tab.active .tab-count {{
    background: var(--navy);
    color: var(--silver-pale);
  }}

  /* Filters */
  .controls {{
    background: var(--white);
    border-bottom: 1px solid var(--line);
    padding: 1.25rem 1.5rem;
    position: sticky;
    top: 0;
    z-index: 10;
    box-shadow: 0 1px 3px rgba(15,30,61,0.04);
  }}
  .controls-inner {{
    max-width: 1100px;
    margin: 0 auto;
    display: grid;
    grid-template-columns: 2fr 1fr 1fr;
    gap: 0.75rem;
    align-items: center;
  }}
  .controls input, .controls select {{
    padding: 0.6rem 0.85rem;
    border: 1px solid var(--line);
    border-radius: 8px;
    font-size: 0.95rem;
    background: var(--bg);
    color: var(--text);
    font-family: inherit;
  }}
  .controls input:focus, .controls select:focus {{
    outline: none;
    border-color: var(--silver);
    box-shadow: 0 0 0 3px rgba(168,192,221,0.25);
  }}

  /* Results */
  main {{
    max-width: 1100px;
    margin: 0 auto;
    padding: 2rem 1.5rem 4rem;
  }}
  .result-count {{
    color: var(--muted);
    font-size: 0.9rem;
    margin-bottom: 1.25rem;
  }}
  .alert-row {{
    background: var(--white);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 1.25rem 1.5rem;
    margin-bottom: 0.85rem;
    transition: border-color 0.15s, transform 0.1s;
    text-decoration: none;
    color: var(--text);
    display: block;
  }}
  .alert-row:hover {{
    border-color: var(--silver);
    transform: translateY(-1px);
    box-shadow: 0 4px 12px rgba(15,30,61,0.06);
  }}
  .alert-meta {{
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
    margin-bottom: 0.55rem;
    align-items: center;
  }}
  .badge {{
    display: inline-block;
    padding: 0.18rem 0.55rem;
    border-radius: 4px;
    font-size: 0.7rem;
    font-weight: 600;
    letter-spacing: 0.03em;
    text-transform: uppercase;
  }}
  .badge-source {{ background: var(--silver-pale); color: var(--navy); }}
  .badge-cat-recall   {{ background: #fdecea; color: var(--class-i);   }}
  .badge-cat-safety   {{ background: #fdf3e3; color: var(--class-ii);  }}
  .badge-cat-guidance {{ background: #e6f0fb; color: var(--class-iii); }}
  .badge-cat-other    {{ background: #ecf3ee; color: #1d7a4f; }}
  .alert-date {{
    color: var(--muted);
    font-size: 0.8rem;
    margin-left: auto;
  }}
  .alert-title {{
    font-size: 1rem;
    font-weight: 600;
    line-height: 1.4;
    margin-bottom: 0.35rem;
  }}
  .alert-summary {{
    color: var(--muted);
    font-size: 0.9rem;
    line-height: 1.55;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }}

  /* Pagination */
  .pagination {{
    display: flex;
    justify-content: center;
    gap: 0.4rem;
    margin-top: 2.5rem;
    flex-wrap: wrap;
  }}
  .pagination button {{
    background: var(--white);
    border: 1px solid var(--line);
    color: var(--text);
    padding: 0.5rem 0.85rem;
    border-radius: 6px;
    font-size: 0.9rem;
    cursor: pointer;
    font-family: inherit;
    min-width: 40px;
  }}
  .pagination button:hover:not(:disabled) {{
    border-color: var(--silver);
    background: var(--bg);
  }}
  .pagination button.active {{
    background: var(--navy);
    color: var(--white);
    border-color: var(--navy);
  }}
  .pagination button:disabled {{
    opacity: 0.4;
    cursor: not-allowed;
  }}

  /* Empty state */
  .empty {{
    text-align: center;
    padding: 3rem 1.5rem;
    color: var(--muted);
    background: var(--white);
    border: 1px dashed var(--line);
    border-radius: 12px;
  }}

  /* Footer */
  footer {{
    background: var(--navy-deep);
    color: var(--silver);
    padding: 2rem 1.5rem;
    text-align: center;
    font-size: 0.85rem;
    margin-top: 3rem;
  }}
  footer a {{ color: var(--silver-pale); text-decoration: none; }}

  /* Mobile */
  @media (max-width: 720px) {{
    .controls-inner {{ grid-template-columns: 1fr; }}
    .alert-row {{ padding: 1rem 1.1rem; }}
    .alert-date {{
      margin-left: 0;
      width: 100%;
    }}
    .cta-bar {{ flex-direction: column; align-items: stretch; text-align: center; }}
  }}
</style>
</head>
<body>

<header>
  <div class="header-inner">
    <div class="logo"><a href="/">RegPatrol</a></div>
    <h1>Regulatory Alerts Archive</h1>
    <p class="subtitle">Every medical device regulatory update we've tracked — searchable.</p>
    <p class="meta-line">
      <strong>{total:,}</strong> alerts across {len(sources)} regulators · Updated {generated_at}
    </p>

    <div class="cta-bar">
      <p>Don't want to browse? Get the curated 5-minute weekly digest.</p>
      <a href="/#signup">Start free trial →</a>
    </div>
  </div>
</header>

<div class="region-tabs">
  <div class="region-tabs-inner">
{tabs_html}  </div>
</div>

<div class="controls">
  <div class="controls-inner">
    <input type="search" id="search" placeholder="Search by title, manufacturer, device…" autocomplete="off">
    <select id="filter-source">
      <option value="">All sources</option>
{source_opts}
    </select>
    <select id="filter-category">
      <option value="">All categories</option>
{category_opts}
    </select>
  </div>
</div>

<main>
  <div class="result-count" id="result-count"></div>
  <div id="results"></div>
  <div class="pagination" id="pagination"></div>
</main>

<footer>
  <p>© 2026 RegPatrol · Curated regulatory intelligence for medical device teams</p>
  <p style="margin-top:0.5rem;"><a href="/">← back to regpatrol.com</a></p>
</footer>

<script>
  const ALERTS = {alerts_json};
  const PER_PAGE = 25;
  let currentPage = 1;
  let currentRegion = '';   // '' = all regions
  let filtered = ALERTS;

  const $search   = document.getElementById('search');
  const $source   = document.getElementById('filter-source');
  const $category = document.getElementById('filter-category');
  const $results  = document.getElementById('results');
  const $count    = document.getElementById('result-count');
  const $pages    = document.getElementById('pagination');
  const $tabs     = document.querySelectorAll('.region-tab');

  function escapeHtml(s) {{
    return String(s).replace(/[&<>"']/g, c => ({{
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }})[c]);
  }}

  function categoryClass(c) {{
    const lc = (c || '').toLowerCase();
    if (lc.includes('recall'))   return 'badge-cat-recall';
    if (lc.includes('safety'))   return 'badge-cat-safety';
    if (lc.includes('guidance')) return 'badge-cat-guidance';
    return 'badge-cat-other';
  }}

  function applyFilters() {{
    const q = $search.value.trim().toLowerCase();
    const src = $source.value;
    const cat = $category.value;

    filtered = ALERTS.filter(a => {{
      if (currentRegion && a.region !== currentRegion) return false;
      if (src && a.source !== src) return false;
      if (cat && a.category !== cat) return false;
      if (q) {{
        const hay = (a.title + ' ' + a.summary + ' ' + a.source).toLowerCase();
        if (!hay.includes(q)) return false;
      }}
      return true;
    }});
    currentPage = 1;
    render();
  }}

  function render() {{
    const total = filtered.length;
    const pages = Math.max(1, Math.ceil(total / PER_PAGE));
    if (currentPage > pages) currentPage = pages;

    $count.textContent = total === 0
      ? 'No alerts match your filters.'
      : `Showing ${{(currentPage-1)*PER_PAGE + 1}}–${{Math.min(currentPage*PER_PAGE, total)}} of ${{total.toLocaleString()}} alerts`;

    if (total === 0) {{
      $results.innerHTML = '<div class="empty">Try clearing a filter or broadening your search.</div>';
      $pages.innerHTML = '';
      return;
    }}

    const start = (currentPage - 1) * PER_PAGE;
    const slice = filtered.slice(start, start + PER_PAGE);

    $results.innerHTML = slice.map(a => `
      <a href="${{escapeHtml(a.url)}}" target="_blank" rel="noopener" class="alert-row">
        <div class="alert-meta">
          <span class="badge badge-source">${{escapeHtml(a.source)}}</span>
          ${{a.category ? `<span class="badge ${{categoryClass(a.category)}}">${{escapeHtml(a.category)}}</span>` : ''}}
          <span class="alert-date">${{escapeHtml(a.date || '')}}</span>
        </div>
        <div class="alert-title">${{escapeHtml(a.title)}}</div>
        ${{a.summary ? `<div class="alert-summary">${{escapeHtml(a.summary)}}</div>` : ''}}
      </a>
    `).join('');

    renderPagination(pages);
  }}

  function renderPagination(pages) {{
    if (pages <= 1) {{ $pages.innerHTML = ''; return; }}

    const buttons = [];
    buttons.push(`<button ${{currentPage === 1 ? 'disabled' : ''}} data-p="${{currentPage-1}}">‹ Prev</button>`);

    // Smart pagination: first, last, current ± 2, ellipses
    const show = new Set([1, pages, currentPage]);
    for (let i = -2; i <= 2; i++) {{
      const p = currentPage + i;
      if (p > 1 && p < pages) show.add(p);
    }}
    const sorted = [...show].sort((a, b) => a - b);
    let prev = 0;
    for (const p of sorted) {{
      if (p - prev > 1) buttons.push('<button disabled>…</button>');
      buttons.push(`<button ${{p === currentPage ? 'class="active"' : ''}} data-p="${{p}}">${{p}}</button>`);
      prev = p;
    }}

    buttons.push(`<button ${{currentPage === pages ? 'disabled' : ''}} data-p="${{currentPage+1}}">Next ›</button>`);
    $pages.innerHTML = buttons.join('');

    $pages.querySelectorAll('button[data-p]').forEach(b => {{
      b.addEventListener('click', () => {{
        currentPage = parseInt(b.dataset.p, 10);
        render();
        window.scrollTo({{ top: 0, behavior: 'smooth' }});
      }});
    }});
  }}

  // Debounce search input
  let searchTimer;
  $search.addEventListener('input', () => {{
    clearTimeout(searchTimer);
    searchTimer = setTimeout(applyFilters, 200);
  }});
  $source.addEventListener('change', applyFilters);
  $category.addEventListener('change', applyFilters);

  // Region tab clicks
  $tabs.forEach(tab => {{
    tab.addEventListener('click', () => {{
      $tabs.forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      currentRegion = tab.dataset.region;
      applyFilters();
    }});
  }});

  // Initial render
  render();
</script>

</body>
</html>
"""


def main():
    print("=" * 60)
    print("  RegPatrol — Alerts Page Builder")
    print("=" * 60)

    alerts = fetch_all_alerts()
    if not alerts:
        print("❌ No alerts found in Airtable. Nothing to build.")
        sys.exit(1)

    print(f"\n🛠️  Building HTML...")
    page = build_html(alerts)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(page)

    size_kb = len(page.encode("utf-8")) / 1024
    print(f"  ✅ Wrote {OUTPUT_FILE} ({size_kb:.1f} KB, {len(alerts):,} alerts)")
    print()
    print("Next steps:")
    print(f"  1. Open {OUTPUT_FILE} locally to preview")
    print(f"  2. Deploy to Netlify: place at /alerts/index.html")
    print(f"  3. Then visit regpatrol.com/alerts")
    print()


if __name__ == "__main__":
    main()
