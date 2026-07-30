import json

import pytest

from lyric_timing.cli import main

SUSPICIOUS = "[00:00.00] One\n[00:00.00] Two\n[00:00.00] Three"
HEALTHY = "[00:10.00]One\n[00:55.00]Two\n[01:40.00]Three\n"


@pytest.fixture
def library(tmp_path):
    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "01 Bad.lrc").write_text(SUSPICIOUS, encoding="utf-8")
    (album / "02 Good.lrc").write_text(HEALTHY, encoding="utf-8")
    return tmp_path


def test_audit_directory_human_output(library, capsys):
    assert main(["audit", str(library)]) == 0
    out = capsys.readouterr().out
    assert "01 Bad.lrc: all-timestamps-identical" in out
    assert "02 Good.lrc" not in out
    assert "1 of 2 suspicious" in out


def test_audit_json_output(library, capsys):
    assert main(["audit", str(library), "--json"]) == 0
    results = json.loads(capsys.readouterr().out)
    assert len(results) == 2
    by_name = {r["path"].rsplit("/", 1)[-1]: r for r in results}
    assert by_name["01 Bad.lrc"]["suspicious"] is True
    assert by_name["01 Bad.lrc"]["reasons"] == ["all-timestamps-identical"]
    assert by_name["02 Good.lrc"]["suspicious"] is False
    assert by_name["02 Good.lrc"]["stats"]["line_count"] == 3


def test_audit_single_file(library, capsys):
    lrc = library / "Artist" / "Album" / "01 Bad.lrc"
    assert main(["audit", str(lrc)]) == 0
    assert "1 of 1 suspicious" in capsys.readouterr().out


def test_audit_missing_file(tmp_path, capsys):
    assert main(["audit", str(tmp_path / "nope.lrc")]) == 2
    assert "no such file" in capsys.readouterr().err


# ── retime (mock backend) ────────────────────────────────────────────────────


@pytest.fixture
def track(tmp_path):
    audio = tmp_path / "song.flac"
    audio.write_bytes(b"not real audio")
    lrc = tmp_path / "song.lrc"
    lrc.write_text(SUSPICIOUS, encoding="utf-8")
    return audio, lrc


def test_retime_prints_lrc_by_default(track, capsys):
    audio, lrc = track
    assert main(["retime", str(audio), str(lrc), "--backend", "mock"]) == 0
    out = capsys.readouterr().out
    lines = out.strip().splitlines()
    assert len(lines) == 3
    assert lines[0].endswith("One")
    assert "[00:00.00]" not in out  # timestamps actually spread out
    assert lrc.read_text(encoding="utf-8") == SUSPICIOUS  # untouched


def test_retime_overwrite_writes_file(track):
    audio, lrc = track
    assert main(["retime", str(audio), str(lrc), "--backend", "mock", "--overwrite"]) == 0
    new = lrc.read_text(encoding="utf-8")
    assert "One" in new and "[00:00.00]" not in new


def test_retime_json_output(track, capsys):
    audio, lrc = track
    assert main(["retime", str(audio), str(lrc), "--backend", "mock", "--json"]) == 0
    results = json.loads(capsys.readouterr().out)
    assert [r["text"] for r in results] == ["One", "Two", "Three"]
    times = [r["time"] for r in results]
    assert times == sorted(times) and times[0] > 0
    assert all(0.0 <= r["confidence"] <= 1.0 for r in results)


def test_retime_plain_text_lyrics(tmp_path, capsys):
    audio = tmp_path / "song.flac"
    audio.write_bytes(b"x")
    lrc = tmp_path / "song.lrc"
    lrc.write_text("[ar:Artist]\nJust plain\nlyric lines\n", encoding="utf-8")
    assert main(["retime", str(audio), str(lrc), "--backend", "mock", "--json"]) == 0
    results = json.loads(capsys.readouterr().out)
    assert [r["text"] for r in results] == ["Just plain", "lyric lines"]


def test_retime_preserves_empty_text_markers(tmp_path, capsys):
    # a hand-placed empty-text timestamp (bounding an instrumental outro)
    # must survive a retime at its manual time
    audio = tmp_path / "song.flac"
    audio.write_bytes(b"x")
    lrc = tmp_path / "song.lrc"
    lrc.write_text(SUSPICIOUS + "\n[04:59.20] \n", encoding="utf-8")
    assert main(["retime", str(audio), str(lrc), "--backend", "mock"]) == 0
    out = capsys.readouterr().out
    assert "[04:59.20]" in out
    assert len(out.strip().splitlines()) == 4  # 3 aligned + 1 marker


def test_retime_missing_audio(tmp_path, capsys):
    lrc = tmp_path / "song.lrc"
    lrc.write_text(SUSPICIOUS, encoding="utf-8")
    assert main(["retime", str(tmp_path / "gone.flac"), str(lrc)]) == 2
    assert "no such file" in capsys.readouterr().err


def test_retime_stdout_stays_parseable_despite_noisy_backend(
    track, capsys, monkeypatch
):
    # demucs prints progress to stdout mid-alignment; --json consumers
    # (the lrc-editor job) must still get pure JSON on stdout
    from lyric_timing.backends.mock import MockBackend

    original = MockBackend.word_timestamps

    def noisy(self, audio_path, transcript, *, language=None):
        print("Separating track... 100%|██████|")
        return original(self, audio_path, transcript, language=language)

    monkeypatch.setattr(MockBackend, "word_timestamps", noisy)
    audio, lrc = track

    assert main(["retime", str(audio), str(lrc), "--backend", "mock", "--json"]) == 0
    out = capsys.readouterr().out
    assert [r["text"] for r in json.loads(out)] == ["One", "Two", "Three"]

    assert main(["retime", str(audio), str(lrc), "--backend", "mock"]) == 0
    out = capsys.readouterr().out
    assert all(line.startswith("[") for line in out.strip().splitlines())


def test_retime_empty_lrc(track, capsys):
    audio, lrc = track
    lrc.write_text("", encoding="utf-8")
    assert main(["retime", str(audio), str(lrc), "--backend", "mock"]) == 2
    assert "no lyric lines" in capsys.readouterr().err


def test_retime_missing_ai_deps_returns_int_not_systemexit(
    track, capsys, monkeypatch
):
    import lyric_timing.cli as cli

    monkeypatch.setattr(cli, "_ai_deps_available", lambda: False)
    audio, lrc = track
    assert main(["retime", str(audio), str(lrc)]) == 2  # returns, never raises
    assert "setup-ai.sh" in capsys.readouterr().err
