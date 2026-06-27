"""Tests for spindlebot.core.albums (pure album-grouping key)."""
from __future__ import annotations

from spindlebot.core.albums import album_key


def test_same_albumartist_album_yield_same_key():
    assert album_key("Boards of Canada", "Geogaddi") == \
        album_key("Boards of Canada", "Geogaddi")


def test_key_is_case_and_whitespace_insensitive():
    assert album_key("  Radiohead ", "KID A") == album_key("radiohead", "kid a")


def test_different_albums_differ():
    assert album_key("Radiohead", "Kid A") != album_key("Radiohead", "Amnesiac")


def test_mb_albumid_takes_precedence_over_tags():
    # Same release id ⇒ same album even when the advisory tags differ.
    a = album_key("Old Name", "Old Title", mb_albumid="abc-123")
    b = album_key("New Name", "New Title", mb_albumid="abc-123")
    assert a == b


def test_mb_albumid_distinct_from_tag_fallback():
    assert album_key("A", "B", mb_albumid="abc-123") != album_key("A", "B")


def test_blank_mb_albumid_falls_back_to_tags():
    assert album_key("A", "B", mb_albumid="   ") == album_key("A", "B")


def test_artist_album_swap_does_not_collide():
    assert album_key("A", "B") != album_key("B", "A")


def test_missing_tags_are_handled():
    # No crash on None; collapses to a stable empty-key (inventory decides
    # whether to create an album at all).
    assert album_key(None, None) == album_key("", "")
