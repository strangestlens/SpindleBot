"""lrc-editor audit page: run job, saved-state recall, load-into-editor."""

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("flask")

ROOT = Path(__file__).resolve().parent.parent

SUSPICIOUS = "[00:00.00] One\n[00:00.00] Two\n[00:00.00] Three"
HEALTHY = "[00:10.00]One\n[00:55.00]Two\n[01:40.00]Three\n"


@pytest.fixture()
def editor(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["lrc-editor"])
    draft = Path("/tmp/lrc-editor-draft.lrc")
    saved_draft = draft.read_bytes() if draft.exists() else None

    loader = importlib.machinery.SourceFileLoader(
        "lrc_editor_script_audit", str(ROOT / "lrc-editor")
    )
    spec = importlib.util.spec_from_loader("lrc_editor_script_audit", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)

    monkeypatch.setattr(mod, "PIPELINE_DIR", ROOT)
    monkeypatch.setattr(mod, "UI_STATE_PATH", tmp_path / "state" / "ui.json")

    yield mod

    if saved_draft is not None:
        draft.write_bytes(saved_draft)


@pytest.fixture()
def library(tmp_path):
    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "01 Bad.lrc").write_text(SUSPICIOUS, encoding="utf-8")
    (album / "01 Bad.flac").write_bytes(b"fake audio")
    (album / "02 Good.lrc").write_text(HEALTHY, encoding="utf-8")
    return tmp_path


def _wait_for_job(mod, timeout=60.0):
    thread = mod.audit_job["thread"]
    assert thread is not None
    thread.join(timeout)
    assert not thread.is_alive(), "audit job did not finish in time"


def test_audit_page_serves(editor):
    r = editor.app.test_client().get("/audit")
    assert r.status_code == 200
    assert "Lyric Timing Audit" in r.get_data(as_text=True)


def test_audit_run_end_to_end(editor, library, tmp_path):
    client = editor.app.test_client()
    output = tmp_path / "audit.json"
    r = client.post(
        "/audit/run", json={"library": str(library), "output": str(output)}
    )
    assert r.get_json()["ok"] is True
    _wait_for_job(editor)

    status = client.get("/audit/status").get_json()
    assert status["error"] is None
    suspicious = [x for x in status["result"] if x["suspicious"]]
    assert len(suspicious) == 1
    assert suspicious[0]["path"].endswith("01 Bad.lrc")

    # output file written and parseable
    assert json.loads(output.read_text(encoding="utf-8")) == status["result"]
    # paths remembered
    ui = json.loads(editor.UI_STATE_PATH.read_text(encoding="utf-8"))
    assert ui == {"audit_library": str(library), "audit_json": str(output)}


def test_audit_saved_recalls_previous_results(editor, library, tmp_path):
    client = editor.app.test_client()
    output = tmp_path / "audit.json"
    client.post("/audit/run", json={"library": str(library), "output": str(output)})
    _wait_for_job(editor)

    saved = client.get("/audit/saved").get_json()
    assert saved["library"] == str(library)
    assert saved["json_path"] == str(output)
    assert len(saved["results"]) == 2


def test_audit_saved_empty_state(editor):
    saved = editor.app.test_client().get("/audit/saved").get_json()
    assert saved == {"library": None, "json_path": None, "results": None}


def test_audit_run_validates_paths(editor, tmp_path):
    client = editor.app.test_client()
    j = client.post(
        "/audit/run",
        json={"library": str(tmp_path / "nope"), "output": str(tmp_path / "o.json")},
    ).get_json()
    assert j["ok"] is False and "not a directory" in j["error"]


def test_load_track_from_audit_row(editor, library):
    client = editor.app.test_client()
    lrc = library / "Artist" / "Album" / "01 Bad.lrc"
    assert client.post("/load", json={"lrc": str(lrc)}).get_json()["ok"] is True
    assert editor.state["flac_path"] == lrc.with_suffix(".flac")
    assert editor.state["lrc_path"] == lrc
    meta = client.get("/meta").get_json()
    assert meta["filename"] == "01 Bad.flac"


def test_load_track_without_audio(editor, tmp_path):
    lrc = tmp_path / "orphan.lrc"
    lrc.write_text(SUSPICIOUS, encoding="utf-8")
    j = editor.app.test_client().post("/load", json={"lrc": str(lrc)}).get_json()
    assert j["ok"] is False and "no audio file" in j["error"]
