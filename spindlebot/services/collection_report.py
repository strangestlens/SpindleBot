"""Render a collection audit as a self-contained HTML page.

Pure: takes an AuditReport, returns a string. The caller writes the file.

The page is one file with inline CSS and JS and no external assets beyond the
cover thumbnails, so it opens from disk, survives being emailed to yourself, and
needs no server. Styling deliberately matches lrc-editor's palette — the two
pages are the same tool wearing the same clothes.
"""
from __future__ import annotations

import html
import time
from urllib.parse import urlsplit

from spindlebot.core.collection_match import MatchStatus
from spindlebot.services.collection_audit import AuditReport

# lrc-editor's palette, kept in sync by hand (see its HTML head).
CSS = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: #1a1a2e; color: #e0e0e0; min-height: 100vh; padding-bottom: 48px;
}
#toolbar {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  padding: 10px 16px; background: #16213e; border-bottom: 1px solid #0f3460;
  position: sticky; top: 0; z-index: 5;
}
#toolbar h1 { font-size: 15px; font-weight: 600; color: #e94560; }
#toolbar .meta { font-size: 12px; color: #8899aa; margin-right: auto; }
/* Tabs stay one group when the toolbar wraps — a split tab row reads as two
   unrelated controls. */
#controls { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
#tabs { display: flex; gap: 8px; }
#search {
  background: #0d1b2a; border: 1px solid #0f3460; border-radius: 6px;
  color: #e0e0e0; font-size: 12px; padding: 7px 10px; min-width: 220px;
  font-family: ui-monospace, monospace;
}
#search::placeholder { color: #5c6b7a; }
button {
  padding: 7px 16px; border: none; border-radius: 6px; font-size: 13px;
  font-weight: 500; cursor: pointer; transition: opacity .15s;
  background: #2a2a4a; color: #aaa;
}
button:hover { opacity: .85; }
button.active { background: #0f3460; color: #e0e0e0; }
button.active[data-status="missing"] { background: #e94560; color: #fff; }
button.active[data-status="uncertain"] { background: #4a3a1a; color: #ff9800; }
button.active[data-status="owned"] { background: #1b4332; color: #6fcf97; }
/* Muted, but it still has to read as SELECTED — #2a2a4a here would be
   indistinguishable from an inactive tab. */
button.active[data-status="ignored"] { background: #35435c; color: #cfd8e3; }
#summary { display: flex; gap: 10px; flex-wrap: wrap; padding: 16px 16px 4px; }
.stat {
  background: #16213e; border: 1px solid #0f3460; border-radius: 8px;
  padding: 10px 16px; min-width: 116px;
}
.stat .n { font-size: 22px; font-weight: 600; }
.stat .l { font-size: 11px; color: #8899aa; text-transform: uppercase;
           letter-spacing: .05em; }
.stat.missing .n { color: #e94560; }
.stat.uncertain .n { color: #ff9800; }
.stat.owned .n { color: #6fcf97; }
.stat.ignored .n { color: #8899aa; }
#grid {
  display: grid; gap: 10px; padding: 12px 16px;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
}
.card {
  display: flex; gap: 10px; align-items: flex-start;
  background: #16213e; border: 1px solid #0f3460; border-radius: 8px;
  padding: 10px; border-left-width: 3px;
}
.card[data-status="missing"] { border-left-color: #e94560; }
.card[data-status="uncertain"] { border-left-color: #ff9800; }
.card[data-status="owned"] { border-left-color: #6fcf97; }
.card[data-status="ignored"] { border-left-color: #5c6b7a; opacity: .62; }
.card .thumb {
  width: 56px; height: 56px; flex-shrink: 0; border-radius: 4px;
  object-fit: cover; background: #0d1b2a;
}
.card .thumb.placeholder {
  display: flex; align-items: center; justify-content: center;
  color: #3d4c5c; font-size: 20px;
}
.card .body { min-width: 0; }
.card .artist { font-size: 12px; color: #8899aa; }
.card .title { font-size: 13.5px; font-weight: 500; overflow-wrap: anywhere; }
.card .sub { font-size: 11px; color: #5c6b7a; margin-top: 3px; }
.card .near { font-size: 11px; color: #ff9800; margin-top: 3px;
              overflow-wrap: anywhere; }
.card a { color: #7ab8e8; text-decoration: none; font-size: 11px; }
.card a:hover { text-decoration: underline; }
#empty { padding: 32px 16px; color: #8899aa; font-size: 13px; display: none; }
"""

JS = """
const cards = Array.from(document.querySelectorAll('.card'));
const tabs = Array.from(document.querySelectorAll('button[data-status]'));
const search = document.getElementById('search');
const empty = document.getElementById('empty');
let status = 'missing';

function apply() {
  const q = search.value.trim().toLowerCase();
  let shown = 0;
  for (const card of cards) {
    const okStatus = status === 'all' || card.dataset.status === status;
    const okText = !q || card.dataset.search.includes(q);
    const show = okStatus && okText;
    card.style.display = show ? '' : 'none';
    if (show) shown++;
  }
  empty.style.display = shown ? 'none' : 'block';
}

for (const tab of tabs) {
  tab.addEventListener('click', () => {
    status = tab.dataset.status;
    tabs.forEach(t => t.classList.toggle('active', t === tab));
    apply();
  });
}
search.addEventListener('input', apply);
apply();
"""

SAFE_SCHEMES = {"http", "https"}


def _safe_url(url: str | None) -> str | None:
    """Allow only http(s). Payload-supplied URLs land in href/src attributes."""
    if not url:
        return None
    return url if urlsplit(url).scheme in SAFE_SCHEMES else None


def _status_slug(match) -> str:
    """What bucket a card renders under.

    Ignored wins over the match status for display only — the underlying
    verdict is still on the row, so un-ignoring needs no recomputation.
    """
    return "ignored" if match.ignored else match.status.value


def _card(match) -> str:
    item = match.item
    e = html.escape
    thumb = _safe_url(item.thumb_url)
    img = (
        f'<img class="thumb" src="{e(thumb, quote=True)}" alt="" loading="lazy">'
        if thumb else '<div class="thumb placeholder">♪</div>'
    )

    bits = []
    if item.year:
        bits.append(str(item.year))
    bits.extend(sorted(m.value for m in item.media))
    if item.catno:
        bits.append(item.catno)
    sub = e(" · ".join(bits))

    near = ""
    if match.status is MatchStatus.UNCERTAIN and match.matched is not None:
        near = (
            f'<div class="near">≈ {e(match.matched.albumartist)} — '
            f'{e(match.matched.album)} ({match.score:.2f})</div>'
        )

    url = _safe_url(item.url)
    link = f'<div><a href="{e(url, quote=True)}" target="_blank" rel="noopener noreferrer">{e(item.source)} ↗</a></div>' if url else ""

    haystack = e(f"{item.artist} {item.title} {item.year or ''}".lower(), quote=True)
    return (
        f'<div class="card" data-status="{_status_slug(match)}" '
        f'data-id="{e(item.source_id, quote=True)}" '
        f'data-search="{haystack}">{img}'
        f'<div class="body">'
        f'<div class="artist">{e(item.artist)}</div>'
        f'<div class="title">{e(item.title)}</div>'
        f'<div class="sub">{sub}</div>{near}{link}'
        f'</div></div>'
    )


def render_html(report: AuditReport, *, generated_utc: float | None = None) -> str:
    """Render an audit as a standalone HTML page."""
    e = html.escape
    # `is None`, not truthiness: 0.0 is a valid timestamp, and the signature
    # says None is the sentinel. Falsy-testing it makes an explicitly pinned
    # time silently non-deterministic.
    when = time.time() if generated_utc is None else generated_utc
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(when))
    media = "/".join(sorted(m.value for m in report.media))
    # Which index answered is part of the report, not a footnote: a wrongly
    # "missing" album is almost always an index that didn't know about it.
    index_label = ", ".join(
        f"{name} {n}" for name, n in sorted(report.library_sources.items())
    ) or "library"
    counts = {
        "missing": len(report.missing),
        "uncertain": len(report.uncertain),
        "owned": len(report.owned),
        "ignored": len(report.ignored),
    }

    stats = "".join(
        f'<div class="stat {slug}"><div class="n">{n}</div>'
        f'<div class="l">{slug}</div></div>'
        for slug, n in counts.items()
        # An empty ignore list is not worth a tile; the others always are.
        if slug != "ignored" or n
    )
    stats += (
        f'<div class="stat"><div class="n">{report.library_albums}</div>'
        f'<div class="l">in library</div></div>'
    )

    # Built with concatenation rather than a nested f-string: escaped quotes
    # inside an f-string expression are Python 3.12+ (PEP 701), and 3.11 is the
    # supported floor.
    def _tab(slug: str) -> str:
        active = ' class="active"' if slug == "missing" else ""
        return (
            '<button data-status="' + slug + '"' + active + '>'
            + slug.title() + " (" + str(counts[slug]) + ")</button>"
        )

    order = ["missing", "uncertain", "owned"]
    if counts["ignored"]:
        order.append("ignored")
    tabs = "".join(_tab(s) for s in order) + '<button data-status="all">All</button>'

    cards = "".join(_card(m) for m in report.matches)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Collection Audit — {e(report.account)}</title>
<style>{CSS}</style>
</head>
<body>
<div id="toolbar">
  <h1>Collection Audit</h1>
  <span class="meta">{e(report.source)}:{e(report.account)} · {report.considered} of
    {report.fetched} on {e(media)} · {e(index_label)} · {stamp}</span>
  <div id="controls">
    <div id="tabs">{tabs}</div>
    <input id="search" type="search" placeholder="filter artist or title…"
           autocomplete="off">
  </div>
</div>
<div id="summary">{stats}</div>
<div id="grid">{cards}</div>
<div id="empty">Nothing matches that filter.</div>
<script>{JS}</script>
</body>
</html>
"""
