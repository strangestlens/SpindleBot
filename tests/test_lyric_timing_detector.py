from lyric_timing.detector import (
    ALL_TIMESTAMPS_IDENTICAL,
    LOW_DISTINCT_TIMESTAMPS,
    NO_TIMED_LINES,
    NON_MONOTONIC,
    TIMESTAMPS_CRAMMED_EARLY,
    audit_lrc,
    audit_lrc_text,
)

PLAIN_TO_LRC_OUTPUT = "[00:00.00] First line\n[00:00.00] Second line\n[00:00.00] Third line"

HEALTHY = "[00:10.00]One\n[00:55.00]Two\n[01:40.00]Three\n[02:30.00]Four\n"


def test_plain_to_lrc_signature_is_suspicious():
    result = audit_lrc_text(PLAIN_TO_LRC_OUTPUT)
    assert result.suspicious
    assert ALL_TIMESTAMPS_IDENTICAL in result.reasons


def test_healthy_file_is_not_suspicious():
    result = audit_lrc_text(HEALTHY, duration=180.0)
    assert not result.suspicious
    assert result.reasons == ()


def test_low_distinct_ratio():
    # 10 lines, 2 distinct timestamps -> ratio 0.2 < 0.3
    text = "\n".join(
        [f"[00:00.00]Line {i}" for i in range(8)]
        + ["[00:10.00]Line 8", "[00:10.00]Line 9"]
    )
    result = audit_lrc_text(text)
    assert result.suspicious
    assert LOW_DISTINCT_TIMESTAMPS in result.reasons


def test_ratio_check_skipped_for_tiny_files():
    # 3 lines, 2 distinct: fine — a short outro lyric can look like this
    text = "[00:10.00]A\n[00:10.00]B\n[00:20.00]C\n"
    assert not audit_lrc_text(text).suspicious


def test_crammed_early_requires_duration():
    text = "[00:05.00]One\n[00:20.00]Two\n[00:40.00]Three\n"
    assert not audit_lrc_text(text).suspicious
    result = audit_lrc_text(text, duration=200.0)
    assert result.suspicious
    assert result.reasons == (TIMESTAMPS_CRAMMED_EARLY,)


def test_spread_out_file_with_duration_is_fine():
    result = audit_lrc_text(HEALTHY, duration=170.0)
    assert not result.suspicious


def test_non_monotonic():
    text = "[00:30.00]Second\n[00:10.00]First\n[01:00.00]Third\n"
    result = audit_lrc_text(text, duration=90.0)
    assert result.suspicious
    assert NON_MONOTONIC in result.reasons


def test_untimed_content_is_suspicious():
    result = audit_lrc_text("These are lyrics\nwith no timestamps at all\n")
    assert result.suspicious
    assert result.reasons == (NO_TIMED_LINES,)


def test_empty_file_is_not_suspicious():
    assert not audit_lrc_text("").suspicious
    assert not audit_lrc_text("\n\n").suspicious


def test_stats_populated():
    result = audit_lrc_text(HEALTHY, duration=180.0)
    assert result.stats == {
        "line_count": 4,
        "distinct_times": 4,
        "max_time": 150.0,
        "duration": 180.0,
    }


def test_audit_lrc_reads_file(tmp_path):
    p = tmp_path / "song.lrc"
    p.write_text(PLAIN_TO_LRC_OUTPUT, encoding="utf-8")
    result = audit_lrc(p)
    assert result.suspicious
    assert result.path == p
