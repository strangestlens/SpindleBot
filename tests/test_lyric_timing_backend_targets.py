"""CTC target construction for the real backend — no torch needed.

Mirrors MMS_FA's vocabulary shape: blank at index 0, lowercase romanized
characters after it, no word-separator token, star appended by us.
"""

import logging

from lyric_timing.backends.torchaudio_backend import (
    TorchaudioBackend,
    build_targets,
)

LABELS = ("-", *"aienoutsrmkldghybpwcvjzf'qx")
DICT = {c: i for i, c in enumerate(LABELS) if i > 0}
STAR = len(LABELS)


def decode(targets):
    """targets -> readable string ('*' for star, one char per token)."""
    return "".join("*" if t == STAR else LABELS[t] for t in targets)


def test_words_map_to_character_targets():
    targets, words, slices = build_targets("hey you", DICT)
    assert decode(targets) == "heyyou"
    assert words == ["hey", "you"]
    assert slices == [(0, 3), (3, 6)]


def test_blank_token_never_appears_inside_a_word():
    # '-' is the blank label; a hyphen in the lyrics must drop out, not map to 0
    targets, _, slices = build_targets("well-worn", DICT)
    assert 0 not in targets
    assert decode(targets) == "wellworn"
    assert slices == [(0, 8)]


def test_uppercase_and_accents_fold_into_the_vocabulary():
    targets, _, _ = build_targets("Éclair", DICT)
    assert decode(targets) == "eclair"


def test_apostrophes_are_kept():
    targets, _, _ = build_targets("don't", DICT)
    assert decode(targets) == "don't"


def test_unmappable_word_gets_no_slice():
    targets, words, slices = build_targets("1999 party", DICT)
    assert words == ["1999", "party"]
    assert slices == [None, (0, 5)]
    assert decode(targets) == "party"


def test_all_words_unmappable_yields_no_targets():
    targets, words, slices = build_targets("1999 !!!", DICT, star=STAR)
    assert targets == []
    assert words == ["1999", "!!!"]
    assert slices == [None, None]


def test_star_brackets_every_line():
    targets, words, slices = build_targets("hey you\nrun", DICT, star=STAR)
    assert decode(targets) == "*heyyou*run*"
    assert words == ["hey", "you", "run"]
    # slices index into targets, skipping the star positions
    assert slices == [(1, 4), (4, 7), (8, 11)]
    assert [decode(targets[a:b]) for a, b in slices] == ["hey", "you", "run"]


def test_without_star_lines_are_concatenated():
    targets, _, slices = build_targets("hey you\nrun", DICT)
    assert decode(targets) == "heyyourun"
    assert slices == [(0, 3), (3, 6), (6, 9)]


def test_blank_lines_do_not_get_their_own_star():
    targets, _, _ = build_targets("hey\n\n\nrun", DICT, star=STAR)
    assert decode(targets) == "*hey*run*"


def test_line_of_only_unmappable_words_still_gets_a_star():
    # the line contributes no tokens, but the audio it covers is real
    targets, words, slices = build_targets("hey\n1999\nrun", DICT, star=STAR)
    assert decode(targets) == "*hey**run*"
    assert words == ["hey", "1999", "run"]
    assert slices == [(1, 4), None, (6, 9)]


def test_wholly_unmappable_transcript_warns(caplog):
    # a non-roman-script lyric file maps to nothing at all — exactly the case
    # worth telling the user about, and the one where there are no targets to
    # align, so the diagnostic has to be emitted here rather than downstream
    with caplog.at_level(logging.WARNING,
                         logger="lyric_timing.backends.torchaudio_backend"):
        targets, words, slices = build_targets("1999 !!!\n2000", DICT, star=STAR)
    assert targets == []
    assert slices == [None, None, None]
    assert "vocabulary" in caplog.text.lower()


def test_a_few_unmappable_words_do_not_warn(caplog):
    with caplog.at_level(logging.WARNING,
                         logger="lyric_timing.backends.torchaudio_backend"):
        build_targets("hey you run 1999 party on", DICT, star=STAR)
    assert caplog.text == ""


class _FakeBundle:
    """Stands in for a torchaudio bundle: only get_labels is consulted."""

    def __init__(self, labels):
        self._labels = labels

    def get_labels(self, star=None, blank="-"):
        return self._labels


def test_vocabulary_for_the_english_model():
    # ('-', '|', 'E', 'T'): blank at 0, word separator at 1, both reserved
    d, separator, star = TorchaudioBackend(
        model="wav2vec2_en")._vocabulary(_FakeBundle(("-", "|", "E", "T")))
    assert separator == 1
    assert d == {"e": 2, "t": 3}
    assert star == 4


def test_vocabulary_for_the_multilingual_model():
    # MMS_FA has no word separator and its labels are already lowercase
    d, separator, star = TorchaudioBackend(
        model="mms_fa")._vocabulary(_FakeBundle(("-", "a", "i")))
    assert separator is None
    assert d == {"a": 1, "i": 2}
    assert star == 3


def test_an_unknown_model_is_refused():
    import pytest

    with pytest.raises(ValueError, match="unknown model"):
        TorchaudioBackend(model="whisper")
