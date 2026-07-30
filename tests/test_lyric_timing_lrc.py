from lyric_timing.lrc import Line, format_lrc, parse_lrc


def test_parse_basic():
    lines = parse_lrc("[00:12.34]Hello\n[01:02.50]World\n")
    assert lines == [Line(12.34, "Hello"), Line(62.5, "World")]


def test_parse_preserves_file_order_not_time_order():
    lines = parse_lrc("[00:30.00]Second\n[00:10.00]First\n")
    assert [ln.text for ln in lines] == ["Second", "First"]
    assert [ln.time for ln in lines] == [30.0, 10.0]


def test_parse_skips_untimed_and_metadata_lines():
    text = "[ar:Artist]\n\nplain text line\n[00:05.00]Real line\n"
    lines = parse_lrc(text)
    assert lines == [Line(5.0, "Real line")]


def test_parse_strips_leading_space_in_text():
    # _plain_to_lrc writes "[00:00.00] line" with a space
    lines = parse_lrc("[00:00.00] Hello\n")
    assert lines[0].text == "Hello"


def test_parse_requires_decimal_seconds():
    # mirror lrc-editor: [mm:ss] without decimals is not matched
    assert parse_lrc("[00:12]No decimals\n") == []


def test_format_matches_lrc_editor_format():
    out = format_lrc([Line(12.34, "Hello"), Line(62.5, "World")])
    assert out == "[00:12.34]Hello\n[01:02.50]World\n"


def test_format_sorts_by_time():
    out = format_lrc([Line(30.0, "Second"), Line(10.0, "First")])
    assert out.splitlines() == ["[00:10.00]First", "[00:30.00]Second"]


def test_roundtrip():
    original = "[00:10.00]First\n[00:30.50]Second\n[01:15.25]Third\n"
    assert format_lrc(parse_lrc(original)) == original
