"""lrc-editor /ai-arrange job orchestration, exercised with the mock backend.

The editor script shells out to `<venv-python> -m lyric_timing retime`; here
the venv python is monkeypatched to the current interpreter and the backend
to `mock`, so no heavy deps are needed.
"""

import importlib.machinery
import importlib.util
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("flask")

ROOT = Path(__file__).resolve().parent.parent

SUSPICIOUS_LINES = ["First line here", "Second line here", "Third line here"]


@pytest.fixture()
def editor(monkeypatch, tmp_path):
    # The script inspects sys.argv and writes an empty draft on import.
    monkeypatch.setattr(sys, "argv", ["lrc-editor"])
    draft = Path("/tmp/lrc-editor-draft.lrc")
    saved_draft = draft.read_bytes() if draft.exists() else None

    loader = importlib.machinery.SourceFileLoader(
        "lrc_editor_script", str(ROOT / "lrc-editor")
    )
    spec = importlib.util.spec_from_loader("lrc_editor_script", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)

    monkeypatch.setattr(mod, "AI_VENV_PY", Path(sys.executable))
    monkeypatch.setattr(mod, "PIPELINE_DIR", ROOT)
    audio = tmp_path / "song.flac"
    audio.write_bytes(b"not real audio")
    mod.state["flac_path"] = audio
    mod.state["lrc_path"] = audio.with_suffix(".lrc")

    yield mod

    if saved_draft is not None:
        draft.write_bytes(saved_draft)


def _wait_for_job(mod, timeout=60.0):
    thread = mod.ai_job["thread"]
    assert thread is not None
    thread.join(timeout)
    assert not thread.is_alive(), "ai job did not finish in time"


def test_ai_arrange_mock_end_to_end(editor):
    client = editor.app.test_client()
    r = client.post("/ai-arrange", json={"lines": SUSPICIOUS_LINES, "backend": "mock"})
    assert r.get_json()["ok"] is True

    _wait_for_job(editor)
    status = client.get("/ai-arrange/status").get_json()
    assert status["running"] is False
    assert status["error"] is None
    result = status["result"]
    assert [x["text"] for x in result] == SUSPICIOUS_LINES
    times = [x["time"] for x in result]
    assert times == sorted(times) and times[0] > 0
    assert all("confidence" in x for x in result)


def test_ai_arrange_requires_file(editor):
    editor.state["flac_path"] = None
    r = editor.app.test_client().post("/ai-arrange", json={"lines": SUSPICIOUS_LINES})
    assert r.get_json() == {"ok": False, "error": "No file loaded"}


def test_ai_arrange_requires_lines(editor):
    r = editor.app.test_client().post("/ai-arrange", json={"lines": ["  ", ""]})
    j = r.get_json()
    assert j["ok"] is False and "No lyric text" in j["error"]


def test_ai_arrange_missing_venv(editor, monkeypatch):
    monkeypatch.setattr(editor, "AI_VENV_PY", Path("/nonexistent/python"))
    r = editor.app.test_client().post("/ai-arrange", json={"lines": SUSPICIOUS_LINES})
    j = r.get_json()
    assert j["ok"] is False and "setup-ai.sh" in j["error"]


def test_ai_arrange_single_job_guard(editor):
    slow = threading.Thread(target=time.sleep, args=(2.0,), daemon=True)
    slow.start()
    editor.ai_job["thread"] = slow
    client = editor.app.test_client()

    r = client.post("/ai-arrange", json={"lines": SUSPICIOUS_LINES})
    assert r.status_code == 409

    status = client.get("/ai-arrange/status").get_json()
    assert status["running"] is True
    assert status["result"] is None


def test_ai_arrange_default_backend_is_a_valid_cli_choice(editor, monkeypatch):
    # The route's default backend string must exist in the retime CLI —
    # a backend rename that misses this default breaks the button silently
    # (the frontend never sends an explicit backend).
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd

        class R:
            returncode = 0
            stdout = "[]"
            stderr = ""

        return R()

    monkeypatch.setattr(editor.subprocess, "run", fake_run)
    client = editor.app.test_client()
    assert client.post(
        "/ai-arrange", json={"lines": SUSPICIOUS_LINES}
    ).get_json()["ok"] is True
    _wait_for_job(editor)

    sent_backend = captured["cmd"][captured["cmd"].index("--backend") + 1]
    from lyric_timing.cli import build_parser

    parser = build_parser()
    parser.parse_args(["retime", "a.flac", "a.lrc", "--backend", sent_backend])


def test_ai_arrange_subprocess_failure_reported(editor, monkeypatch):
    # a python that exits nonzero for any invocation
    monkeypatch.setattr(editor, "AI_VENV_PY", Path("/usr/bin/false"))
    client = editor.app.test_client()
    r = client.post("/ai-arrange", json={"lines": SUSPICIOUS_LINES, "backend": "mock"})
    assert r.get_json()["ok"] is True
    _wait_for_job(editor)
    status = client.get("/ai-arrange/status").get_json()
    assert status["error"] is not None
    assert status["result"] is None
