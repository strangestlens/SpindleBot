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

# Only sent with the served page. The exported file must stay inert — a static
# report with dead buttons would be worse than one with none.
INTERACTIVE_CSS = """
.card { position: relative; }
.card .act {
  margin-left: auto; align-self: center; flex-shrink: 0;
  padding: 5px 11px; font-size: 11.5px;
  background: #2a2a4a; color: #8899aa;
}
.card[data-status="ignored"] .act { background: #1b4332; color: #6fcf97; }
.card .act:disabled { opacity: .4; cursor: default; }
#toast {
  position: fixed; left: 50%; bottom: 22px; transform: translateX(-50%);
  background: #16213e; border: 1px solid #0f3460; border-radius: 8px;
  padding: 9px 14px; font-size: 12.5px; color: #e0e0e0;
  display: flex; align-items: center; gap: 12px;
  opacity: 0; pointer-events: none; transition: opacity .18s; z-index: 20;
}
#toast.show { opacity: 1; pointer-events: auto; }
#toast.err { border-color: #e94560; color: #e94560; }
#toast button { padding: 4px 10px; font-size: 11.5px;
                background: #0f3460; color: #e0e0e0; }
"""

INTERACTIVE_JS = """
const toast = document.getElementById('toast');
const toastText = document.getElementById('toast-text');
const toastUndo = document.getElementById('toast-undo');
let toastTimer = null;
let lastAction = null;

function showToast(text, {error = false, undo = null} = {}) {
  toastText.textContent = text;
  toast.classList.toggle('err', error);
  toastUndo.style.display = undo ? '' : 'none';
  lastAction = undo;
  toast.classList.add('show');
  clearTimeout(toastTimer);
  // An error you blink and miss is an error you'll hit again. Success toasts
  // are disposable — the card's own undo button is the durable path.
  toastTimer = setTimeout(() => toast.classList.remove('show'), error ? 15000 : 6000);
}

function recount() {
  const tally = {missing: 0, uncertain: 0, owned: 0, ignored: 0};
  for (const card of cards) tally[card.dataset.status] += 1;
  for (const [slug, n] of Object.entries(tally)) {
    document.querySelectorAll('[data-count="' + slug + '"]')
      .forEach(el => { el.textContent = n; });
  }
}

async function toggle(card, verb) {
  const button = card.querySelector('.act');
  button.disabled = true;
  try {
    let res;
    try {
      res = await fetch(verb === 'ignore' ? '/ignore' : '/unignore', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({key: card.dataset.key}),
      });
    } catch (netErr) {
      // fetch only throws for network-level failures, and for a localhost tool
      // that means one thing: the server is gone. This page outlives the
      // process that served it — leaving a tab open overnight is the normal
      // case, not an edge case — and "Failed to fetch" tells you nothing.
      throw new Error(
        'collection-browser is not running — restart it and reload this page'
      );
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) throw new Error(data.error || ('request failed (' + res.status + ')'));
    // The verdict never changed, so undoing is just restoring the bucket.
    card.dataset.status = verb === 'ignore' ? 'ignored' : card.dataset.verdict;
    button.dataset.act = verb === 'ignore' ? 'undo' : 'ignore';
    button.textContent = button.dataset.act;
    recount();
    apply();
    const title = card.querySelector('.title').textContent;
    showToast(
      (verb === 'ignore' ? 'Ignored ' : 'Restored ') + title,
      {undo: () => toggle(card, verb === 'ignore' ? 'unignore' : 'ignore')},
    );
  } catch (err) {
    showToast(String(err.message || err), {error: true});
  } finally {
    button.disabled = false;
  }
}

document.getElementById('grid').addEventListener('click', (ev) => {
  const button = ev.target.closest('.act');
  if (!button) return;
  toggle(button.closest('.card'), button.dataset.act === 'undo' ? 'unignore' : 'ignore');
});

toastUndo.addEventListener('click', () => {
  const action = lastAction;
  toast.classList.remove('show');
  if (action) action();
});
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


def _card(match, *, interactive: bool = False) -> str:
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
    act = ""
    if interactive:
        # Owned albums have no ignore affordance — ignoring one is meaningless
        # (the audit drops the flag anyway) and the button would only confuse.
        if match.status is not MatchStatus.OWNED:
            verb = "undo" if match.ignored else "ignore"
            act = f'<button class="act" data-act="{verb}">{verb}</button>'
    return (
        f'<div class="card" data-status="{_status_slug(match)}" '
        f'data-id="{e(item.source_id, quote=True)}" '
        f'data-key="{e(item.key, quote=True)}" '
        f'data-verdict="{match.status.value}" '
        f'data-search="{haystack}">{img}'
        f'<div class="body">'
        f'<div class="artist">{e(item.artist)}</div>'
        f'<div class="title">{e(item.title)}</div>'
        f'<div class="sub">{sub}</div>{near}{link}'
        f'</div>{act}</div>'
    )


def render_html(
    report: AuditReport,
    *,
    generated_utc: float | None = None,
    interactive: bool = False,
) -> str:
    """Render an audit as a standalone HTML page.

    `interactive` adds per-card ignore/undo buttons wired to a local server.
    One renderer serves both the exported file and the served page so the
    markup and palette can't drift apart; the export stays inert.
    """
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

    # An empty ignore list isn't worth a tile in an export — but on the served
    # page you can create one at any moment, so it always has a home.
    def _shown(slug: str) -> bool:
        return slug != "ignored" or counts[slug] or interactive

    stats = "".join(
        f'<div class="stat {slug}"><div class="n" data-count="{slug}">{n}</div>'
        f'<div class="l">{slug}</div></div>'
        for slug, n in counts.items()
        if _shown(slug)
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
            + slug.title() + ' (<span data-count="' + slug + '">'
            + str(counts[slug]) + "</span>)</button>"
        )

    order = [s for s in ("missing", "uncertain", "owned", "ignored") if _shown(s)]
    tabs = "".join(_tab(s) for s in order) + '<button data-status="all">All</button>'

    cards = "".join(_card(m, interactive=interactive) for m in report.matches)
    page_css = CSS + (INTERACTIVE_CSS if interactive else "")
    page_js = JS + (INTERACTIVE_JS if interactive else "")
    # Every ignore is one click from being taken back, right where you made it.
    toast_html = (
        '<div id="toast"><span id="toast-text"></span>'
        '<button id="toast-undo">Undo</button></div>'
    ) if interactive else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Collection Audit — {e(report.account)}</title>
<style>{page_css}</style>
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
{toast_html}
<script>{page_js}</script>
</body>
</html>
"""
