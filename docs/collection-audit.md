# Collection audit

*Optional.* Compares a collection you already maintain elsewhere against the
digital library and lists the discs you own but haven't ripped. Entirely
assistive — it reads the library and writes nothing but a fetch cache.
Unconfigured, it simply doesn't run, and nothing in the import or sync path
depends on it.

```bash
python3 -m spindlebot collection-audit --handle your-discogs-handle
```

Put the handle in `config.toml` under `[collection]` and the flag becomes
optional; `--handle` always overrides it.

```
discogs:your-discogs-handle — 212 item(s), 152 on cd
library (beets 112, db 177) — 176 unique album(s)

UNCERTAIN (1) — confirm these yourself
  1506644   Tori Amos — Live At Montreux 1991 & 1992 (2008)
      ≈ TORI AMOS — Live At Montreux: 1991/1992 (0.93)

MISSING (47)
  500883    Afro Celt Sound System — Volume 1 Sound Magic (1996)
  21066448  Akercocke — Renaissance In Extremis (2021)
  16500462  Beck — Hyperspace (2020)
  ...

104 owned · 1 uncertain · 47 missing

Not going to rip one? spindlebot collection-ignore 500883
```

Every missing row leads with the id, because a list you can't act on from is
just a list.

| Flag | Effect |
|------|--------|
| `--source discogs\|fixture` | Where the collection comes from |
| `--media cd,vinyl` | Which media count as rippable (default `cd`, which includes CDrs) |
| `--index auto\|beets\|db` | Which view of the library to compare against (default `auto`, the union — see [Which library index?](#which-library-index)) |
| `--refresh` | Re-fetch instead of using the cached collection |
| `--strict` | Treat uncertain matches as missing |
| `--all` | Also list what you already own |
| `--show-ignored` | Include ignored discs |
| `--html <file>` | Also write a browsable [HTML report](#html-report) |
| `--json` | Structured output |

Results land in three buckets, not two. `uncertain` exists so that a
normalization miss sends you to check a row rather than to re-rip a disc you
already own.

## Sources

**Discogs** needs no credentials for a public collection. A personal access
token in `secrets.toml` (`[discogs] token`) raises the API rate limit from 25 to
60 requests/minute and is required for a private collection.

**Adding another source** means writing one provider: an impure client that
fetches, and a pure transformer that maps its payload to `CollectionItem`. The
`fixture` provider — a JSON file you write by hand — is a working example, and
doubles as the way in for anyone not on Discogs.

## Ignoring discs you're not going to rip

Damaged discs, gifts, a release the matcher can't reach. Without somewhere to
put these, the missing list keeps a permanent floor of noise and stops being
worth opening. Every line of the missing list leads with the id you need:

```bash
python3 -m spindlebot collection-ignore 16500462 --reason "owned as a katakana-tagged copy"
```

| Command | Effect |
|---------|--------|
| `collection-ignore <id...>` | Stop reporting these. Takes a bare id or a full `discogs:<id>`, one or many |
| `collection-ignore --list` | What's ignored, with reasons |
| `collection-ignore --remove <id...>` | Put one back (`--unignore` works too) |
| `collection-ignore --clear --yes` | Un-ignore everything |

Ignoring is always reversible and never destroys the verdict underneath — an
ignored disc keeps its real `missing` / `uncertain` status, so un-ignoring
restores exactly what the audit said before. And if you ignore something and
then rip it later, the rip wins: an owned album is never reported as ignored, so
stale entries quietly stop mattering.

The list lives at `~/.config/spindlebot/collection-ignore.json` (override with
`[collection] ignore_path`). It's a plain file, not a schema change — the audit
stays read-only against your library.

## collection-browser — click-to-ignore UI

```bash
./collection-browser --handle your-discogs-handle
```

Opens a local web UI at `localhost` with the same report, plus an **ignore**
button on every card and an **undo** on every ignored one. Counts and tabs
update live; each action also raises a toast with its own Undo. Sibling to
[`lrc-editor`](lyrics.md#lrc-editor--visual-timestamp-editor) — same palette,
same shape, same single-file design — and it lives outside `spindlebot/`
deliberately so the pipeline package never grows a Flask dependency.

| Flag | Effect |
|------|--------|
| `--handle` / `--source` / `--media` / `--index` / `--refresh` | As `collection-audit` |
| `--port N` | Fixed port (default: a free one) |
| `--no-open` | Don't launch a browser |

It binds to `127.0.0.1` and refuses cross-origin POSTs — any page in your
browser can post to a localhost port, and a drive-by edit of your ignore list is
still an edit.

## HTML report

```bash
python3 -m spindlebot collection-audit --html ~/Desktop/collection.html
```

Writes a single self-contained page — inline CSS and JS, no server, no build
step — styled to match [`lrc-editor`](lyrics.md#lrc-editor--visual-timestamp-editor).
Cover thumbnails, status tabs, and a live text filter. The header names which
library indexes answered, same as the CLI. The only network requests it makes
are for the cover images; everything else works offline.

The exported page is inert: unlike `collection-browser` it has no server behind
it, so it ships without the ignore controls rather than with dead buttons.

## Which library index?

Neither backend is a complete picture, and they go stale in opposite directions:

| Index | Knows about | Blind to |
|-------|-------------|----------|
| `beets` | Everything it imported and still tracks | Albums that reached a drive without a `beet import` |
| `db` | Everything `spindlebot inventory` has scanned at any location | A fresh import that hasn't synced yet |

Measured on a real library: 67 albums existed **only** in the SpindleBot DB
(copied-in files with no `beets_item_id`) and 2 existed **only** in beets.
Auditing against beets alone reported **95 of 152 CDs missing — 48 of them
owned**.

So `auto` (the default) unions them: an album counts as owned if either index
knows it, which can only ever shrink the missing list. Every run prints the
breakdown (`library (beets 112, db 177) — 176 unique album(s)`), because when an
album is wrongly reported missing, the index is the first suspect.

If both indexes come back empty the audit **fails** rather than reporting your
entire collection as missing.

This generalizes: anything else that needs to answer "do I have this?" should
read `services/library_index.py` rather than picking a single backend.

## Why matching is artist-scoped

The matcher resolves the artist first, then compares titles only within that
artist's albums. This is deliberate and load-bearing: a whole-string similarity
over `"artist title"` measurably does not work. A multi-disc rip carries a
`" - <disc title>"` suffix from MusicBrainz, and Discogs release names run long
or short, so `Ummagumma` vs `Ummagumma - Live Album` scored 0.78 — below every
usable threshold and indistinguishable from an album that genuinely isn't there.
Seven real albums were reported missing that way.

A related constraint: non-Latin scripts must survive normalization. Blanket
NFKD plus combining-mark removal strips Japanese dakuten (パ decays to ハ, a
different kana), and an ASCII-only character class empties a katakana title
entirely. A library tagged in a different script than the collection lists
stays unmatchable without transliteration — that is what the ignore list is
for, not a reason to add a heavy dependency to `spindlebot/`.

See `tests/test_collection_match.py` for the match table that guards each case.
