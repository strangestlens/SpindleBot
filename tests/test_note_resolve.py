"""
Free text -> a note subject. The rule under test throughout: NEVER GUESS.

A wrong auto-resolution is the worst outcome here — it files writing under an
album the author did not mean, and nothing surfaces the mistake. So a near miss
comes back AMBIGUOUS with candidates, and creating a subject the library does
not have requires an explicit --new.
"""
from __future__ import annotations

import pytest

from spindlebot.core.collection import LibraryAlbum
from spindlebot.core.enums import NoteSubjectKind
from spindlebot.core.notes import NoteSubjectRef
from spindlebot.services.note_resolve import ResolutionStatus, resolve

LIBRARY = [
    LibraryAlbum("Old 97's", "Fight Songs", 1999, "mb-fight"),
    LibraryAlbum("Old 97's", "Hitchhike to Rhome", 1994, None),
    LibraryAlbum("Old 97's", "Too Far to Care", 1997, None),
    LibraryAlbum("Loreena McKennitt", "An Ancient Muse", 2006, "mb-muse"),
    LibraryAlbum("Afro Celt Sound System", "Volume 1: Sound Magic", 1996, None),
    LibraryAlbum("Massive Attack", "Mezzanine", 1998, None),
]


# ── artist ───────────────────────────────────────────────────────────────────

def test_artist_resolves_through_the_normalized_key():
    """"Old 97s" is how the corpus spells it; "Old 97's" is how it is tagged."""
    r = resolve(LIBRARY, artist="Old 97s")
    assert r.ok and r.subject.kind is NoteSubjectKind.ARTIST
    assert r.subject.subject_key == NoteSubjectRef.for_artist("Old 97's").subject_key


def test_artist_display_name_comes_from_the_library_not_the_typing():
    assert resolve(LIBRARY, artist="old 97s").subject.artist_name == "Old 97's"


def test_unknown_artist_is_unmatched_and_suggests_near_spellings():
    r = resolve(LIBRARY, artist="Loreena McKennit")  # one 't'
    assert r.status is ResolutionStatus.UNMATCHED
    assert [c.label for c in r.candidates] == ["Loreena McKennitt"]


def test_a_near_spelling_never_resolves_on_its_own():
    """difflib exists to suggest, never to decide."""
    assert not resolve(LIBRARY, artist="Loreena McKennit").ok


def test_new_creates_an_artist_the_library_does_not_have():
    r = resolve(LIBRARY, artist="Kathryn Joseph", allow_new=True)
    assert r.ok and r.subject.artist_name == "Kathryn Joseph"


# ── album ────────────────────────────────────────────────────────────────────

def test_album_resolves_via_the_shared_matcher():
    r = resolve(LIBRARY, artist="Old 97s", album="Fight Songs")
    assert r.ok and r.subject.kind is NoteSubjectKind.ALBUM
    assert r.subject.album_title == "Fight Songs"


def test_a_resolved_album_carries_the_librarys_mbid():
    """Which is what makes the subject key survive a later re-tag."""
    assert resolve(LIBRARY, artist="Old 97s", album="Fight Songs").subject.mbid == "mb-fight"


def test_album_resolves_without_an_artist_when_the_title_is_unique():
    r = resolve(LIBRARY, album="Mezzanine")
    assert r.ok and r.subject.artist_name == "Massive Attack"


def test_a_duplicate_title_without_an_artist_is_ambiguous():
    library = [*LIBRARY, LibraryAlbum("Another Band", "Mezzanine", 2001, None)]
    r = resolve(library, album="Mezzanine")
    assert r.status is ResolutionStatus.AMBIGUOUS
    assert "--artist" in r.reason


def test_a_mistyped_title_offers_the_artists_albums_rather_than_guessing():
    r = resolve(LIBRARY, artist="Old 97s", album="Fite Songs")
    assert r.status is ResolutionStatus.AMBIGUOUS
    # Labels now carry the --mbid selector, so match on the album rather than
    # the whole string.
    assert any("Fight Songs" in c.label for c in r.candidates)
    assert all(c.artist == "Old 97's" for c in r.candidates)


def test_an_album_the_library_lacks_is_unmatched_without_new():
    r = resolve(LIBRARY, artist="Massive Attack", album="Blue Lines")
    assert r.status in (ResolutionStatus.UNMATCHED, ResolutionStatus.AMBIGUOUS)
    assert not r.ok


def test_new_creates_an_album_the_library_does_not_have():
    """The corpus contains notes about records not owned — "Look for Morada Del
    Corazon" — so this path is load-bearing, not an escape hatch."""
    r = resolve(LIBRARY, artist="Loreena McKennitt", album="Morada Del Corazon",
                allow_new=True)
    assert r.ok and r.subject.album_title == "Morada Del Corazon"


# ── track ────────────────────────────────────────────────────────────────────

def test_track_resolves_under_its_resolved_album():
    r = resolve(LIBRARY, artist="Old 97s", album="Fight Songs", track="Murder")
    assert r.ok and r.subject.kind is NoteSubjectKind.TRACK
    assert r.subject.subject_key == NoteSubjectRef.for_track(
        "Old 97's", "Fight Songs", "Murder", "mb-fight"
    ).subject_key


def test_a_track_without_an_album_is_a_usage_error():
    """A track key is scoped inside its album; there is nothing to scope to."""
    with pytest.raises(ValueError, match="requires --album"):
        resolve(LIBRARY, artist="Old 97s", track="Murder")


def test_an_unresolvable_album_does_not_yield_a_track_subject():
    r = resolve(LIBRARY, artist="Old 97s", album="Fite Songs", track="Murder")
    assert not r.ok and r.subject is None


def test_a_track_title_is_taken_as_typed():
    """LibraryAlbum carries no track list, so the ALBUM is verified and the
    track title is not. Documented limitation, pinned so it is a decision."""
    r = resolve(LIBRARY, artist="Old 97s", album="Fight Songs",
                track="A Song That Is Not On This Record")
    assert r.ok and r.subject.track_title == "A Song That Is Not On This Record"


# ── usage ────────────────────────────────────────────────────────────────────

def test_resolving_nothing_is_a_usage_error():
    with pytest.raises(ValueError, match="nothing to resolve"):
        resolve(LIBRARY)


def test_an_empty_library_never_silently_resolves():
    """The failure mode library_index already refuses for the audit: an empty
    index makes everything look new."""
    assert not resolve([], artist="Old 97s").ok


# ── the leading article (review follow-up, PR #72) ───────────────────────────
# `artist_key` stopped folding a leading article, because folding it merged "The
# Band" with "Band" permanently. Resolution absorbs the forgiveness instead: it
# matches article-insensitively and then keys off the LIBRARY's spelling, so
# typing either form still lands on ONE subject.

ARTICLED = [
    LibraryAlbum("The Beatles", "Revolver", 1966, None),
    LibraryAlbum("The Band", "Music from Big Pink", 1968, None),
]


@pytest.mark.parametrize("typed", ["The Beatles", "Beatles", "the beatles", "BEATLES"])
def test_either_spelling_resolves_to_the_librarys_artist(typed):
    r = resolve(ARTICLED, artist=typed)
    assert r.ok and r.subject.artist_name == "The Beatles"


def test_both_spellings_land_on_one_subject():
    """The invariant that makes the strict identity key safe."""
    with_article = resolve(ARTICLED, artist="The Beatles").subject.subject_key
    without = resolve(ARTICLED, artist="Beatles").subject.subject_key
    assert with_article == without


def test_an_exact_key_match_wins_over_the_loose_one():
    """An artist literally named "Band" is not "The Band"."""
    library = [*ARTICLED, LibraryAlbum("Band", "Self Titled", 2000, None)]
    assert resolve(library, artist="Band").subject.artist_name == "Band"
    assert resolve(library, artist="The Band").subject.artist_name == "The Band"


def test_a_loose_match_spanning_two_artists_refuses():
    """`normalize_artist` folds strictly more than the identity key — the article
    AND a Discogs disambiguation suffix — so the loose step can reach two real
    subjects at once. Guessing between them is what this module never does."""
    library = [
        LibraryAlbum("The Band", "Music from Big Pink", 1968, None),
        LibraryAlbum("Band (2)", "Something Else", 2000, None),
    ]
    r = resolve(library, artist="band")
    assert r.status is ResolutionStatus.AMBIGUOUS
    assert {c.label for c in r.candidates} == {"The Band", "Band (2)"}


def test_a_mistyped_album_under_an_article_less_artist_still_offers_candidates():
    r = resolve(ARTICLED, artist="Beatles", album="Revolvr")
    assert not r.ok
    assert "The Beatles — Revolver" in [c.label for c in r.candidates]


# ── review round 3 (PR #74) ──────────────────────────────────────────────────

def test_a_typod_artist_is_never_accepted_even_with_an_exact_album():
    """The album path used to call `match_items` directly, inheriting its FUZZY
    artist candidates — so `Loreena McKennit` + `An Ancient Muse` came back
    OWNED and filed under `Loreena McKennitt`, while the artist-only path
    refused that same typo. Two rules for "is this the same artist" is one too
    many, and the loose one silently misfiles notes."""
    assert not resolve(LIBRARY, artist="Loreena McKennit").ok, "precondition"
    r = resolve(LIBRARY, artist="Loreena McKennit", album="An Ancient Muse")
    assert not r.ok
    assert r.subject is None


def test_two_releases_sharing_artist_and_title_are_ambiguous():
    """Editions differ by `mb_albumid`, and the matcher returns whichever comes
    first — which would bake an arbitrary release id into the subject key."""
    library = [
        LibraryAlbum("Old 97's", "Fight Songs", 1999, "mb-original"),
        LibraryAlbum("Old 97's", "Fight Songs", 2019, "mb-deluxe"),
    ]
    r = resolve(library, artist="Old 97s", album="Fight Songs")
    assert r.status is ResolutionStatus.AMBIGUOUS
    assert "MusicBrainz id" in r.reason


def test_one_release_with_a_duplicate_row_still_resolves():
    """Same album listed by both indexes is not two editions — the union index
    can legitimately carry it twice with the same mbid."""
    library = [
        LibraryAlbum("Old 97's", "Fight Songs", 1999, "mb-fight"),
        LibraryAlbum("Old 97's", "Fight Songs", 1999, "mb-fight"),
    ]
    assert resolve(library, artist="Old 97s", album="Fight Songs").ok


def test_new_keys_an_unowned_album_off_the_librarys_artist_spelling():
    """Otherwise "Old 97s" and "Old 97's" fork into two subjects for one
    unowned record, even though resolution can already tell they are one
    artist."""
    a = resolve(LIBRARY, artist="Old 97s", album="Unreleased Thing", allow_new=True)
    b = resolve(LIBRARY, artist="Old 97's", album="Unreleased Thing", allow_new=True)
    assert a.subject.subject_key == b.subject.subject_key
    assert a.subject.artist_name == "Old 97's", "the library's spelling wins"


def test_new_still_works_for_an_artist_the_library_has_never_heard_of():
    """Nothing to canonicalize against, and --new says the library is not the
    authority — so the typed spelling is correct here."""
    r = resolve(LIBRARY, artist="Kathryn Joseph", album="Bones You Have Thrown Me",
                allow_new=True)
    assert r.ok and r.subject.artist_name == "Kathryn Joseph"


def test_a_track_ref_carries_its_albums_mbid():
    """Needed so the subject can be recognised as the same work once the album
    gains a MusicBrainz id."""
    r = resolve(LIBRARY, artist="Old 97s", album="Fight Songs", track="Murder")
    assert r.subject.mbid == "mb-fight"


# ── review round 4 (PR #74) ──────────────────────────────────────────────────

EDITIONS = [
    LibraryAlbum("Old 97's", "Fight Songs", 1999, "mb-original"),
    LibraryAlbum("Old 97's", "Fight Songs", 2019, "mb-deluxe"),
]


def test_the_ambiguous_release_message_names_a_flag_that_exists():
    """It said "resolve with --key <album_key>", which was never implemented —
    so a legitimate note about either release was impossible to add except with
    --new, which throws the release identity away."""
    r = resolve(EDITIONS, artist="Old 97s", album="Fight Songs")
    assert r.status is ResolutionStatus.AMBIGUOUS
    assert "--mbid" in r.reason


def test_the_candidates_offer_the_releases_not_the_discography():
    """Two textually identical rows are useless for choosing between editions,
    so the label carries the value --mbid wants."""
    r = resolve(EDITIONS, artist="Old 97s", album="Fight Songs")
    assert len(r.candidates) == 2
    assert {c.mb_albumid for c in r.candidates} == {"mb-original", "mb-deluxe"}
    assert all("--mbid" in c.label for c in r.candidates)


def test_mbid_selects_one_release():
    r = resolve(EDITIONS, artist="Old 97s", album="Fight Songs", mb_albumid="mb-deluxe")
    assert r.ok and r.subject.mbid == "mb-deluxe"


def test_mbid_picks_out_the_right_subject_key():
    a = resolve(EDITIONS, artist="Old 97s", album="Fight Songs", mb_albumid="mb-original")
    b = resolve(EDITIONS, artist="Old 97s", album="Fight Songs", mb_albumid="mb-deluxe")
    assert a.subject.subject_key != b.subject.subject_key


def test_an_unknown_mbid_is_refused_rather_than_ignored():
    r = resolve(EDITIONS, artist="Old 97s", album="Fight Songs", mb_albumid="mb-nope")
    assert not r.ok and "MusicBrainz id" in r.reason


def test_the_loose_artist_match_folds_punctuation_too():
    """`normalize_artist` alone is not an equality basis — the fourth instance of
    that trap on this branch, and this one silently emptied the candidate list
    for the very artist the strict key was introduced for."""
    from spindlebot.services.note_resolve import _albums_by_loose_artist
    assert len(_albums_by_loose_artist(EDITIONS, "Old 97s")) == 2
    assert len(_albums_by_loose_artist(EDITIONS, "old 97's")) == 2


def test_the_loose_artist_match_still_ignores_the_article():
    from spindlebot.services.note_resolve import _albums_by_loose_artist
    library = [LibraryAlbum("The Beatles", "Revolver", 1966, None)]
    assert len(_albums_by_loose_artist(library, "Beatles")) == 1


# ── review round 5 (PR #74) ──────────────────────────────────────────────────

AMBIGUOUS_ARTISTS = [
    LibraryAlbum("The Band", "Music from Big Pink", 1968, None),
    LibraryAlbum("Band (2)", "Something Else", 2000, None),
]


def test_new_does_not_paper_over_an_ambiguous_artist():
    """`--new` says "the library is not the authority here". It does NOT say
    "pick something" — when the name reaches two real artists, inventing a third
    subject under the typed spelling is the guess this module refuses."""
    r = resolve(AMBIGUOUS_ARTISTS, artist="band", allow_new=True)
    assert r.status is ResolutionStatus.AMBIGUOUS
    assert r.subject is None


def test_new_does_not_paper_over_an_ambiguous_artist_on_the_album_path():
    r = resolve(AMBIGUOUS_ARTISTS, artist="band", album="Some Record", allow_new=True)
    assert r.status is ResolutionStatus.AMBIGUOUS


def test_new_does_not_paper_over_an_ambiguous_RELEASE():
    """The album is in the library twice; the caller still has to say which."""
    r = resolve(EDITIONS, artist="Old 97s", album="Fight Songs", allow_new=True)
    assert r.status is ResolutionStatus.AMBIGUOUS


def test_new_still_works_when_nothing_competes():
    """Ambiguous is not the same as absent, and only the former blocks --new."""
    assert resolve(AMBIGUOUS_ARTISTS, artist="Kathryn Joseph", allow_new=True).ok


def test_the_release_ambiguity_check_survives_the_real_index_path():
    """`library_index._dedupe` collapsed two releases into one BEFORE the
    resolver saw them, which made this whole check — and `--mbid` — dead code on
    the actual `note add` path. Resolution is only able to refuse to guess
    between editions it can see."""
    from spindlebot.services.library_index import _dedupe
    indexed = _dedupe(list(EDITIONS))
    assert len(indexed) == 2, "distinct MBIDs must survive the index"
    assert resolve(indexed, artist="Old 97s", album="Fight Songs").status \
        is ResolutionStatus.AMBIGUOUS
    assert resolve(indexed, artist="Old 97s", album="Fight Songs",
                   mb_albumid="mb-deluxe").subject.mbid == "mb-deluxe"


# ── review round 6 (PR #74) ──────────────────────────────────────────────────

def test_mbid_without_an_album_is_a_usage_error():
    """An mbid names one RELEASE, so it is meaningless on an artist query — and
    it was silently ignored there."""
    with pytest.raises(ValueError, match="needs --album"):
        resolve(LIBRARY, artist="Old 97s", mb_albumid="mb-fight")


def test_an_unknown_mbid_is_refused_even_under_new():
    """`--new` is for something the library lacks, not for asserting a release id
    nothing backs. It was being dropped, so the subject came out name-keyed and
    the requested id vanished."""
    r = resolve(LIBRARY, artist="Old 97s", album="Fight Songs",
                mb_albumid="mb-typo", allow_new=True)
    assert not r.ok and "MusicBrainz id" in r.reason


def test_new_does_not_slip_past_an_uncertain_title():
    """"Fite Songs" against a library holding "Fight Songs" was creating a second
    subject instead of asking. The gate is the match STATUS, not how many
    releases share the title."""
    r = resolve(LIBRARY, artist="Old 97s", album="Fite Songs", allow_new=True)
    assert r.status is ResolutionStatus.AMBIGUOUS
    assert r.subject is None


def test_new_still_creates_a_genuinely_absent_album():
    r = resolve(LIBRARY, artist="Old 97s", album="Wreck Your Life", allow_new=True)
    assert r.ok and r.subject.album_title == "Wreck Your Life"


def test_mbid_must_agree_with_the_rest_of_the_query():
    """An id names one release. With a mismatched artist, `--new` fired first and
    produced a name-keyed subject with the requested id thrown away — a precise
    claim answered with a guess."""
    r = resolve(LIBRARY, artist="Wrong Artist", album="An Ancient Muse",
                mb_albumid="mb-muse", allow_new=True)
    assert not r.ok
    assert "mb-muse" in r.reason


def test_mbid_with_a_mismatched_album_is_also_refused():
    r = resolve(LIBRARY, artist="Loreena McKennitt", album="Wrong Album",
                mb_albumid="mb-muse", allow_new=True)
    assert not r.ok


def test_mbid_with_a_matching_query_still_resolves():
    r = resolve(LIBRARY, artist="Loreena McKennitt", album="An Ancient Muse",
                mb_albumid="mb-muse", allow_new=True)
    assert r.ok and r.subject.mbid == "mb-muse"
