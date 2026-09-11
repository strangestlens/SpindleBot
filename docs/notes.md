# Listening notes

Notes about what you heard, attached at **artist**, **album**, or **track**
level. Multiple notes per subject, edited by appending rather than overwriting,
grouped into listening sessions, and exportable back to plain markdown at any
time.

Everything else in `spindlebot.db` is observed and regenerable — drop it, run
`spindlebot inventory`, and the rows come back. Notes are the first **authored**
data in the system. That single fact explains most of the design below.

## Writing a note

```bash
spindlebot note add --artist "Old 97s" --album "Fight Songs" -m "A delightful record."
spindlebot note add --artist "Old 97s" --album "Fight Songs" --track "What We Talk About" \
    -m "Tango through line, virtuosic instrumentals." --tag surprise
spindlebot note add --artist "Loreena McKennitt" -m "Her liner notes are the record."
```

### Getting text in

Four input modes, in precedence order:

| Mode | Use |
|------|-----|
| `-m/--message` | Repeatable; each value becomes a paragraph |
| `-F/--file <path>` | Read a file you already wrote |
| `-`, or any pipe | `pbpaste \| spindlebot note add --album X` |
| nothing, on a terminal | Opens `$EDITOR` (`$VISUAL`, then `vi`) |

`-m` is repeatable so multi-paragraph input never has to fight the shell over
newlines, and a single `-m` value keeps whatever newlines it contains.

The editor buffer is deliberately empty rather than carrying a commented header
the way `git commit` does: `#` is a markdown heading here, so stripping comments
and keeping the author's headings would be the same operation.

### Naming a subject

Free text is matched against the library — beets and the SpindleBot DB, unioned
— using the same artist-scoped matcher the collection audit uses. Spelling is
folded: `Old 97s` finds `Old 97's`, `the beatles` finds `The Beatles`.

The CLI **refuses rather than guesses**. An ambiguous or unmatched subject exits
non-zero and lists what the library does have:

```
$ spindlebot note add --artist "Old 97s" --album "Fite Songs" -m "..."
ambiguous: similar title
    Old 97's — Fight Songs
    Old 97's — Too Far to Care
    (use --new to write about something the library doesn't have)
```

A note filed under the wrong album is invisible — there is no later error to
notice — so this is the one place the tool is deliberately unhelpful.

`--new` writes about something the library does not have. That is a real use
case, not an escape hatch: a note that says "look for this record" is about
something you do not own yet.

## Reading back

```bash
spindlebot note list                              # everything, newest first
spindlebot note list --artist "Old 97s"           # the band: artist, album AND track notes
spindlebot note list --album "Fight Songs"        # that album and its tracks
spindlebot note list --kind track                 # narrow to one level
spindlebot note list --tag todo                   # your listening queue
spindlebot note show 12 --history                 # every revision, oldest last
```

Filters reach **downward**: `--artist` returns that band's album and track notes
too, because the question being asked is "what have I said about this band".
Use `--kind` when you want the narrow version.

Ordering is by creation, deliberately. This is a log — fixing a typo in a 2019
note should not vault it above everything written since. Edit order stays
recoverable through the revision chain.

## Editing, and why nothing is lost

```bash
spindlebot note edit 12 -m "Revised thoughts."
spindlebot note rm 12          # soft — recoverable
spindlebot note restore 12
```

`edit` **appends** a revision; the previous text stays in the chain and
`note show --history` prints it. An unchanged body writes nothing at all, so
opening a note in your editor and closing it untouched — or an editor that
rewrites line endings on the way out — does not create a phantom revision.

`rm` is a status change, not a delete. The revisions survive it.

## Sessions

A session is one sitting: an evening with a record that produces several notes
across several subjects.

```bash
spindlebot note session start --title "Sunday CDs"     # -> session 3
spindlebot note add --album "Fight Songs" --session 3 -m "..."
spindlebot note list --session 3
spindlebot note sessions --since 2026-01-01
```

`session_id` is optional — a note written outside a sitting is still a
first-class note.

## Importing what you already wrote

Markdown headings name subjects, and the prose under a heading is the note:

```markdown
# Artist
## Album
### Track
```

`--root-level` shifts that window down the document. A file that opens with a
grouping heading like `# CDs` needs `--root-level 2`, which reads `##` as the
artist level and reports `CDs` as a skipped heading rather than inventing an
artist by that name.

```bash
spindlebot note import notes.md --root-level 2 --dry-run   # resolution table, writes nothing
spindlebot note import notes.md --root-level 2             # one import = one session
```

Three guarantees:

- **Atomic.** One unresolved note blocks the whole import. Use `--new`, fix the
  headings, or pass `--skip-unresolved` to accept a partial import explicitly.
- **Idempotent.** A subject already carrying that exact body is reported as a
  duplicate, so re-running an import is safe.
- **Nothing is dropped in silence.** Headings that name no subject are listed.

Always `--dry-run` first. The resolution table is what tells you a heading level
is wrong before anything is written.

### All the prose under a heading is one note

Splitting on blank lines would be a guess about where one thought ends, and a
wrong guess shreds a paragraph into fragments. Two notes means two headings.

A heading with no prose under it produces no note — `# Old 97's` above
`## Fight Songs` is structure, not an empty note about the band.

## Exporting

```bash
spindlebot note export                            # to stdout
spindlebot note export -o ~/Documents/notes.md
spindlebot note export --artist "Old 97s" --since 2026-01-01
```

`parse(render(x)) == x` is a tested contract: export a corpus, import it into an
empty database, export again, and the bytes match. That is what keeps your
writing out of a SQLite-shaped trap. Export regularly — nothing else in this
system can rebuild these rows.

Output is grouped by artist → album → track rather than newest-first, because an
exported document is meant to be read as a document.

**One known limitation.** Heading nesting cannot express "this album has no
artist" once an artist heading is in scope — there is no syntax that unsets a
level without also setting it. So an album that appears both with and without an
artist folds into the parented subject on re-import. The writing itself is never
lost, and it only arises from hand-written input that names an album without
ever naming its artist.

## Where notes live

`note_subject` / `note_session` / `note` / `note_revision` / `note_tag`, added
in schema v8. Subjects key on the same identity the library uses — an album
subject's key **is** `album.album_key` — but carry no foreign key into it, so a
note about an unripped album, an artist with no rows, or a track whose audio was
pruned is all perfectly legal.

See [architecture](architecture.md) for how that fits the content-addressed
model, and `CLAUDE.md` for the rules that must not drift.
