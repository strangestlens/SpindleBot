"""v10 preserves legacy presence and stores scan-linked absence conservatively."""
from __future__ import annotations

import sqlite3

import pytest

from spindlebot.core.enums import ScanStatus
from spindlebot.db import migrations
from spindlebot.db.connection import connect, open_db
from spindlebot.core.enums import ActionKind, ContentKind, RunKind
from spindlebot.db.repositories import action_repo, presence_repo, run_repo, scan_repo, sidecar_presence_repo


def test_v10_upgrades_existing_presence_without_inventing_absence(tmp_path):
    db = tmp_path / "spindlebot.db"
    conn = connect(db)
    for version, filename in migrations.MIGRATIONS:
        if version > 9:
            break
        conn.executescript((migrations._SCHEMA_DIR / filename).read_text())
        conn.execute(f"PRAGMA user_version = {version}")
    conn.execute("INSERT INTO location (uuid, name, kind) VALUES ('u','RetentionDrive','local_drive')")
    conn.execute("INSERT INTO audio_content (identity,identity_kind,first_seen_utc,last_seen_utc) VALUES ('audio','audio_md5',0,0)")
    conn.execute("INSERT INTO sidecar_content (parent_kind,parent_id,role,sha256,first_seen_utc,last_seen_utc) VALUES ('track',1,'lrc','lyric-sha',0,0)")
    conn.execute("INSERT INTO audio_presence (audio_id,location_id,present,rel_path,file_sha256,observed_utc) VALUES (1,1,0,'track.flac','audio-sha',100)")
    conn.execute("INSERT INTO sidecar_presence (sidecar_id,location_id,present,rel_path,file_sha256,observed_utc) VALUES (1,1,1,'track.lrc','lyric-sha',100)")
    conn.execute("INSERT INTO run (kind,started_utc) VALUES ('reconcile',100)")
    conn.execute("INSERT INTO pending_action (run_id,action_kind,content_kind,content_id,rel_path,expected_sha256,created_utc) VALUES (1,'copy','audio',1,'track.flac','audio-sha',100)")
    conn.commit()
    assert migrations.current_version(conn) == 9
    assert migrations.migrate(conn) == 10
    conn.close()
    conn = open_db(db)
    try:
        assert migrations.migrate(conn) == 10
        audio = presence_repo.get(conn, 1, 1)
        sidecar = sidecar_presence_repo.get(conn, 1, 1)
        assert not audio.present and audio.file_sha256 == "audio-sha"
        assert sidecar.present and sidecar.file_sha256 == "lyric-sha"
        assert audio.absence_scan_id is sidecar.absence_scan_id is None
        legacy = action_repo.get(conn, 1)
        assert legacy.source_rel_path is None and not legacy.dest_must_be_absent
        assert legacy.expected_sha256 == "audio-sha"
        scan = scan_repo.start_scan(conn, 1, 100)
        scan_repo.finish_scan(conn, scan, files_seen=0, status=ScanStatus.OK, now=101)
        for repo, content_key in ((presence_repo, "audio_id"), (sidecar_presence_repo, "sidecar_id")):
            row = repo.set_presence(conn, **{content_key: 1}, location_id=1,
                                    present=False, observed_utc=100, absence_scan_id=scan)
            assert row.absence_scan_id == scan
        conn.commit()
        run = run_repo.start_run(conn, RunKind.RECONCILE, now=100)
        planned = action_repo.add(
            conn, run_id=run, action_kind=ActionKind.COPY,
            content_kind=ContentKind.AUDIO, content_id=1, now=100,
            rel_path="target/track.flac", source_rel_path="source/track.flac",
            expected_sha256="audio-sha", dest_must_be_absent=True,
        )
        # Dropping old scan history invalidates its authorization, rather than
        # dangling an id or deleting content/presence.
        conn.execute("DELETE FROM location_scan WHERE id = ?", (scan,))
        assert presence_repo.get(conn, 1, 1).absence_scan_id is None
        assert sidecar_presence_repo.get(conn, 1, 1).absence_scan_id is None
        retained_plan = action_repo.get(conn, planned.id)
        assert retained_plan.source_rel_path == "source/track.flac"
        assert retained_plan.dest_must_be_absent, "history deletion must not weaken execution safety"
        with pytest.raises(sqlite3.IntegrityError):
            presence_repo.set_presence(conn, audio_id=1, location_id=1,
                                       present=False, observed_utc=100, absence_scan_id=999)
    finally:
        conn.close()
