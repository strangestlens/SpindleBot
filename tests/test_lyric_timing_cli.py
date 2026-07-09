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
